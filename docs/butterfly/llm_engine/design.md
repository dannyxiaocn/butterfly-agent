# LLM Engine — Design

The LLM engine provides **provider adapters** that normalize different vendor APIs to the common `Provider.complete()` interface.

## Responsibilities

- Adapt external model APIs (Anthropic, OpenAI, Kimi, Codex) to a uniform interface
- Registry maps string keys (from `config.yaml`) to concrete provider classes
- Handle vendor-specific features (thinking, streaming, prompt caching) transparently
- Expose a single catalog (`models.yaml`) so per-model knobs reach providers + UI + HUD without each layer hardcoding its own table

## Design Constraints

- `core.Agent` only talks to the `Provider` interface — never to vendor SDKs directly
- Provider resolution is lazy (import on first use) to keep startup fast
- Each provider is a self-contained file that can be added/removed independently
- The catalog is data, not code. Adding a model is a YAML edit; opting a model into adaptive thinking / a different cache ladder is a YAML edit.

## ModelSpec-driven configuration (v2.0.31)

`butterfly/llm_engine/models.yaml` is the authoritative source of per-model parameters. `model_catalog.py::ModelSpec` is the dataclass that carries them at runtime. Providers read a spec per `complete()` call and resolve request parameters from spec → caller kwarg → class default (first non-None wins). Absent YAML keys land as `None` on the spec; providers fall through to legacy behavior.

Seven optional fields are recognised (all apply to Anthropic today; other providers ignore them):

| Field | Values |
|---|---|
| `thinking_mode` | `enabled` \| `adaptive` |
| `thinking_effort` | `low` \| `medium` \| `high` \| `xhigh` \| `max` |
| `thinking_display` | `summarized` \| `omitted` |
| `thinking_budget_tokens` | int (only consulted when `mode=enabled`) |
| `interleaved_thinking_beta` | bool (sends the dated betas header when true) |
| `cache_strategy` | `single` \| `two_plus_two` \| `auto` |
| `cache_ttl` | `5m` \| `1h` |

The catalog also drives the web UI (`butterfly/service/models_service.py` → `/api/models` → `ui/web/frontend/src/types.ts::ModelCatalogEntry`) and the HUD's context-% denominator (`get_max_context_tokens`).

## Adaptive vs enabled thinking

Two shapes hit the Anthropic wire:

- **Adaptive** (model 4.6+): `thinking: {type: "adaptive", display: "summarized"}` paired with `output_config: {effort: "high"}`. The server picks the budget at serve time. Engaged when `thinking_mode=adaptive` and the provider class advertises `_supports_adaptive_thinking = True` (Anthropic proper). `KimiAnthropicProvider` pins this to `False` so Kimi-served models take the legacy branch even with `mode=adaptive` in YAML.
- **Legacy `enabled`** (older models + Kimi): `thinking: {type: "enabled", budget_tokens: N}` via the `anthropic-beta` header namespace (Anthropic proper), or `extra_body: {thinking: {type: "enabled"}}` via regular Messages namespace (Kimi's Anthropic-compat surface, which doesn't route through `betas`). `max_tokens` is bumped to `max(self.max_tokens, budget + 1000)` so the response has room to think.

Resolution order inside `_apply_thinking_kwargs`: `mode/display/effort/budget/use_betas ← spec → caller kwarg → class default`. Any field absent in YAML preserves the pre-2.0.31 payload exactly.

## Cache strategy ladder

`butterfly/llm_engine/providers/anthropic.py::_apply_cache_strategy` dispatches to one of three helpers:

- **`single`** — one `cache_control` breakpoint on the tail user/assistant message + one on the system prefix (legacy). When no spec is provided, `_resolve_cache_control` emits the exact `{type: "ephemeral"}` shape existing tests pin against (no `ttl` key leaks through).
- **`two_plus_two`** — two breakpoints on the last two non-tool user/assistant messages + two on `system_prefix` and `system_dynamic` (when both non-empty). Consumes all four cache_control slots Anthropic allows per request, targeting better hit rates on long sessions. Degrades to `single` when fewer than 2 qualifying messages exist or the provider class reports `_supports_cache_control=False`. `_two_plus_two_anchor_indices` walks backwards over `messages`, skipping `role == "tool"` rows (they render as user-shaped on the wire but aren't cache anchors).
- **`auto`** — Anthropic Feb-2026 server-managed caching. Per-block breakpoints suppressed; a single top-level `cache_control` kwarg is added to the request. Relies on a per-account entitlement; sessions without it should pin `two_plus_two` explicitly.

`_build_system_param` distinguishes "caller didn't pass cache_control" (sentinel → stamp legacy default) from "caller explicitly wants no breakpoint" (the `auto` path's `None`) so legacy call sites keep their exact payload.

`cache_ttl` is stamped as `{ttl: "5m"}` or `{ttl: "1h"}` on every cache_control dict the strategy emits. Spec-less calls never emit `ttl`.

## SSE watchdog (Codex + OpenAI Responses)

Both `providers/codex.py` and `providers/openai_responses.py` wrap their event iterator's `__anext__()` in `asyncio.wait_for(..., timeout=_CHUNK_IDLE_TIMEOUT)` where `_CHUNK_IDLE_TIMEOUT = 45.0`. On timeout they raise `ProviderTimeoutError("...SSE stalled — no bytes/event for 45s...")`. Without the watchdog a mid-stream stall would wait out the full `_DEFAULT_READ_TIMEOUT = 600.0` (10 min). Tests monkey-patch the constant to sub-second values.

Shape difference: Codex measures per-**chunk** (bytes from `resp.aiter_bytes()`) because SSE blocks span multiple bytes; OpenAI Responses measures per-**event** (iterator items from the SDK's typed stream). Same 45-s threshold on both.

## Built-in vs function tools

Two tool kinds reach a provider's `tools=[]` list:

- **Function tools** — Butterfly owns the executor. The provider receives a `type: "function"` schema; the server replies with `function_call` items; the agent loop runs `Tool.execute()` locally and feeds the result back.
- **Provider-native built-in tools** — the provider (currently Codex / OpenAI Responses) executes the tool server-side. Butterfly only declares it at request time via the raw `{"type": "web_search"}` (or similar) dict. Local `execute()` raises `NotImplementedError`.

The line is carried by `butterfly/core/tool.py::Tool`:

- `builtin_dict: dict | None` ctor kwarg — stores the raw provider spec verbatim.
- `to_builtin_dict()` — providers call this first when formatting `tools=[]`; non-None return ⇒ splice verbatim (not wrap as `function`).
- `is_builtin` property — boolean shortcut.

`tool_engine/loader.py::_load_builtin_dict` reads a module-level `builtin_dict` class attr off each toolhub executor; when present, the loaded `Tool` carries it through. Three ship today: `toolhub/web_search/`, `toolhub/file_search/`, `toolhub/code_interpreter/` — each `execute()` raises `NotImplementedError`.

SSE events for built-in tools are classified by `_classify_builtin_event(etype)` (both codex and openai_responses) into `(tool_type, phase)` pairs. Item captures on `response.output_item.done` land in `builtin_items`; progress events land in `builtin_progress`. See `providers/impl.md` for the phase vocabulary.

## `consume_extra_blocks()` + `consume_builtin_tool_events()` responsibilities

Both are `Provider` methods overridden by providers that carry server-side state across turns.

- `consume_extra_blocks()` — drains items the provider needs to **re-echo on the next turn** so the server can resume its state. Codex + OpenAI Responses drain both `reasoning_items` (chain-of-thought retention) and `builtin_items` (provider-native tool calls validated by id + encrypted_content). The agent loop appends them to the last assistant `Message.content`; the provider's `_convert_assistant` replays them verbatim in the next request's `input[]`. Kimi's OpenAI-compat variant drains a single `reasoning_content` block (see `providers/impl.md`). Function-tool providers (Anthropic, OpenAI Chat Completions) return an empty list.
- `consume_builtin_tool_events()` — drains **transient progress events** for UI / telemetry only. Not replayed. Each entry is `{"tool_type", "phase", "payload"}`. Failures are represented as `phase == "failed"`, never raised. Callers that don't render built-in-tool progress can simply ignore the drain.

Both are one-shot drains — calling twice without an intervening `complete()` returns an empty list.
