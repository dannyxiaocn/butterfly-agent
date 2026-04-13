# LLM Engine — Implementation

## Registry

`registry.py` maps provider keys to concrete classes:

| Key | Class | Auth |
|-----|-------|------|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` | `OPENAI_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` or `KIMI_API_KEY` |
| `codex-oauth` | `CodexProvider` | `codex login` → `~/.codex/auth.json` |

Usage:
```python
from nutshell.llm_engine.registry import resolve_provider
provider = resolve_provider("codex-oauth")
```

## Thinking Support

| Provider | `thinking` | `thinking_budget` | `thinking_effort` |
|----------|-----------|-------------------|-------------------|
| `anthropic` | ✓ | ✓ | ignored |
| `kimi-coding-plan` | ✓ | ignored | ignored |
| `openai` | ignored | ignored | ignored |
| `codex-oauth` | ✓ | ignored | ✓ |

## How Sessions Switch Providers

`Session._load_session_capabilities()` reads `core/config.yaml` for `provider` and `model`, then calls `resolve_provider()`. Changes take effect on the next activation.
