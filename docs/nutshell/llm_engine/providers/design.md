# Providers — Design

Each file adapts one external model API to the common `Provider.complete()` interface. All vendor-specific translation (request format, auth, streaming, usage accounting) is isolated here.

## Design Rule

The rest of the runtime is provider-agnostic. These adapters normalize:
- Text output
- Tool calls (name, arguments, result)
- Token usage accounting
- Thinking/reasoning blocks
