# LLM Engine — Implementation

## Registry

`registry.py` maps provider keys to concrete classes:

| Key | Class | Auth |
|-----|-------|------|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` (Chat Completions) | `OPENAI_API_KEY` |
| `openai-responses` | `OpenAIResponsesProvider` | `OPENAI_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` or `KIMI_API_KEY` |
| `codex-oauth` | `CodexProvider` | `codex login` → `~/.codex/auth.json` |

Usage:
```python
from butterfly.llm_engine.registry import resolve_provider
provider = resolve_provider("codex-oauth")
```

Pick `openai-responses` for o-series / gpt-5 reasoning models; it uses the Responses API (reasoning retention + encrypted_content replay). Pick `openai` for gpt-4o / gpt-4.1 / legacy Chat Completions.

## Thinking Support

| Provider | `thinking` | `thinking_budget` | `thinking_effort` |
|----------|-----------|-------------------|-------------------|
| `anthropic` | ✓ | ✓ | ignored |
| `kimi-coding-plan` | ✓ | ignored | ignored |
| `openai` (Chat Completions) | ✓ (reasoning models only) | ignored | ✓ → `reasoning_effort` |
| `openai-responses` | ✓ | ignored | ✓ → `reasoning.effort` |
| `codex-oauth` | ✓ | ignored | ✓ |

## Error Taxonomy

`butterfly/llm_engine/errors.py` exposes `ProviderError`, `AuthError`, `RateLimitError`, `ContextWindowExceededError`, `BadRequestError`, `ServerError`, `TimeoutError`. Providers raise these from recognized failure classes so the session loop can handle them uniformly.

## How Sessions Switch Providers

`Session._load_session_capabilities()` reads `core/config.yaml` for `provider` and `model`, then calls `resolve_provider()`. Changes take effect on the next activation.

## Cross-turn Reasoning Continuation

Providers whose backend retains server-side reasoning state (Codex, OpenAI Responses) override `Provider.consume_extra_blocks()`. After `complete()` the agent loop drains those blocks into the assistant `Message.content`, and on the next turn the provider re-echoes them in its request `input`, preserving chain-of-thought across tool calls without re-thinking. See `providers/impl.md` for details.
