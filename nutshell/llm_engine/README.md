# `nutshell/llm_engine`

Provider adapters live here. `registry.py` maps provider keys in `params.json` and `agent.yaml` to concrete classes.

## Providers

| Key | Class | Auth |
| --- | --- | --- |
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` | `OPENAI_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` or `KIMI_API_KEY` |
| `codex-oauth` | `CodexProvider` | `codex login` -> `~/.codex/auth.json` |

`openai` support depends on the optional `openai` package extra.

## How To Use It

Switch provider per session:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6"
}
```

Or load one directly:

```python
from nutshell.llm_engine.registry import resolve_provider

provider = resolve_provider("codex-oauth")
```

## How It Contributes To The Whole System

- `core.Agent` talks only to the `Provider` interface.
- `session_engine.Session` reloads `provider`, `model`, fallback settings, and thinking flags from `core/params.json`.
- Entities choose defaults, but this directory implements the actual network protocol for each vendor.

## Thinking Support

| Provider | `thinking` | `thinking_budget` | `thinking_effort` |
| --- | --- | --- | --- |
| `anthropic` | supported | used | ignored |
| `kimi-coding-plan` | supported | ignored | ignored |
| `openai` | ignored | ignored | ignored |
| `codex-oauth` | supported | ignored | used |

Provider-specific notes live in [providers/README.md](/Users/xiaobocheng/agent_core/nutshell/nutshell/llm_engine/providers/README.md).

