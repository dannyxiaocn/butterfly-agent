from __future__ import annotations
import asyncio
import logging
from typing import Any

from butterfly.core.hook import OnLoopEnd, OnLoopStart, OnTextChunk, OnToolCall, OnToolDone
from butterfly.core.provider import Provider
from butterfly.core.skill import Skill
from butterfly.core.tool import Tool
from butterfly.core.types import AgentResult, Message, ToolCall

_log = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class Agent:
    """A minimal LLM agent.

    Args:
        system_prompt: Defines the agent's identity and behavior.
        tools: List of Tool objects the agent can call.
        skills: List of Skill objects. File-backed skills (with a ``location``)
                are listed in a catalog so the model can activate them on
                demand (progressive disclosure). Inline skills (no location)
                have their body injected directly into the system prompt.
        model: Model identifier string (default: claude-sonnet-4-6).
        provider: LLM provider instance. If omitted, AnthropicProvider
                  is used with the ANTHROPIC_API_KEY environment variable.
        max_iterations: Max tool-call loops per run (default: 20).
    """

    def __init__(
        self,
        system_prompt: str = "",
        tools: list[Tool] | None = None,
        skills: list[Skill] | None = None,
        model: str = _DEFAULT_MODEL,
        provider: Provider | None = None,
        max_iterations: int = 20,
        task_prompt: str = "",
        env_template: str = "",
        fallback_model: str = "",
        fallback_provider: str = "",
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.system_prompt = system_prompt
        self.tools: list[Tool] = tools or []
        self.skills: list[Skill] = skills or []
        self.model = model
        self.max_iterations = max_iterations
        self.task_prompt = task_prompt
        self.env_template = env_template
        self._provider = provider
        self.fallback_model = fallback_model
        self._fallback_provider_str = fallback_provider
        self._fallback_provider: Provider | None = None
        self._history: list[Message] = []
        # Runtime-injectable fields — set by Session before each activation.
        # Not constructor params; Session owns the values, Agent owns the rendering.
        self.memory: str = ""
        self.caller_type: str = "human"  # "human" or "agent" — set per-run
        # Extra named memory layers from core/memory/*.md, sorted by filename.
        # Each entry is (label, content) where label is the .md file stem.
        self.memory_layers: list[tuple[str, str]] = []
        # App notifications from core/apps/*.md, injected as system-prompt block.
        self.app_notifications: list[tuple[str, str]] = []
        self.env_context: str = ""
        self.thinking: bool = False
        self.thinking_budget: int = 8000
        self.thinking_effort: str = "high"

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            from butterfly.llm_engine.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider()
        return self._provider

    def _get_fallback_provider(self) -> "Provider | None":
        """Resolve the fallback provider, caching the result.

        Precedence:
          * ``fallback_provider`` (registry key) wins when set — resolved once.
          * Otherwise, if only ``fallback_model`` is configured, fall back to
            the primary provider class so the caller can retry the same
            backend with a different model. (Previously this silently
            returned ``None`` and the fallback never fired.)
          * Neither set → no fallback.
        """
        if not self._fallback_provider_str and not self.fallback_model:
            return None
        if self._fallback_provider is not None:
            return self._fallback_provider
        if self._fallback_provider_str:
            from butterfly.llm_engine.registry import resolve_provider
            self._fallback_provider = resolve_provider(self._fallback_provider_str)
            return self._fallback_provider
        # fallback_model set but no fallback_provider — reuse the primary
        # provider instance; the run loop will pass fallback_model as the model.
        self._fallback_provider = self.provider
        return self._fallback_provider

    # Memory layers longer than this many lines are truncated in the prompt.
    # The agent reads the full layer on demand via bash: cat core/memory/<name>.md
    _MEMORY_LAYER_INLINE_LINES: int = 60

    @classmethod
    def _render_memory_layer(cls, name: str, content: str) -> str:
        """Render a named memory layer, truncating large ones for prompt efficiency."""
        # Bug 27: splitlines() handles \r\n / \r / \n uniformly. Join with \n
        # on BOTH branches so short memory layers are also normalised — the
        # short-path used to return raw ``content`` which kept stray \r on
        # Windows-style files.
        lines = content.splitlines()
        normalised = "\n".join(lines)
        if len(lines) <= cls._MEMORY_LAYER_INLINE_LINES:
            return f"## Memory: {name}\n\n{normalised}"
        head = "\n".join(lines[: cls._MEMORY_LAYER_INLINE_LINES])
        omitted = len(lines) - cls._MEMORY_LAYER_INLINE_LINES
        hint = f"... ({omitted} lines omitted — full content: `cat core/memory/{name}.md`)"
        return f"## Memory: {name}\n\n{head}\n{hint}"

    def _build_system_parts(self) -> tuple[str, str]:
        """Return (static_prefix, dynamic_suffix) for cache-aware prompt building.

        static_prefix  — system.md + session context. Stable across activations;
                         eligible for Anthropic prompt caching.
        dynamic_suffix — memory + skills. Changes each activation; not cached.
        """
        from butterfly.skill_engine.renderer import build_skills_block
        static_parts = [self.system_prompt] if self.system_prompt else []
        if self.env_context:
            static_parts.append("\n\n---\n" + self.env_context)

        dynamic_parts: list[str] = []
        if self.memory or self.memory_layers:
            memory_parts = []
            if self.memory:
                memory_parts.append(f"## Session Memory\n\n{self.memory}")
            for name, content in self.memory_layers:
                memory_parts.append(self._render_memory_layer(name, content))
            dynamic_parts.append("\n\n---\n" + "\n\n".join(memory_parts))
        # App notifications — core/apps/*.md, always-visible persistent channel
        if self.app_notifications:
            notif_parts = []
            for app_name, app_content in self.app_notifications:
                notif_parts.append(f"### {app_name}\n\n{app_content}")
            dynamic_parts.append("\n\n---\n## App Notifications\n\n" + "\n\n".join(notif_parts))

        # Agent-mode structured reply guidance
        if getattr(self, "caller_type", "human") == "agent":
            agent_guidance = (
                "\n\n---\n"
                "## Agent Collaboration Mode\n\n"
                "Your caller is another agent (not a human). Structure your final reply using one of these prefixes:\n\n"
                "- **[DONE]** — task completed successfully. Summarise what was accomplished.\n"
                "- **[REVIEW]** — work finished but needs human review before proceeding.\n"
                "- **[BLOCKED]** — cannot proceed; explain what is needed.\n"
                "- **[ERROR]** — an unrecoverable error occurred; include diagnostics.\n\n"
                "Always start your final reply with exactly one prefix. Keep the reply concise and machine-parseable."
            )
            dynamic_parts.append(agent_guidance)

        skills_block = build_skills_block(self.skills)
        if skills_block:
            dynamic_parts.append(skills_block)

        return "\n".join(static_parts), "\n".join(dynamic_parts)

    def _tool_map(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}

    async def run(
        self,
        input: str,
        *,
        clear_history: bool = False,
        on_text_chunk: OnTextChunk | None = None,
        on_tool_call: OnToolCall | None = None,
        on_tool_done: OnToolDone | None = None,
        on_loop_start: OnLoopStart | None = None,
        on_loop_end: OnLoopEnd | None = None,
        caller_type: str = "human",
    ) -> AgentResult:
        """Run the agent with the given input and return an AgentResult."""
        if clear_history:
            self._history = []
        self.caller_type = caller_type

        if on_loop_start:
            on_loop_start(input)

        from butterfly.core.types import TokenUsage as _TokenUsage
        system_prefix, system_dynamic = self._build_system_parts()
        tool_map = self._tool_map()
        messages: list[Message] = [*self._history, Message(role="user", content=input)]
        all_tool_calls: list[ToolCall] = []
        total_usage = _TokenUsage()

        _cache_history = bool(self._history) and getattr(
            self.provider, "_supports_cache_control", False
        )

        active_provider = self.provider
        active_model = self.model

        iterations = 0
        for _ in range(self.max_iterations):
            iterations += 1
            try:
                content, tool_calls, turn_usage = await active_provider.complete(
                    messages=messages,
                    tools=self.tools,
                    system_prompt=system_dynamic,
                    model=active_model,
                    on_text_chunk=on_text_chunk,
                    cache_system_prefix=system_prefix,
                    cache_last_human_turn=_cache_history,
                    thinking=self.thinking,
                    thinking_budget=self.thinking_budget,
                    thinking_effort=self.thinking_effort,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                # Bug 23: cancellation / interrupt must propagate, not be
                # swallowed as a "provider failure".
                raise
            except Exception as primary_exc:  # noqa: BLE001 - narrowed below
                # Bug 23: only fallback on the butterfly error taxonomy +
                # low-level transport errors (connection resets, TLS, DNS).
                # Plain Python errors (TypeError, ValueError) indicate logic
                # bugs and should propagate. Import is deferred to avoid a
                # circular import on module load.
                from butterfly.llm_engine.errors import ProviderError as _ProviderError

                if not isinstance(primary_exc, (_ProviderError, OSError)):
                    raise
                fb_provider = self._get_fallback_provider()
                fb_model = self.fallback_model or active_model
                # Only block the retry if both provider class AND model would be
                # unchanged — otherwise a "same provider, different model"
                # fallback (common when only ``fallback_model`` is set) is a
                # legitimate retry path.
                if fb_provider is None or (
                    active_provider is fb_provider and fb_model == active_model
                ):
                    raise
                # Bug 24: log the exception TYPE, not str(exc) — provider
                # error messages can contain request bodies / tokens / tracebacks.
                _log.warning(
                    "primary provider failed (%s); switching to fallback",
                    type(primary_exc).__name__,
                )
                active_provider = fb_provider
                active_model = fb_model
                content, tool_calls, turn_usage = await active_provider.complete(
                    messages=messages,
                    tools=self.tools,
                    system_prompt=system_dynamic,
                    model=active_model,
                    on_text_chunk=on_text_chunk,
                    cache_system_prefix=system_prefix,
                    cache_last_human_turn=_cache_history,
                    thinking=self.thinking,
                    thinking_budget=self.thinking_budget,
                    thinking_effort=self.thinking_effort,
                )
            total_usage = total_usage + turn_usage
            # Bug 26: preserve on_text_chunk so every turn in a multi-step
            # tool loop streams to the caller, not just the first call.

            extra_blocks = active_provider.consume_extra_blocks()

            assistant_content: Any = content
            if tool_calls or extra_blocks:
                blocks: list[Any] = []
                for eb in extra_blocks:
                    blocks.append(eb)
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                assistant_content = blocks

            messages.append(Message(role="assistant", content=assistant_content))
            all_tool_calls.extend(tool_calls)

            if not tool_calls:
                break

            if on_tool_call:
                for tc in tool_calls:
                    on_tool_call(tc.name, tc.input)

            tool_results = await _execute_tools(tool_calls, tool_map, on_tool_done=on_tool_done)
            messages.append(Message(role="tool", content=tool_results))

        self._history = list(messages)

        result = AgentResult(
            content=content,
            tool_calls=all_tool_calls,
            usage=total_usage,
            messages=list(messages),
            iterations=iterations,
        )

        if on_loop_end:
            on_loop_end(result)

        return result

    def close(self) -> None:
        """Clear conversation history. Synchronous; does not release HTTP pools.

        For full cleanup (closing the provider's underlying SDK / HTTP pool),
        await ``aclose()`` instead.
        """
        self._history = []

    async def aclose(self) -> None:
        """Async cleanup: clear history and close primary + fallback providers.

        Safe to call multiple times. Errors during provider close are
        swallowed individually so one failing provider doesn't strand the
        other's resources. When the fallback provider reuses the primary
        instance (only-``fallback_model`` path), the underlying SDK client
        is only closed once.
        """
        self._history = []
        seen: set[int] = set()
        for prov in (self._provider, self._fallback_provider):
            if prov is None or id(prov) in seen:
                continue
            seen.add(id(prov))
            try:
                await prov.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                _log.debug("provider aclose failed", exc_info=True)


async def _execute_tools(
    tool_calls: list[ToolCall],
    tool_map: dict[str, Tool],
    *,
    on_tool_done: OnToolDone | None = None,
) -> list[dict]:
    """Execute tool calls concurrently and return Anthropic-format tool_result blocks."""
    async def _call(tc: ToolCall) -> dict:
        tool = tool_map.get(tc.name)
        is_error = False
        if tool is None:
            content = f"Error: tool '{tc.name}' not found."
            is_error = True
        else:
            try:
                content = await tool.execute(**tc.input)
            except Exception as exc:
                content = f"Error executing '{tc.name}': {exc}"
                is_error = True
        if on_tool_done:
            on_tool_done(tc.name, tc.input, content)
        return {
            "type": "tool_result",
            "tool_use_id": tc.id,
            "content": content,
            "is_error": is_error,
        }

    return list(await asyncio.gather(*[_call(tc) for tc in tool_calls]))
