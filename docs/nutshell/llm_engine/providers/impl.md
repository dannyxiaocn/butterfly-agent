# Providers — Implementation

## Files

| File | Purpose |
|------|---------|
| `_common.py` | Shared helpers for parsing JSON tool arguments |
| `anthropic.py` | Anthropic Messages API, prompt-cache support, streamed thinking |
| `openai_api.py` | OpenAI Chat Completions API |
| `kimi.py` | Kimi for Coding — Anthropic-compatible variant with `extra_body` thinking |
| `codex.py` | ChatGPT OAuth-backed Codex Responses API over SSE |

## Provider Notes

- **Anthropic**: thinking mode uses beta Messages namespace (`client.beta.messages`), not `client.messages`
- **Kimi**: same adapter shape as Anthropic but thinking via `extra_body`, no beta namespace
- **Codex**: uses `reasoning.effort` instead of `thinking_budget`

## Adding a New Provider

1. Create `providers/<name>.py` implementing `Provider.complete()`
2. Register in `registry.py`
3. Document thinking support in `docs/nutshell/llm_engine/impl.md`
