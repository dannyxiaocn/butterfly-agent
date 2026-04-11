# Agent Entity — Implementation

## Usage

```bash
nutshell new --entity agent
nutshell chat --entity agent "build a small CLI"
```

## Configuration (agent.yaml)

- `model`: gpt-5.4
- `provider`: codex-oauth
- `fallback_model`: kimi-for-coding
- `fallback_provider`: kimi-coding-plan
- `max_iterations`: 20
- `params.thinking`: true

## What It Ships

- **Prompts**: system.md (operating rules), heartbeat.md (autonomous wake-up), session.md (session layout)
- **Tools**: bash.json, skill.json, web_search.json
- **Skills**: creator-mode (self-extending at runtime)
