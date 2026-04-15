# Agent Entity — Implementation

## Usage

```bash
butterfly new --entity agent
butterfly chat --entity agent "build a small CLI"
```

## Configuration (config.yaml)

- `model`: gpt-5.4
- `provider`: codex-oauth
- `fallback_model`: kimi-for-coding
- `fallback_provider`: kimi-coding-plan
- `max_iterations`: 1000
- `params.thinking`: true

## What It Ships

- **Prompts**: system.md (operating rules), task.md (autonomous wake-up), env.md (session layout)
- **Tools**: bash.json, skill.json, web_search.json
- **Skills**: creator-mode (self-extending at runtime)
