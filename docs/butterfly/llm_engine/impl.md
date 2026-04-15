# LLM Engine — Implementation

## Registry

`registry.py` maps provider keys to concrete classes:

| Key | Class | Auth |
|-----|-------|------|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` (Chat Completions) | `OPENAI_API_KEY` |
| `openai-responses` | `OpenAIResponsesProvider` | `OPENAI_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` |
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

## Related open-source projects

Projects worth reading or borrowing from when evolving this engine. **litellm** (BerriAI/litellm) and **kosong** (MoonshotAI/kimi-cli) are already tracked upstream and intentionally omitted here.

- **openai/codex (codex-rs)** — https://github.com/openai/codex
  - OpenAI's official Rust CLI coding agent; `codex-rs/core` is the reference implementation for driving the Responses API over SSE.
  - Borrow: `codex-rs/core/src/client.rs` is the closest mirror to our `providers/codex.py`. Read it for (a) the `stream_responses_api` / `map_response_stream` pattern — an mpsc-channel-backed `ResponseStream` that collects `OutputItemDone` into a `Vec` while forwarding events, exactly the shape our `consume_extra_blocks()` replay needs; (b) `include: ["reasoning.encrypted_content"]` wiring around a `Reasoning` struct gated on `supports_reasoning_summaries` — a clean spot to copy for gating `thinking_effort`; (c) the session-scoped `PendingUnauthorizedRetry` + `handle_unauthorized()` → `AuthManager` token-refresh loop, richer than our single-shot 401 retry; (d) the WebSocket transport with per-session `force_http_fallback()` latch, if persistent connections ever matter. `client_common.rs` separates stable session state from per-turn parameters in a `Prompt` struct — a nice discipline.
  - Verdict: reference to read and selectively port; do not depend on the Rust crate.

- **NousResearch/hermes-agent** — https://github.com/NousResearch/hermes-agent
  - Python self-improving agent with a centralized provider router covering ~19 first-class providers plus custom endpoints.
  - Borrow: three files map 1:1 onto our pain points. (1) `agent/error_classifier.py` defines a `FailoverReason` enum with 13 categories and a priority-ordered pipeline (provider-specific patterns → status codes → structured `error.code` → message regex → transport heuristics → session-aware heuristics → fallback); each classification carries boolean retry hints (`retryable`, `should_compress`, `should_rotate_credential`, `should_fallback`). Our 6-class taxonomy is coarser — their `thinking_signature`, `long_context_tier`, and `auth_permanent` vs transient `auth` splits are genuinely useful. (2) `agent/prompt_caching.py` implements a "system_and_3" breakpoint budget — 1 marker on system, 3 rolling on latest non-system messages — with message-format-aware placement (tool vs string vs list content). We cache a static prefix only today; adopting the rolling-tail pattern would improve hit rates on multi-turn loops. (3) `agent/retry_utils.py` + `rate_limit_tracker.py` pair with the classifier for adaptive retry. `smart_model_routing.py` + `model_metadata.py` encode "same model, different context limits by provider" — something we'll need eventually.
  - Verdict: codebase to copy patterns from (license-permitting).

- **sst/opencode** — https://github.com/sst/opencode
  - TypeScript multi-provider coding agent built on Vercel AI SDK + Models.dev, 75+ providers.
  - Borrow: the `ProviderTransform` namespace is the most polished "normalize messages to survive every provider's quirks" code I've seen. `normalizeMessages()` encodes empirical rules (Anthropic/Bedrock reject empty content, Claude requires alphanumeric tool IDs, Mistral demands 9-char IDs and synthetic "Done" assistant messages between tool and user messages); `applyCaching()` implements the "first 2 system + last 2 messages" cache-breakpoint heuristic and routes Anthropic/Bedrock to message-level `cache_control` vs OpenAI-compat `providerOptions.cache_control`; `schema()` recursively strips `$schema` / `definitions` / `$defs` from tool JSON schemas and strips `required` on nested objects for Anthropic — exactly the tool-schema translation layer we lack today. `smallOptions()` for compaction calls is a nice bonus. Auth subsystem unifies API keys, OAuth flows, env vars, and well-known enterprise configs in one model.
  - Verdict: reference to read; port transform rules as a `butterfly/llm_engine/transforms.py` utility module.

- **Aider-AI/aider** — https://github.com/Aider-AI/aider
  - Mature coding assistant built on LiteLLM with its own `ModelSettings` catalog layered on top.
  - Borrow: `aider/models.py` is the best "model catalog" reference in the ecosystem. `ModelSettings` fields worth copying into our per-model config: `edit_format`, `weak_model_name`/`editor_model_name` (≈ our `fallback_model`), `use_temperature`, `streaming`, `cache_control`, `caches_by_default`, `reasoning_tag`, `remove_reasoning`, `accepts_settings` (whitelist of params the model tolerates), `extra_params`. `ModelInfoManager` caches LiteLLM's model-catalog JSON for 24h and layers user JSON5 overrides — a pragmatic shape for a future butterfly model registry. The `caches_by_default` flag is something we don't have and would simplify the "inject cache_control?" decision. `aider/llm.py` lazy-loads LiteLLM to defer a ~1.5s import — a micro-pattern worth stealing for CLI cold-start. Note: `aider/sendchat.py` is *not* the retry file (only does message-role alternation repair); retry lives inside LiteLLM itself.
  - Verdict: reference to read for model-catalog shape; do not adopt LiteLLM indirectly.

- **continuedev/continue (core/llm)** — https://github.com/continuedev/continue
  - Open-source IDE coding assistant with a TypeScript `core/llm/` layer fronting many providers.
  - Borrow: `core/llm/openaiTypeConverters.ts` normalizes every provider's message/tool-call format to OpenAI shape — the inverse of what we do today (Anthropic-native internally, translate outward). Worth reading to decide whether our canonical message type should be OpenAI-shaped or Anthropic-shaped. `core/llm/toolSupport.ts` is a declarative table of which providers support tool calling and in what dialect — we currently scatter this knowledge across providers; a single `butterfly/llm_engine/capabilities.py` would be cleaner. `countTokens.ts` + `getAdjustedTokenCount.ts` + `tiktokenWorkerPool.mjs` show a production approach to token counting (off-thread Tiktoken) for when context-budget pruning becomes real. `autodetect.ts` / `fetchModels.ts` solve the "given `model=gpt-5.4`, which provider?" problem.
  - Verdict: reference to read; port a `toolSupport`-style capability table.

- **pydantic/pydantic-ai** — https://github.com/pydantic/pydantic-ai
  - Pydantic team's typed agent framework with per-provider Model classes in `pydantic_ai_slim/pydantic_ai/models/`.
  - Borrow: `models/fallback.py` implements a `FallbackModel` that wraps an ordered list and catches configurable exception types (default `ModelAPIError`, extendable to `RateLimitError` etc.). On any failure it tries the next model and, if all fail, raises a `FallbackExceptionGroup` (Python 3.11 exception groups) containing every underlying error. Cleaner than our current `fallback_model` / `fallback_provider` pair in `config.yaml` — and it auto-detects whether a user-supplied handler is a response validator (first param typed `ModelResponse`) or an exception handler. `models/wrapper.py` + `instrumented.py` show the decorator-stack pattern for layering retries / tracing / fallback without inheritance. Per-provider files (`anthropic.py`, `openai.py`, `gemini.py`, …) share one `Model` ABC — worth comparing signatures to our `Provider` ABC.
  - Verdict: reference to read; port the `FallbackModel` wrapper pattern.

- **openai/openai-agents-python** — https://github.com/openai/openai-agents-python
  - OpenAI's official Python agent SDK; explicitly provider-agnostic (Responses + Chat Completions + 100+ other LLMs).
  - Borrow: *the* reference for how to shuttle reasoning items across turns. The SDK's `Items` abstraction (`openai.github.io/openai-agents-python/ref/items/`) converts streamed reasoning items into replayable input items — directly analogous to our `consume_extra_blocks()` → `encrypted_content` flow. Read `src/agents/models/` for the Responses-vs-Chat-Completions split and the `include=["reasoning.encrypted_content"]` handling for ZDR / `store=False` scenarios (tracker issues #919 and #2063 capture real edge cases: ZDR still storing reasoning items; reasoning broken with `store=False`). Handoffs ("agents as tools") are orthogonal to llm_engine, but the streaming event types (`RunItemStreamEvent`, `RawResponsesStreamEvent`) are a clean reference for normalizing SSE into a typed event stream — a generalization of our current raw-delta callbacks.
  - Verdict: authoritative reference; align our reasoning-replay contract with this SDK's shape so interop stays cheap.

- **simonw/llm** — https://github.com/simonw/llm
  - Single-binary multi-provider CLI with a pluggable model system registered via the `register_models(register, model_aliases)` pluggy hook.
  - Borrow: mostly architectural. The plugin-hook registration model is how our `registry.py` could eventually expose provider plugins without shipping every backend in-tree — useful once community providers land. `llm.ModelError` as a base exception class that plugin authors extend is a lighter-weight alternative to our full taxonomy for third-party providers. Async models, tools, streaming, and structured schemas are all documented as plugin capabilities, so capability advertisement is baked into the protocol.
  - Verdict: reference to read for plugin-system shape.

- **traceloop/openllmetry** — https://github.com/traceloop/openllmetry
  - OpenTelemetry-based observability for LLM apps; its semantic conventions were upstreamed into OpenTelemetry's `gen_ai` namespace.
  - Borrow: a *vocabulary*, not a dependency. If our `TokenUsage` adopts OTel `gen_ai.*` attribute names (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.request.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.reasoning_tokens`), any downstream tracer (Langfuse, Arize, Honeycomb, Datadog) works for free. The upstream spec lives in OpenTelemetry's `semantic-conventions` repo under `docs/gen-ai/`.
  - Verdict: rename TokenUsage fields to the OTel shape as a cheap compatibility win.

### Deliberately skipped

- **langchain-ai/langchain** — provider abstraction is too entangled with the LCEL runnable hierarchy; shape mismatch with our small async ABC. Hermes' classifier already covers the one part worth reading (`langchain_core.exceptions`).
- **instructor-ai/instructor** — structured output only; orthogonal to llm_engine.
- **sgl-project/sglang** / **vllm-project/vllm** — inference *servers*, not clients. Our `openai_api` provider already talks to their OpenAI-compat endpoints.
- **openai/swarm** — deprecated in favor of openai-agents-python (covered above).
- **openclaw** family (win4r/ClawTeam-OpenClaw, BlockRunAI/ClawRouter) — orchestrators / routing-policy boxes, not provider abstractions. ClawRouter's cost-aware sub-1ms routing is interesting; revisit only if butterfly ever needs cost-aware routing.
