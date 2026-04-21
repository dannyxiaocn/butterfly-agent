from __future__ import annotations
import importlib.util
import os
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from butterfly.core.provider import Provider
from butterfly.core.types import Message, TokenUsage, ToolCall
from butterfly.llm_engine.model_catalog import ModelSpec, get_model_spec

if TYPE_CHECKING:
    from butterfly.core.tool import Tool


class AnthropicProvider(Provider):
    _supports_thinking: ClassVar[bool] = True
    """LLM provider backed by Anthropic Claude."""

    _supports_cache_control: ClassVar[bool] = True
    # When True, thinking is enabled via Anthropic's betas header + thinking param.
    # When False (e.g. Kimi), thinking is enabled via extra_body only (no betas).
    _thinking_uses_betas: ClassVar[bool] = True
    # When True, the provider can emit the adaptive-thinking request shape
    # (``thinking={type: "adaptive", display: ...}`` + ``output_config``).
    # Kimi's Anthropic-compat surface cannot — override to False there so the
    # provider always falls back to the legacy ``enabled`` + budget_tokens
    # branch even when the model spec says ``adaptive``.
    _supports_adaptive_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import anthropic as _anthropic
            import httpx
        except ImportError:
            raise ImportError("Install anthropic: pip install anthropic") from None
        http_client = _build_http_client(httpx)
        client_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        if default_headers is not None:
            client_kwargs["default_headers"] = default_headers
        self._client = _anthropic.AsyncAnthropic(**client_kwargs)
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def complete(
        self,
        messages: list[Message],
        tools: list["Tool"],
        system_prompt: str,
        model: str,
        *,
        on_text_chunk: Callable[[str], None] | None = None,
        on_thinking_start: Callable[[], None] | None = None,
        on_thinking_end: Callable[[str], None] | None = None,
        cache_system_prefix: str = "",
        cache_last_human_turn: bool = False,
        thinking: bool = False,
        thinking_budget: int = 8000,
        thinking_effort: str = "high",  # caller-side override when spec lacks one
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        spec = get_model_spec(model)

        # Cache strategy: dispatched via spec.cache_strategy (defaults to
        # "single" when unknown — identical to the pre-spec behavior). The
        # helpers return (api_messages, system_param, extra_kwargs) so
        # ``complete`` just composes; auto strategy routes its breakpoint
        # through the top-level ``cache_control`` kwarg rather than per-block.
        api_messages, system_param, cache_extra_kwargs = _apply_cache_strategy(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            cache_last_human_turn=cache_last_human_turn,
            supports_cache=self._supports_cache_control,
            system_prompt=system_prompt,
        )
        api_tools = [t.to_api_dict() for t in tools] if tools else []

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system_param,
            "messages": api_messages,
        }
        # ``cache_extra_kwargs`` is empty for single/two_plus_two and carries
        # ``cache_control`` at the request top level for the "auto" strategy.
        kwargs.update(cache_extra_kwargs)
        if thinking and self._supports_thinking:
            _apply_thinking_kwargs(
                kwargs,
                spec=spec,
                thinking_effort=thinking_effort,
                thinking_budget=thinking_budget,
                supports_adaptive=self._supports_adaptive_thinking,
                thinking_uses_betas=self._thinking_uses_betas,
                max_tokens_floor=self.max_tokens,
            )
        if api_tools:
            kwargs["tools"] = api_tools

        # beta.messages is required when betas=[...] is set (e.g. interleaved thinking).
        # Regular messages.stream/create does not accept the 'betas' kwarg.
        betas = kwargs.pop("betas", None)
        messages_ns = self._client.beta.messages if betas else self._client.messages

        # Thinking state is accumulated INSIDE the stream path (it's the only
        # place where block-lifecycle events are visible). We never forward
        # thinking text into `on_text_chunk` — that would leak it into the
        # partial_text channel that renders as regular assistant output in the
        # web UI. Instead thinking is delivered through the dedicated
        # on_thinking_start / on_thinking_end hooks so the UI can render a
        # tool-like "💭 Thinking…" cell.
        streamed_thinking_blocks: list[str] = []
        saw_streamed_thinking = False
        if on_text_chunk is not None or on_thinking_start is not None or on_thinking_end is not None:
            stream_kwargs = {"betas": betas, **kwargs} if betas else kwargs
            async with messages_ns.stream(**stream_kwargs) as stream:
                current_thinking_parts: list[str] = []
                thinking_active = False
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        btype = getattr(block, "type", None)
                        if btype in ("thinking", "redacted_thinking"):
                            thinking_active = True
                            current_thinking_parts = []
                            saw_streamed_thinking = True
                            if on_thinking_start is not None:
                                try:
                                    on_thinking_start()
                                except Exception:  # noqa: BLE001 - hook must not crash stream
                                    pass
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", None) or ""
                            if text and on_text_chunk is not None:
                                on_text_chunk(text)
                        elif dtype == "thinking_delta":
                            # BUFFER thinking locally; do NOT forward to UI text.
                            part = getattr(delta, "thinking", None) or ""
                            if part:
                                current_thinking_parts.append(part)
                    elif etype == "content_block_stop":
                        if thinking_active:
                            thinking_active = False
                            body = "".join(current_thinking_parts)
                            current_thinking_parts = []
                            streamed_thinking_blocks.append(body)
                            if on_thinking_end is not None:
                                try:
                                    on_thinking_end(body)
                                except Exception:  # noqa: BLE001
                                    pass
                response = await stream.get_final_message()
        else:
            create_kwargs = {"betas": betas, **kwargs} if betas else kwargs
            response = await messages_ns.create(**create_kwargs)

        content_text = ""
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "thinking":
                # Non-stream path (or stream without start/delta visibility):
                # if we didn't already emit the block via the streaming
                # lifecycle, synthesize a start+end pair now so the UI still
                # renders a thinking cell. Still never forwarded to text_chunk.
                if not saw_streamed_thinking:
                    thinking_text = _extract_thinking_text(block)
                    if on_thinking_start is not None:
                        try:
                            on_thinking_start()
                        except Exception:  # noqa: BLE001
                            pass
                    if on_thinking_end is not None:
                        try:
                            on_thinking_end(thinking_text)
                        except Exception:  # noqa: BLE001
                            pass
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        usage = _extract_usage(response)
        return content_text, tool_calls, usage


def _build_http_client(httpx_module: Any) -> Any | None:
    """Prefer explicit HTTP(S) proxies when SOCKS support is unavailable.

    Some local environments export both HTTP(S)_PROXY and ALL_PROXY=socks5://...
    but do not have socksio installed. httpx then errors before the request is
    sent. When that happens, pin the client to the HTTP(S) proxy explicitly and
    ignore the environment proxy auto-detection.
    """
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    all_proxy = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")

    explicit_proxy = https_proxy or http_proxy
    if not explicit_proxy:
        return None

    if _is_socks_proxy(all_proxy) and not _has_socks_support():
        return httpx_module.AsyncClient(proxy=explicit_proxy, trust_env=False)
    return None


def _is_socks_proxy(proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    return proxy_url.lower().startswith(("socks4://", "socks4a://", "socks5://", "socks5h://"))


def _has_socks_support() -> bool:
    return importlib.util.find_spec("socksio") is not None


def _extract_usage(response: Any) -> TokenUsage:
    """Extract token usage from an Anthropic API response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    # Kimi thinking mode reports reasoning_tokens via output_tokens_details.
    # Anthropic proper folds reasoning into output_tokens and doesn't expose
    # this field, in which case the getattr default of 0 is correct.
    out_details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(out_details, "reasoning_tokens", 0) if out_details else 0
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        reasoning_tokens=reasoning or 0,
    )


def _extract_thinking_text(block: Any) -> str:
    thinking = getattr(block, "thinking", None)
    if isinstance(thinking, str):
        return thinking
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    return ""


# Sentinel used by ``_build_system_param`` to distinguish "caller didn't pass
# cache_control" (use the legacy default) from "caller passed None" (caller
# explicitly wants the prefix block uncached — the ``auto`` strategy does
# this). A plain default of ``None`` would conflate the two.
_DEFAULT_CACHE_CONTROL_SENTINEL: dict = {"__sentinel__": "default"}


def _build_system_param(
    cache_prefix: str,
    dynamic: str,
    supports_cache: bool,
    *,
    cache_control: dict | None = _DEFAULT_CACHE_CONTROL_SENTINEL,  # type: ignore[assignment]
    mark_dynamic: bool = False,
) -> str | list[dict]:
    """Build the system param for the Anthropic API.

    When caching is supported and a prefix is provided, returns a list of text
    blocks with ``cache_control`` on the prefix. Otherwise returns a plain
    string.

    ``cache_control`` is the dict to stamp on the prefix block. Three values
    are meaningful:

      * Omitted (sentinel default) → stamp ``{type: "ephemeral"}`` — the
        pre-YAML shape. Preserves existing test fixtures that call this
        helper with only positional args.
      * Explicit dict (e.g. ``{type: "ephemeral", ttl: "1h"}``) → stamp that
        exact dict. The strategy-driven path uses this.
      * ``None`` → leave the prefix uncached. The ``auto`` strategy uses this
        because it caches at the request top level instead.

    ``mark_dynamic`` additionally stamps the dynamic block with the same
    control; used by ``two_plus_two`` to consume its second system breakpoint.
    """
    if not cache_prefix:
        return dynamic
    if not supports_cache:
        # Concatenate for providers that don't support cache_control
        return (cache_prefix + "\n" + dynamic).strip() if dynamic else cache_prefix

    # Resolve the sentinel to the legacy default.
    effective_ctrl: dict | None
    if cache_control is _DEFAULT_CACHE_CONTROL_SENTINEL:
        effective_ctrl = {"type": "ephemeral"}
    else:
        effective_ctrl = cache_control

    prefix_block: dict = {"type": "text", "text": cache_prefix}
    if effective_ctrl is not None:
        prefix_block["cache_control"] = dict(effective_ctrl)
    blocks: list[dict] = [prefix_block]
    if dynamic:
        dyn_block: dict = {"type": "text", "text": dynamic}
        if mark_dynamic and effective_ctrl is not None:
            dyn_block["cache_control"] = dict(effective_ctrl)
        blocks.append(dyn_block)
    return blocks


def _apply_thinking_kwargs(
    kwargs: dict[str, Any],
    *,
    spec: ModelSpec | None,
    thinking_effort: str,
    thinking_budget: int,
    supports_adaptive: bool,
    thinking_uses_betas: bool,
    max_tokens_floor: int,
) -> None:
    """Mutate ``kwargs`` to enable thinking per the resolved mode.

    Resolution order (first non-None wins):
      * ``mode``     ← spec.thinking_mode,            default "enabled" (legacy).
      * ``display``  ← spec.thinking_display,         default "summarized".
      * ``effort``   ← spec.thinking_effort,          default caller's ``thinking_effort``.
      * ``budget``   ← spec.thinking_budget_tokens,   default caller's ``thinking_budget``.
      * ``use_betas``← spec.interleaved_thinking_beta,default class attr
                      (``_thinking_uses_betas``) — Kimi pins it False via the
                      class attr when no spec is present.

    The adaptive branch only fires when both ``supports_adaptive`` is True
    (class attr) and ``mode == "adaptive"``. KimiAnthropicProvider forces the
    legacy branch via ``_supports_adaptive_thinking = False`` — which falls
    through to the ``extra_body`` shape the Kimi gateway expects.
    """
    mode = (spec.thinking_mode if spec else None) or "enabled"
    display = (spec.thinking_display if spec else None) or "summarized"
    effort = (spec.thinking_effort if spec else None) or thinking_effort
    budget_spec = spec.thinking_budget_tokens if spec else None
    budget = budget_spec if budget_spec is not None else thinking_budget
    beta_flag = spec.interleaved_thinking_beta if spec else None
    use_betas = beta_flag if beta_flag is not None else thinking_uses_betas

    if supports_adaptive and mode == "adaptive":
        kwargs["thinking"] = {"type": "adaptive", "display": display}
        kwargs["output_config"] = {"effort": effort}
        # Adaptive doesn't use budget_tokens; leave max_tokens as-is (caller's
        # ``self.max_tokens`` already populated the kwarg).
    else:
        # Legacy "enabled" path. When the class doesn't route via the betas
        # header (Kimi), emit the ``extra_body`` shape Moonshot accepts.
        if thinking_uses_betas:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["max_tokens"] = max(max_tokens_floor, budget + 1000)

    if use_betas:
        # Redundant on 4.6+ (interleaved thinking is GA) but still required on
        # older models — the YAML-driven flag lets callers drop it per-model.
        kwargs["betas"] = ["interleaved-thinking-2025-05-14"]


def _resolve_cache_control(spec: ModelSpec | None) -> dict[str, Any]:
    """Build the ``cache_control`` dict for a breakpoint block.

    Emits ``{type: "ephemeral"}`` when the spec doesn't pin a TTL (or there's
    no spec at all) so existing tests that compare the exact-shape dict keep
    passing. When the YAML carries ``cache_ttl``, include it verbatim.
    """
    block: dict[str, Any] = {"type": "ephemeral"}
    ttl = spec.cache_ttl if spec else None
    if ttl:
        block["ttl"] = ttl
    return block


def _apply_cache_strategy(
    spec: ModelSpec | None,
    *,
    messages: list[Message],
    cache_system_prefix: str,
    cache_last_human_turn: bool,
    supports_cache: bool,
    system_prompt: str,
) -> tuple[list[dict], str | list[dict], dict[str, Any]]:
    """Dispatch to one of the three cache strategies.

    Returns ``(api_messages, system_param, extra_kwargs)``. ``extra_kwargs`` is
    empty for single/two_plus_two and carries a top-level ``cache_control``
    value for ``auto``. Unknown ``cache_strategy`` values fall through to
    ``single`` so a YAML typo still serves requests.
    """
    strategy = (spec.cache_strategy if spec else None) or "single"
    if strategy == "auto":
        return _apply_cache_auto(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            supports_cache=supports_cache,
            system_prompt=system_prompt,
        )
    if strategy == "two_plus_two":
        return _apply_cache_two_plus_two(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            cache_last_human_turn=cache_last_human_turn,
            supports_cache=supports_cache,
            system_prompt=system_prompt,
        )
    return _apply_cache_single(
        spec,
        messages=messages,
        cache_system_prefix=cache_system_prefix,
        cache_last_human_turn=cache_last_human_turn,
        supports_cache=supports_cache,
        system_prompt=system_prompt,
    )


def _apply_cache_single(
    spec: ModelSpec | None,
    *,
    messages: list[Message],
    cache_system_prefix: str,
    cache_last_human_turn: bool,
    supports_cache: bool,
    system_prompt: str,
) -> tuple[list[dict], str | list[dict], dict[str, Any]]:
    """Legacy behavior: one breakpoint on the tail user/assistant message + one
    on the system prefix (when either is configured).

    When ``spec`` is None and ``cache_ttl`` isn't set, the emitted
    ``cache_control`` is the exact ``{type: "ephemeral"}`` dict existing tests
    expect — no ``ttl`` key leaks through.
    """
    cache_ctrl = _resolve_cache_control(spec)
    breakpoint_ctrl = cache_ctrl if cache_last_human_turn and supports_cache else None
    cache_idx = _find_cache_breakpoint(messages) if breakpoint_ctrl is not None else None
    api_messages = _to_api_messages(
        messages,
        cache_breakpoint_index=cache_idx,
        cache_control=breakpoint_ctrl,
    )
    system_param = _build_system_param(
        cache_system_prefix,
        system_prompt,
        supports_cache,
        cache_control=cache_ctrl if supports_cache else None,
    )
    return api_messages, system_param, {}


def _apply_cache_two_plus_two(
    spec: ModelSpec | None,
    *,
    messages: list[Message],
    cache_system_prefix: str,
    cache_last_human_turn: bool,
    supports_cache: bool,
    system_prompt: str,
) -> tuple[list[dict], str | list[dict], dict[str, Any]]:
    """Two breakpoints on system (prefix + dynamic) plus two on the last two
    non-system user/assistant messages.

    Caveats:
      * "non-system" means ``role in {user, assistant}`` — raw ``tool`` rows
        (which become user-shaped on the wire) aren't counted as cache anchors.
      * With fewer than 2 such messages, falls back to ``single`` — a single
        breakpoint is still useful, and emitting an un-anchored one wastes a
        cache-control slot.
      * When caching is disabled at the class level (Kimi), fall back to
        ``single`` so ``supports_cache=False`` still gets the legacy shape.
    """
    if not supports_cache:
        return _apply_cache_single(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            cache_last_human_turn=cache_last_human_turn,
            supports_cache=supports_cache,
            system_prompt=system_prompt,
        )

    anchors = _two_plus_two_anchor_indices(messages)
    if len(anchors) < 2:
        return _apply_cache_single(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            cache_last_human_turn=cache_last_human_turn,
            supports_cache=supports_cache,
            system_prompt=system_prompt,
        )

    cache_ctrl = _resolve_cache_control(spec)
    api_messages = _to_api_messages(
        messages,
        cache_breakpoint_indices=anchors,
        cache_control=cache_ctrl,
    )
    # System side: stamp both the prefix and the dynamic block so we consume
    # two of the four cache_control slots Anthropic allows per request. When
    # there's no dynamic body, only the prefix gets marked — that's still one
    # breakpoint, and the caller's 2-message anchor covers the rest.
    system_param = _build_system_param(
        cache_system_prefix,
        system_prompt,
        supports_cache,
        cache_control=cache_ctrl,
        mark_dynamic=bool(system_prompt),
    )
    return api_messages, system_param, {}


def _apply_cache_auto(
    spec: ModelSpec | None,
    *,
    messages: list[Message],
    cache_system_prefix: str,
    supports_cache: bool,
    system_prompt: str,
) -> tuple[list[dict], str | list[dict], dict[str, Any]]:
    """Anthropic's Feb-2026 server-managed caching — top-level ``cache_control``
    on the request, zero per-block breakpoints.

    When the provider class doesn't honor cache_control (Kimi), fall through
    to ``single`` which also emits no top-level kwarg.
    """
    if not supports_cache:
        return _apply_cache_single(
            spec,
            messages=messages,
            cache_system_prefix=cache_system_prefix,
            cache_last_human_turn=False,
            supports_cache=supports_cache,
            system_prompt=system_prompt,
        )

    api_messages = _to_api_messages(messages)
    # Top-level cache_control still supports ttl, same shape as per-block.
    system_param = _build_system_param(
        cache_system_prefix,
        system_prompt,
        supports_cache,
        cache_control=None,  # explicitly no per-block breakpoint
    )
    return api_messages, system_param, {"cache_control": _resolve_cache_control(spec)}


def _two_plus_two_anchor_indices(messages: list[Message]) -> list[int]:
    """Return up to 2 indices into ``messages`` where we should place cache
    breakpoints — the last two ``user``/``assistant`` rows.

    Skips ``tool`` rows (tool_result payloads). Returned list is in ascending
    index order (so ``_to_api_messages`` can apply breakpoints in one pass).
    """
    picks: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role in ("user", "assistant"):
            picks.append(i)
            if len(picks) == 2:
                break
    picks.reverse()
    return picks


def _find_cache_breakpoint(messages: list[Message]) -> int | None:
    """Return the index of the last user/human message before the final message.

    This is where we place the cache breakpoint so Anthropic caches all
    conversation history up to (and including) that message on the next call.

    Returns None if there are fewer than 2 messages or no suitable breakpoint.
    """
    if len(messages) < 2:
        return None
    # Walk backwards from second-to-last message, find last non-tool role
    for i in range(len(messages) - 2, -1, -1):
        if messages[i].role in ("user", "assistant"):
            return i
    return None


# Block types the Anthropic Messages API accepts on assistant content. We
# strip anything else (notably "reasoning" from Codex/OpenAI Responses) so
# a cross-provider fallback doesn't send opaque blocks the server rejects.
_ANTHROPIC_ALLOWED_BLOCK_TYPES = frozenset({
    "text", "thinking", "redacted_thinking",
    "tool_use", "tool_result",
    "image", "document",
})


def _sanitize_content_for_anthropic(content: Any) -> Any:
    """Filter assistant content blocks down to Anthropic-accepted types.

    String content is returned as-is. A list with every block stripped
    collapses to a single ``[continued]`` text block so the turn stays
    valid (Anthropic rejects empty content arrays).
    """
    if not isinstance(content, list):
        return content
    filtered = [
        b for b in content
        if not isinstance(b, dict) or b.get("type") in _ANTHROPIC_ALLOWED_BLOCK_TYPES
    ]
    if not filtered:
        return [{"type": "text", "text": "[continued]"}]
    return filtered


def _to_api_messages(
    messages: list[Message],
    cache_breakpoint_index: int | None = None,
    cache_breakpoint_indices: list[int] | None = None,
    cache_control: dict[str, Any] | None = None,
) -> list[dict]:
    """Convert butterfly ``Message`` rows into Anthropic API payload dicts.

    Breakpoint placement supports two shapes:

      * ``cache_breakpoint_index`` — single-breakpoint legacy path (``single``
        cache strategy). Left as an int to keep the existing call sites and
        tests intact.
      * ``cache_breakpoint_indices`` — multi-breakpoint path (``two_plus_two``).
        Pass a list of ascending ints; each listed row gets its last block
        marked with ``cache_control``.

    ``cache_control`` defaults to the classic ``{type: "ephemeral"}`` dict so
    the legacy one-argument call shape (``cache_breakpoint_index=N``) with no
    explicit control block still emits the exact pre-YAML payload. When the
    caller provides the dict (strategy-driven path), it's honored verbatim.
    """
    # Normalize the two breakpoint shapes into one set lookup.
    breakpoint_set: set[int] = set()
    if cache_breakpoint_index is not None:
        breakpoint_set.add(cache_breakpoint_index)
    if cache_breakpoint_indices:
        breakpoint_set.update(cache_breakpoint_indices)

    # ``cache_control`` defaults to the legacy shape so callers that only pass
    # ``cache_breakpoint_index=N`` keep the identical pre-refactor payload.
    ctrl_dict = cache_control if cache_control is not None else {"type": "ephemeral"}

    result = []
    for i, msg in enumerate(messages):
        role = "user" if msg.role == "tool" else msg.role
        # Only sanitize ASSISTANT content — user messages and tool_result
        # payloads may legitimately contain custom block types (images,
        # documents, etc.) that the filter would otherwise strip away. The
        # cross-provider fallback hazard is purely about assistant-origin
        # provider-opaque blocks (e.g. Codex-produced reasoning).
        content = (
            _sanitize_content_for_anthropic(msg.content)
            if msg.role == "assistant"
            else msg.content
        )

        # Add cache_control at the specified breakpoint(s).
        if i in breakpoint_set:
            if isinstance(content, str):
                content = [{"type": "text", "text": content, "cache_control": dict(ctrl_dict)}]
            elif isinstance(content, list) and content:
                # Mutate last block in the list to add cache_control
                last = dict(content[-1])
                last["cache_control"] = dict(ctrl_dict)
                content = [*content[:-1], last]

        result.append({"role": role, "content": content})
    return result
