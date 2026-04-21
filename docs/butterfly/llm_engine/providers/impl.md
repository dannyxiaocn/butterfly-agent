# Providers — Implementation

## Files

| File | Purpose |
|------|---------|
| `_common.py` | Shared helpers for parsing JSON tool arguments |
| `anthropic.py` | Anthropic Messages API, prompt-cache support, streamed thinking |
| `openai_api.py` | OpenAI Chat Completions API (legacy + non-reasoning models) |
| `openai_responses.py` | OpenAI Responses API — reasoning models (o-series / gpt-5) |
| `kimi.py` | Kimi for Coding — `KimiOpenAIProvider` (default, OpenAI-compat) + `KimiAnthropicProvider` (opt-in, Anthropic-compat); both use `extra_body` thinking |
| `codex.py` | ChatGPT OAuth Codex Responses API over SSE |

## Provider Notes

- **Anthropic**: thinking mode uses beta Messages namespace (`client.beta.messages`) **only when the resolved kwargs carry a `betas=[...]` field** (legacy interleaved-thinking path). v2.0.31 adaptive-mode requests skip the beta namespace entirely — `thinking={type: "adaptive", display: ...}` + `output_config={effort: ...}` go through `client.messages.stream/create`, which is where 4.6+ adaptive is served. Cache strategies (`single` / `two_plus_two` / `auto`) dispatch from `_apply_cache_strategy`; see `design.md` for the selection rules and §"Anthropic — v2.0.31 ModelSpec wiring" below.
- **Kimi (default, OpenAI-compat)**: `KimiOpenAIProvider` subclasses `OpenAIProvider` and points at `https://api.kimi.com/coding/v1/`. Thinking via `extra_body={"thinking":{"type":"enabled"}}`. Usage extraction prefers Moonshot's top-level `cached_tokens`, falling back to `prompt_tokens_details.cached_tokens`; `reasoning_tokens` come from `completion_tokens_details.reasoning_tokens` when populated. Mirrors kimi-cli (`kosong/chat_provider/kimi.py`).
- **Kimi (opt-in, Anthropic-compat)**: `KimiAnthropicProvider` — same adapter shape as `AnthropicProvider` but thinking via `extra_body={"thinking":{"type":"enabled"}}`, no beta namespace. `cache_control` is not honored by this surface, so `_supports_cache_control=False`.
- **Both Kimi variants**: auth is limited to the **Kimi For Coding** path only — `KIMI_FOR_CODING_API_KEY` env var or an explicit `api_key` kwarg. There are no `KIMI_API_KEY` / `MOONSHOT_API_KEY` fallbacks and no base-URL overrides — if a proxy is required, edit the `_KIMI_*_BASE_URL` constants in `providers/kimi.py`. Both providers pass `User-Agent: claude-code/0.1.0` (see `_KIMI_DEFAULT_HEADERS` in `kimi.py`) via the new `default_headers` param on `OpenAIProvider` / `AnthropicProvider` — this header is required by Kimi's access-control gate for coding agents (without it the API returns 403 `access_terminated_error`). Value matches openclaw's `kimi-coding` extension.
- **OpenAI (Chat Completions)**: model-family-aware param scrubber (`_apply_model_specific_params`) routes reasoning models (`o*`, `gpt-5*`, `gpt-oss*`) to `max_completion_tokens` + `reasoning_effort`; legacy models keep `max_tokens`.
- **OpenAI Responses**: Responses API path — flat tool schema, `instructions` field separate from `input`, `max_output_tokens`, `reasoning={"effort","summary":"auto"}`, `include=["reasoning.encrypted_content"]` when thinking. Replays reasoning items on subsequent turns (see below). **v2.0.31**: `_stream` gained the same per-event `_CHUNK_IDLE_TIMEOUT = 45.0` watchdog as Codex (measured per SDK event rather than per byte-chunk; same 45-s ceiling). **Built-in tools** handled in the shared section below.
- **Codex**: Responses-API over SSE against the ChatGPT-OAuth endpoint. Default model `gpt-5.4` (ChatGPT-OAuth rejects `gpt-5-codex` even though codex-rs defaults to it). The "use my default" signal is an explicit allow-list (`_is_codex_compatible_model` — `gpt-*`, `o\d+-*`, `codex-*`, `ft:gpt-*`), so Kimi/Gemini/typos no longer slip through to a 400. Token refresh is async (httpx) so it doesn't block the event loop, uses a module-level `asyncio.Lock` to serialize concurrent refreshes. **Auth store** (v2.0.13): tokens are read/written to `~/.butterfly/auth.json` (butterfly's own session, `0o600`), NOT `~/.codex/auth.json`. On first use, if `~/.butterfly/auth.json` is absent but `~/.codex/auth.json` exists, `_read_auth()` migrates the tokens automatically. This prevents refresh-token rotation conflicts when the Codex CLI or VS Code extension refreshes their token (which would have invalidated butterfly's session when sharing `~/.codex/auth.json`). **v2.0.31**: `_get_auth_async` prefers the persisted `account_id` field when present; `_refresh_access_token_async` re-extracts `chatgpt_account_id` from the fresh JWT and stores it on the tokens dict, so every refresh auto-corrects a rotation server-side. Legacy auth.json files (no `account_id`) still work via a JWT-extraction fallback. Sends `max_output_tokens`, `prompt_cache_key`, and `session_id` header; **no cache_read_tokens have been observed in practice on the ChatGPT-OAuth backend** as of 2026-04-15, so the caching fields are best-effort. SSE parser caps buffer growth at 1 MiB; v2.0.31 added a per-chunk **idle watchdog** (`_CHUNK_IDLE_TIMEOUT = 45.0`) that raises `ProviderTimeoutError` on stall instead of waiting out the 600-s read timeout. Stream error taxonomy: codes match an explicit enum (`context_length_exceeded`, `rate_limit_exceeded`, `invalid_api_key`, …) plus narrow message phrases — no loose substring matching. Reasoning items are replayed across turns with `summary: null` coerced to `[]`. **Built-in tools** (v2.0.31, see §"Built-in tools — Codex + OpenAI Responses" below).

## Cross-provider fallback sanitization

When the primary provider is reasoning-aware (Codex, OpenAI Responses) and emits a `reasoning` block captured into the assistant `Message.content`, a later fallback to a non-reasoning provider (Anthropic / Kimi / OpenAI Chat Completions) used to send that opaque block verbatim and 400. Now:

- `anthropic._sanitize_content_for_anthropic` strips any block type not on Anthropic's allow-list; a fully-filtered assistant message collapses to a single `[continued]` text block.
- `openai_api._build_messages` substitutes the same `[continued]` placeholder when a filtered assistant message has no text and no tool_calls.

This keeps the default agent config (`codex-oauth` primary, `kimi-coding-plan` fallback) reliable.

## Agent fallback scope

`Agent.run` only switches to the fallback provider on `ProviderError` (butterfly taxonomy) and `OSError` (transport / DNS / TLS). `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, and plain Python errors (`TypeError`, `ValueError`, `AssertionError`, …) propagate — they indicate either a deliberate cancellation or a logic bug that the fallback can't fix. The switch is logged via the `butterfly.core.agent` logger (exception type only; we do not log `str(exc)` since provider error messages can contain request bodies or secrets).

## Reasoning continuation (Codex + OpenAI Responses)

When `thinking=True`, the provider sends `include=["reasoning.encrypted_content"]` so the server returns encrypted reasoning items on each turn. The provider captures these items during the stream and surfaces them via `consume_extra_blocks()`, which the agent loop appends to the assistant `Message.content`. On the next turn `_convert_assistant` emits each reasoning block back into the request `input` verbatim, and the server resumes its chain-of-thought without re-thinking.

Every concrete provider inherits `Provider.consume_extra_blocks()` from the ABC (default: empty list), so the agent loop calls it directly.

### Kimi OpenAI-compat: `reasoning_content` echo (v2.0.16)

Moonshot/Kimi's OpenAI-compatible surface streams reasoning tokens as `delta.reasoning_content` alongside the assistant text and expects every assistant message carrying `tool_calls` on subsequent requests to include the matching `reasoning_content` string. Losing it causes a 400:

```
{"error": {"message": "thinking is enabled but reasoning_content is missing in assistant tool call message at index N", "type": "invalid_request_error"}}
```

Prior to v2.0.16 the stream parser only tracked `delta.content` and `delta.tool_calls`, so Kimi would 400 on iteration 2 of every tool-using turn — the agent loop would commit the tool call, execute it, re-call Kimi with the tool result, and the second call would crash. `Agent.run` treated that as a `ProviderError` and (if no fallback was configured, or the fallback also failed) raised; `Session._do_chat` re-raised the exception up to the dispatcher which rejected the caller's future — the user saw a tool cell followed by silence.

The fix wires reasoning_content through the same round-trip as Codex reasoning:

1. `OpenAIProvider._stream_complete` / `_non_stream_complete` accumulate `reasoning_content` into `self._pending_reasoning_content` and fire `on_thinking_start` / `on_thinking_end(body)` so the session emits `thinking_start` / `thinking_done` IPC events (thinking cell in the web UI). Standard OpenAI streams never populate `reasoning_content`, so the hooks stay silent there.
2. `OpenAIProvider.consume_extra_blocks` returns `[{"type": "reasoning_content", "text": "…"}]` when reasoning was captured, clearing the slot after — `Agent.run` appends the block to the committed assistant `Message.content` before the `text` / `tool_use` blocks.
3. `_build_messages` sees the `reasoning_content` block on an assistant message and — **only when that message also has `tool_calls`** — stamps `entry["reasoning_content"] = block["text"]` on the OpenAI request entry. Plain-text turns never carry the field, so a mixed history replayed against a standard OpenAI model does not 400 on an unknown assistant field.
4. `Session._clean_content_for_api` allow-lists the block type, so a session reload preserves the field through `context.jsonl` round-trip.

### Codex SSE: defending against summary leak (v2.0.11)

The ChatGPT-OAuth backend has been observed to deliver reasoning summary content via `response.output_text.delta` events nested inside a `reasoning` output_item — not via the spec'd `response.reasoning_summary_text.delta` channel. Without defenses, that content leaks into `text_parts` and the assistant message buffer (visible as the model "narrating" its plan in the final reply, with no Thinking… cell ever opening).

`_parse_sse_stream` defends with three layered fixes:

1. **`current_output_item_type` tracking** — set on `response.output_item.added`, cleared on `.done`. When `response.output_text.delta` fires while the open item is `reasoning`, the delta routes to `thinking_parts`, not `text_parts`.
2. **`item.summary[].text` fallback** — on `response.output_item.done` for a reasoning item, if `thinking_parts` is empty (no streamed deltas captured) the body is extracted from `item.summary` via `_extract_summary_text` and emitted as the thinking block, so the UI never gets an empty Thought-for-Xs cell.
3. **Catch-all for `response.reasoning*` / `*summary*` etypes** — any unknown variant routes its `delta` / `text` (or nested `part.text` / `item.summary`) to thinking. Set `BUTTERFLY_CODEX_DEBUG=1` to print the names of unhandled etypes for backend-change diagnosis.

## Error taxonomy

`butterfly/llm_engine/errors.py` exposes a normalized taxonomy providers should raise from recognizable error conditions:

| Error | When |
|-------|------|
| `AuthError` | 401/403, expired/revoked refresh token |
| `RateLimitError` | 429 with optional `retry_after` |
| `ContextWindowExceededError` | server-reported context-length stop |
| `BadRequestError` | 400 (malformed request) |
| `ProviderTimeoutError` | client-side or server-side timeout (renamed from `TimeoutError` in v2.0.4 to stop shadowing the Python builtin) |
| `ServerError` | 5xx (transient) |
| `ProviderError` | base — fallback for unclassified failures |

`str(err)` on any of these renders the message plus a `[provider=… status=…]` tag (and `[retry_after=Ns]` on rate-limits) so logs carry full context without callers having to inspect attributes. Codex parses `response.failed` events into this taxonomy; HTTP non-200 statuses are routed through `_raise_from_status`.

## Lifecycle

Every provider implements `async def aclose(self) -> None`. The base class default is a no-op; `AnthropicProvider`, `OpenAIProvider`, and `OpenAIResponsesProvider` forward to the underlying SDK client's `close()`. `Agent.aclose()` clears history and closes the primary + fallback providers, swallowing per-provider errors so one failure doesn't strand the other's resources. The legacy synchronous `Agent.close()` only clears history — use `aclose()` for full cleanup.

## Tool-result rendering

`butterfly/llm_engine/providers/_common.py::stringify_tool_result_content` is the single source of truth for converting a `tool_result` block payload to a flat string. All three providers (Codex, OpenAI Responses, OpenAI Chat Completions) call it directly, so the same payload renders identically regardless of which backend receives it. Rules: `text` blocks pass through; non-text dict blocks become `[<type> block omitted]` (no `dict.__repr__` leakage); plain string entries pass through; other shapes are dropped.

## TokenUsage

`butterfly.core.types.TokenUsage` has five fields: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`. `input_tokens` is *non-cached* input across all providers (OpenAI's `prompt_tokens` has cached subtracted out so the math `input + cache_read = total input` holds uniformly).

## thinking_effort conventions

All providers accept `thinking_effort ∈ {"none", "minimal", "low", "medium", "high", "xhigh"}`. An invalid value falls back to `"medium"` uniformly (Codex / OpenAI Responses / OpenAI Chat Completions). For the Responses API an explicit `"none"` is honored by **omitting** the `reasoning` request field entirely — sending `reasoning={"effort":"none"}` would either 400 or still bill reasoning tokens depending on the model.

## Fallback provider / model

`Agent._get_fallback_provider()` returns:

- `None` when neither `fallback_provider` nor `fallback_model` is configured.
- A freshly-resolved provider when only `fallback_provider` is set.
- The primary provider instance itself when only `fallback_model` is set — the run loop then retries with the same provider class and the new model.

The run loop blocks the retry only when both the provider class AND the model would be unchanged, so "same provider, different model" is a valid fallback path.

## Login helpers

Two CLI helpers live at `ui/cli/login.py` and are wired into the top-level `butterfly` entry point:

- **`butterfly codex login`** (v2.0.13) — runs a built-in OpenAI device-code OAuth flow; no dependency on the `codex` CLI. Steps: (1) POST `https://auth.openai.com/api/accounts/deviceauth/usercode` → get `user_code` / `device_auth_id`; (2) show user the code and the URL `https://auth.openai.com/codex/device`; (3) poll `https://auth.openai.com/api/accounts/deviceauth/token` until authorized (404 = still pending; 403 with `error: access_denied/expired_token` = terminal denial; other 403 = still pending); (4) exchange `authorization_code` + `code_verifier` for tokens via `https://auth.openai.com/oauth/token`. Tokens are written to `~/.butterfly/auth.json` (butterfly's own session, separate from `~/.codex/auth.json`). On first run, offers to import from `~/.codex/auth.json` to avoid a re-login for upgrading users. Mirrors hermes-agent's approach (`_login_openai_codex` / `_codex_device_code_login`). Flags: `--import-codex-cli` (import without prompting), `--no-verify` (skip the post-login account-ID display).
- **`butterfly kimi login`** (v2.0.31 — OAuth-first rewrite) — auto-selects between three paths. Default: fast-path skip + verify when `KIMI_FOR_CODING_API_KEY` already resolves via `os.environ` or `.env`; otherwise run Moonshot's KLIP-14 device-authorization flow. Flags: `--oauth` (force OAuth even with a key set), `--api-key` (force legacy getpass paste; mutually exclusive with `--oauth`), `--key KEY` (non-interactive: write then verify), `--no-verify`, `--env-file PATH`. OAuth endpoints: `auth.kimi.com/api/oauth/device_authorization` + `/api/oauth/token`; `client_id=17e5f671-d194-4dfb-9706-5516cb48c098` (the public kimi-cli client id; reused since butterfly is a CLI of the same shape). Polling: `authorization_pending` → retry at `interval`; `slow_down` → double interval (cap 15 s); `access_denied` / `expired_token` → raise `_KimiOAuthDenied` immediately (fail-fast, no 15-minute spin); `expires_in` elapsed → `_KimiOAuthExpired`; Ctrl-C → exit 130. On success writes `access_token` as `KIMI_FOR_CODING_API_KEY` and (when Moonshot ships one) `refresh_token` as `KIMI_REFRESH_TOKEN` in `.env`. Device headers include `X-Msh-Platform: butterfly`, `X-Msh-Version`, `X-Msh-Device-Id = sha256(hostname+platform.platform())[:16]` (stable, non-PII). Every exit path (success, failure, skip, Ctrl-C) prints a three-line agent-hint block via `_print_kimi_agent_hint` wrapped in `try/finally` so LLM agents reading stdout learn how to request a key instead of retrying OAuth in a loop. Verification ping: `_kimi_ping` sends `max_tokens=1` to `kimi-for-coding` at `api.kimi.com/coding/v1/chat/completions` with `User-Agent: claude-code/0.1.0`.

## Anthropic — v2.0.31 ModelSpec wiring

`AnthropicProvider.complete` consults `get_model_spec(model)` on every call. Three helper groups drive the request shape:

**Thinking** — `_apply_thinking_kwargs(kwargs, spec, ...)` mutates `kwargs` per the resolved mode. Resolution order (first non-None wins): `spec.thinking_mode` → `"enabled"`; `spec.thinking_display` → `"summarized"`; `spec.thinking_effort` → caller's `thinking_effort` kwarg; `spec.thinking_budget_tokens` → caller's `thinking_budget`; `spec.interleaved_thinking_beta` → class attr `_thinking_uses_betas`. The adaptive branch emits `thinking={type: "adaptive", display: ...}` + `output_config={effort: ...}` only when both `_supports_adaptive_thinking=True` (class attr) AND `mode == "adaptive"` — `KimiAnthropicProvider` pins the class attr to `False`, so Kimi-served models always take the legacy branch even with `mode=adaptive` in YAML.

**Cache strategies** — `_apply_cache_strategy(spec, ...)` dispatches to `_apply_cache_single`, `_apply_cache_two_plus_two`, or `_apply_cache_auto` based on `spec.cache_strategy` (default `single` when absent/unknown). All three return `(api_messages, system_param, extra_kwargs)`; only `auto` populates `extra_kwargs` (with a top-level `cache_control`).

- `single` — one breakpoint on tail user/assistant (via `_find_cache_breakpoint`) + one on system prefix (via `_build_system_param`). Exact pre-YAML shape when `spec is None` — regression test `test_legacy_cache_shape_regression` pins this.
- `two_plus_two` — `_two_plus_two_anchor_indices` picks the last two `user`/`assistant` indices (skipping `tool` rows); `_to_api_messages` stamps both with cache_control; `_build_system_param(..., mark_dynamic=True)` stamps both system prefix and dynamic body. Falls back to `single` when < 2 qualifying messages or `_supports_cache_control=False`.
- `auto` — `_build_system_param(..., cache_control=None)` suppresses per-block breakpoints; `extra_kwargs["cache_control"]` carries a top-level `{type: "ephemeral", ttl?: "5m|1h"}`. `_supports_cache_control=False` clients (Kimi) fall back to `single`.

**Cache TTL** — `_resolve_cache_control(spec)` emits `{type: "ephemeral"}` when no `ttl` pinned (legacy-exact) or `{type: "ephemeral", ttl: "5m"}` when `spec.cache_ttl` is set. Applied uniformly by every strategy that emits a cache_control dict.

Sentinel protocol: `_build_system_param` accepts `cache_control` by sentinel (`_DEFAULT_CACHE_CONTROL_SENTINEL`) so legacy callers that passed no control block get the legacy default, while the strategy-driven path passes explicit dicts (or explicit `None` for `auto`).

## Built-in tools — Codex + OpenAI Responses (v2.0.31)

Provider-native built-in tools are executed server-side by OpenAI's Responses API. The provider pushes the declaration through on request; the server replies with progress events + a final `output_item.done` the provider captures; Butterfly replays the captured item on the next turn so the server can resume its state.

### Declaration

`butterfly/core/tool.py::Tool` accepts `builtin_dict: dict | None` at construction time. When non-empty, `to_builtin_dict()` returns a copy; `is_builtin` returns True. The provider's `_format_tool_for_request(tool)` calls `to_builtin_dict()` first — a non-None return is spliced into the `tools=[]` list verbatim, NOT wrapped as `type: "function"`. Regular function tools fall through to the existing `_tool_to_responses_api` shaping.

Three toolhub stubs ship (`toolhub/web_search/`, `toolhub/file_search/`, `toolhub/code_interpreter/`); each executor class carries a module-level `builtin_dict` attr that `tool_engine/loader.py::_load_builtin_dict` reads best-effort. Calling `execute()` on any of them raises `NotImplementedError` with a configuration-hint message.

### Built-in tool types

Declared (request side) — `_BUILTIN_TOOL_TYPES`:

```
web_search | file_search | code_interpreter |
image_generation | mcp | computer_use_preview
```

Captured (response side, `output_item.done`) — `_BUILTIN_ITEM_TYPES`:

```
web_search_call | file_search_call | code_interpreter_call |
image_generation_call | mcp_call | mcp_list_tools | computer_call
```

### 6-tuple stream return

`_parse_sse_stream` (codex) and `_stream` (openai_responses) return `(text, tool_calls, usage, reasoning_items, builtin_items, builtin_progress)`. The first three are unchanged; the last three are new.

- `builtin_items` — one entry per `output_item.done` whose `type` is in `_BUILTIN_ITEM_TYPES`. Replayed in next turn's `input[]` by `_convert_assistant` when the assistant message carries the block. Strips `None`-valued keys and `_`-prefixed keys on replay so the server doesn't see schema-invalid nulls (e.g. `file_search_call.queries` must be a list). For `code_interpreter_call`, the provider accumulates `response.code_interpreter_call.code.delta` chunks in a `code_deltas: dict[item_id, list[str]]` and, when `output_item.done` doesn't include a `code` field, splices the assembled text onto the captured item.
- `builtin_progress` — classified via `_classify_builtin_event(etype)` (shared regex between codex + openai_responses). Each entry is `{"tool_type": str, "phase": str, "payload": dict}`. `response.mcp_call_arguments.delta` is special-cased to `("mcp_call", "arguments_delta")` since it uses underscore-separated prefix rather than dotted.

Phase vocabulary: `{in_progress, searching, completed, interpreting, generating, partial_image, failed, code_delta, code_done, arguments_delta}`. Failures are represented as `phase == "failed"`, not raised.

### Drain API

Two provider methods expose the new channels:

- `consume_extra_blocks() -> list[dict]` — drains `reasoning_items + builtin_items`, clears both. Agent loop appends them verbatim to the last assistant `Message.content` so next-turn's `_convert_assistant` re-emits them in `input[]`.
- `consume_builtin_tool_events() -> list[dict]` — drains `builtin_progress`. Intended for UI / telemetry; not replayed. Callers that don't render progress can ignore.

Both are one-shot drains.

## Adding a New Provider

1. Create `providers/<name>.py` implementing `Provider.complete()`.
2. Raise errors from `butterfly.llm_engine.errors` for recognized failures.
3. Populate `TokenUsage.reasoning_tokens` when the backend reports it.
4. If the backend has server-side state that must round-trip (e.g. encrypted reasoning), override `consume_extra_blocks()`.
5. Register in `registry.py`.
6. Document in this file.
