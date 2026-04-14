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
- **Codex**: Responses-API over SSE against the ChatGPT-OAuth endpoint. Default model `gpt-5-codex`. Sends `prompt_cache_key` + `session_id` header for server-side prompt caching. Reasoning items are replayed across turns.

## Reasoning continuation (Codex + OpenAI Responses)

When `thinking=True`, the provider sends `include=["reasoning.encrypted_content"]` so the server returns encrypted reasoning items on each turn. The provider captures these items during the stream and surfaces them via `consume_extra_blocks()`, which the agent loop appends to the assistant `Message.content`. On the next turn `_convert_assistant` emits each reasoning block back into the request `input` verbatim, and the server resumes its chain-of-thought without re-thinking.

The agent loop is tolerant of providers that don't implement `consume_extra_blocks` (legacy or mock providers) — it falls back to `lambda: []`.

## Error taxonomy

`butterfly/llm_engine/errors.py` exposes a normalized taxonomy providers should raise from recognizable error conditions:

| Error | When |
|-------|------|
| `AuthError` | 401/403, expired/revoked refresh token |
| `RateLimitError` | 429 with optional `retry_after` |
| `ContextWindowExceededError` | server-reported context-length stop |
| `BadRequestError` | 400 (malformed request) |
| `ServerError` | 5xx (transient) |
| `ProviderError` | base — fallback for unclassified failures |

Codex parses `response.failed` events into this taxonomy; HTTP non-200 statuses are routed through `_raise_from_status`.

## TokenUsage

`butterfly.core.types.TokenUsage` has five fields: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`. `input_tokens` is *non-cached* input across all providers (OpenAI's `prompt_tokens` has cached subtracted out so the math `input + cache_read = total input` holds uniformly).

## Adding a New Provider

1. Create `providers/<name>.py` implementing `Provider.complete()`.
2. Raise errors from `butterfly.llm_engine.errors` for recognized failures.
3. Populate `TokenUsage.reasoning_tokens` when the backend reports it.
4. If the backend has server-side state that must round-trip (e.g. encrypted reasoning), override `consume_extra_blocks()`.
5. Register in `registry.py`.
6. Document in this file.
