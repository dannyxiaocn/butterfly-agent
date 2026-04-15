# Providers — Implementation

## Files

| File | Purpose |
|------|---------|
| `_common.py` | Shared helpers for parsing JSON tool arguments |
| `anthropic.py` | Anthropic Messages API, prompt-cache support, streamed thinking |
| `openai_api.py` | OpenAI Chat Completions API (legacy + non-reasoning models) |
| `openai_responses.py` | OpenAI Responses API — reasoning models (o-series / gpt-5) |
| `kimi.py` | Kimi for Coding — Anthropic-compatible; `extra_body` thinking |
| `codex.py` | ChatGPT OAuth Codex Responses API over SSE |

## Provider Notes

- **Anthropic**: thinking mode uses beta Messages namespace (`client.beta.messages`), not `client.messages`.
- **Kimi**: same adapter shape as Anthropic but thinking via `extra_body={"thinking":{"type":"enabled"}}`, no beta namespace. Base URL overridable via `KIMI_BASE_URL`.
- **OpenAI (Chat Completions)**: model-family-aware param scrubber (`_apply_model_specific_params`) routes reasoning models (`o*`, `gpt-5*`, `gpt-oss*`) to `max_completion_tokens` + `reasoning_effort`; legacy models keep `max_tokens`.
- **OpenAI Responses**: Responses API path — flat tool schema, `instructions` field separate from `input`, `max_output_tokens`, `reasoning={"effort","summary":"auto"}`, `include=["reasoning.encrypted_content"]` when thinking. Replays reasoning items on subsequent turns (see below).
- **Codex**: Responses-API over SSE against the ChatGPT-OAuth endpoint. Default model `gpt-5.4` (ChatGPT-OAuth rejects `gpt-5-codex` even though codex-rs defaults to it). The "use my default" signal is an explicit allow-list (`_is_codex_compatible_model` — `gpt-*`, `o\d+-*`, `codex-*`, `ft:gpt-*`), so Kimi/Gemini/typos no longer slip through to a 400. Token refresh is async (httpx) so it doesn't block the event loop, uses a module-level `asyncio.Lock` to serialize concurrent refreshes, and writes `~/.codex/auth.json` with `0o600`. Sends `max_output_tokens`, `prompt_cache_key`, and `session_id` header; **no cache_read_tokens have been observed in practice on the ChatGPT-OAuth backend** as of 2026-04-15, so the caching fields are best-effort. SSE parser caps buffer growth at 1 MiB. Stream error taxonomy: codes match an explicit enum (`context_length_exceeded`, `rate_limit_exceeded`, `invalid_api_key`, …) plus narrow message phrases — no loose substring matching. Reasoning items are replayed across turns with `summary: null` coerced to `[]`.

## Cross-provider fallback sanitization

When the primary provider is reasoning-aware (Codex, OpenAI Responses) and emits a `reasoning` block captured into the assistant `Message.content`, a later fallback to a non-reasoning provider (Anthropic / Kimi / OpenAI Chat Completions) used to send that opaque block verbatim and 400. Now:

- `anthropic._sanitize_content_for_anthropic` strips any block type not on Anthropic's allow-list; a fully-filtered assistant message collapses to a single `[continued]` text block.
- `openai_api._build_messages` substitutes the same `[continued]` placeholder when a filtered assistant message has no text and no tool_calls.

This keeps the default entity config (`codex-oauth` primary, `kimi-coding-plan` fallback) reliable.

## Agent fallback scope

`Agent.run` only switches to the fallback provider on `ProviderError` (butterfly taxonomy) and `OSError` (transport / DNS / TLS). `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, and plain Python errors (`TypeError`, `ValueError`, `AssertionError`, …) propagate — they indicate either a deliberate cancellation or a logic bug that the fallback can't fix. The switch is logged via the `butterfly.core.agent` logger (exception type only; we do not log `str(exc)` since provider error messages can contain request bodies or secrets).

## Reasoning continuation (Codex + OpenAI Responses)

When `thinking=True`, the provider sends `include=["reasoning.encrypted_content"]` so the server returns encrypted reasoning items on each turn. The provider captures these items during the stream and surfaces them via `consume_extra_blocks()`, which the agent loop appends to the assistant `Message.content`. On the next turn `_convert_assistant` emits each reasoning block back into the request `input` verbatim, and the server resumes its chain-of-thought without re-thinking.

Every concrete provider inherits `Provider.consume_extra_blocks()` from the ABC (default: empty list), so the agent loop calls it directly.

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

## Login helpers (v2.0.7)

Two CLI helpers live at `ui/cli/login.py` and are wired into the top-level `butterfly` entry point:

- **`butterfly codex login`** — checks for the `codex` CLI on `PATH`, shells out to `codex login` (ChatGPT OAuth), then reads `~/.codex/auth.json` to confirm `access_token` / `refresh_token` are present and that `_extract_account_id` succeeds. If the CLI is missing, it prints install + re-verify instructions (`npm install -g @openai/codex`, then `butterfly codex login --skip-cli`). Flags: `--skip-cli` (verify only), `--no-verify` (run CLI only).
- **`butterfly kimi login`** — prompts via `getpass.getpass` (hidden input) for a Kimi For Coding key (offering to reuse `KIMI_FOR_CODING_API_KEY` from the env if already set), upserts it into `<repo>/.env` with `0600` permissions (preserving other lines), then pings the provider via a 16-token `complete()` call against `kimi-k2-turbo-preview`. Flags: `--env-file PATH`, `--key KEY` (non-interactive), `--no-verify` (skip ping).

Both write exactly where the runtime already reads: `runtime/env.py::load_dotenv` picks up `.env` at repo root for Kimi, and `codex.py::_read_auth` reads `~/.codex/auth.json` for Codex. Neither helper introduces a new config source.

Default Moonshot dashboard URL hardcoded to `https://platform.moonshot.ai/console/api-keys`; override by editing `ui/cli/login.py` if the dashboard moves.

## Adding a New Provider

1. Create `providers/<name>.py` implementing `Provider.complete()`.
2. Raise errors from `butterfly.llm_engine.errors` for recognized failures.
3. Populate `TokenUsage.reasoning_tokens` when the backend reports it.
4. If the backend has server-side state that must round-trip (e.g. encrypted reasoning), override `consume_extra_blocks()`.
5. Register in `registry.py`.
6. Document in this file.
