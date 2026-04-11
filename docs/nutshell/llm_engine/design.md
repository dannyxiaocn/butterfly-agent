# LLM Engine — Design

The LLM engine provides **provider adapters** that normalize different vendor APIs to the common `Provider.complete()` interface.

## Responsibilities

- Adapt external model APIs (Anthropic, OpenAI, Kimi, Codex) to a uniform interface
- Registry maps string keys (from `params.json` / `agent.yaml`) to concrete provider classes
- Handle vendor-specific features (thinking, streaming, prompt caching) transparently

## Design Constraints

- `core.Agent` only talks to the `Provider` interface — never to vendor SDKs directly
- Provider resolution is lazy (import on first use) to keep startup fast
- Each provider is a self-contained file that can be added/removed independently
