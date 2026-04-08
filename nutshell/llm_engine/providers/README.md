# `nutshell/llm_engine/providers`

Each file in this directory adapts one external model API to the common `Provider.complete(...)` interface.

## Files

- `_common.py`: shared helpers for parsing JSON tool arguments.
- `anthropic.py`: Anthropic Messages API, prompt-cache support, optional streamed thinking.
- `openai_api.py`: OpenAI Chat Completions API.
- `kimi.py`: Kimi for Coding, implemented as an Anthropic-compatible variant.
- `codex.py`: ChatGPT OAuth-backed Codex Responses API over SSE.

## How To Use This Part

Add or modify code here when:

- introducing a new vendor
- changing request/response translation
- extending streamed tool-call or token-usage behavior

Then register the provider in [registry.py](/Users/xiaobocheng/agent_core/nutshell/nutshell/llm_engine/registry.py).

## How It Contributes To The Whole System

This directory is the boundary between the Nutshell runtime and external model APIs. Everything above it remains provider-agnostic because these adapters normalize text output, tool calls, and usage accounting.

