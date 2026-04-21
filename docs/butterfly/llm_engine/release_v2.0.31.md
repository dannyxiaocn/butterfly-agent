# v2.0.31 — LLM engine overhaul (PR TBD, branch `v2.0.31-llm-engine`)

Headline: Five-phase rework of the LLM engine. `models.yaml` grows seven optional per-model knobs that reach the Anthropic / web UI / service layers; Anthropic learns adaptive thinking and three cache-strategy ladders; Codex + OpenAI Responses gain a per-chunk SSE watchdog and both return a 6-tuple with built-in-tool items + progress; Kimi CLI auth is rebuilt around the Moonshot KLIP-14 device flow; and three new toolhub stubs (`web_search`, `file_search`, `code_interpreter`) plus a `Tool.builtin_dict` plumbing path let provider-native built-in tools declare themselves without ever running locally. **1018/1018 tests pass.**

## Scope summary

This is a provider-layer release. `core.Agent` and the session engine are untouched. Every change lives inside `butterfly/llm_engine/`, `butterfly/core/tool.py`, `butterfly/tool_engine/loader.py`, `toolhub/`, `butterfly/service/models_service.py`, `ui/cli/login.py`, and `ui/web/frontend/src/types.ts`. The release is **opt-in by default**: any agent config from v2.0.29 or earlier keeps running unchanged — absent YAML keys fall through to the exact pre-2.0.31 request shapes.

---

## Phase 1A — `models.yaml` schema extension

`butterfly/llm_engine/models.yaml` + `butterfly/llm_engine/model_catalog.py` picked up seven optional per-model fields, all defaulted to `None` so an unmodified catalog preserves legacy behavior:

| Field | Values | Consumed by |
|---|---|---|
| `thinking_mode` | `enabled` \| `adaptive` | Anthropic `_apply_thinking_kwargs` |
| `thinking_effort` | `low` \| `medium` \| `high` \| `xhigh` \| `max` | Anthropic adaptive branch |
| `thinking_display` | `summarized` \| `omitted` | Anthropic adaptive branch |
| `thinking_budget_tokens` | int | Anthropic legacy branch |
| `interleaved_thinking_beta` | bool | Anthropic `betas=[...]` header |
| `cache_strategy` | `single` \| `two_plus_two` \| `auto` | Anthropic `_apply_cache_strategy` |
| `cache_ttl` | `5m` \| `1h` | Anthropic `_resolve_cache_control` |

The shipped Anthropic entry carries these defaults (all inside the YAML, not invented by provider code):

```yaml
- name: claude-sonnet-4-6
  thinking_mode: adaptive
  thinking_effort: high
  thinking_display: summarized
  thinking_budget_tokens: 8000          # only used when thinking_mode=enabled
  interleaved_thinking_beta: false      # 4.6+ GA'd interleaved thinking
  cache_strategy: two_plus_two          # opencode-proven default
  cache_ttl: 5m
```

Non-Anthropic providers leave every knob unset — they are parsed to `None` on `ModelSpec` and non-Anthropic providers don't read them.

The web UI sees the same catalog. `butterfly/service/models_service.py::_models_for()` serializes all seven fields (nulls pass through as JSON `null`) and `ui/web/frontend/src/types.ts::ModelCatalogEntry` declares them so the config editor can render them for providers that expose them.

Migration: nothing to do. A v2.0.29 `models.yaml` round-trips through the 2.0.31 loader unchanged because every new field is `.get(key)` defaulted to `None`.

---

## Phase 1B — Kimi model name cleanup

Replaced 37 occurrences of `kimi-k2.5 / kimi-k2-turbo-preview / kimi-k2-0711-preview` with `kimi-for-coding` — the live rolling alias that `https://api.kimi.com/coding/v1/` serves. Touched: `ui/cli/login.py` (verification ping), test fixtures across `tests/butterfly/llm_engine/` and `tests/ui/cli/`, and the one-line reference in `docs/butterfly/llm_engine/providers/impl.md`.

No behavior change — `kimi-k2-turbo-preview` still resolved to the same model server-side, but the new name stops pointing at a deprecated label.

---

## Phase 2 — Anthropic adaptive thinking + cache strategies

`butterfly/llm_engine/providers/anthropic.py` consults `ModelSpec` (via `get_model_spec(model)`) on every `complete()` call. New module helpers:

| Helper | Role |
|---|---|
| `_apply_thinking_kwargs` | Resolves `mode/display/effort/budget/use_betas` (first non-None wins; caller → spec → default) and mutates `kwargs` accordingly. |
| `_apply_cache_strategy` | Dispatches to one of three strategy helpers below; unknown strings fall through to `single`. |
| `_apply_cache_single` | Legacy — 1 breakpoint on tail user/assistant + 1 on system prefix. |
| `_apply_cache_two_plus_two` | 2 breakpoints on last 2 user/assistant messages + 2 on system prefix and dynamic. Falls back to `single` when < 2 qualifying messages or `supports_cache=False`. |
| `_apply_cache_auto` | Anthropic Feb-2026 server-managed caching — top-level `cache_control` kwarg, no per-block breakpoints. |
| `_resolve_cache_control` | Emits `{type: ephemeral}` when no `cache_ttl` is pinned, or `{type: ephemeral, ttl: <value>}` when set. Preserves exact-shape for legacy tests. |

Thinking paths:

- **Adaptive** (`mode=adaptive` + `_supports_adaptive_thinking=True`): `thinking={type:"adaptive", display:...}` + `output_config={effort:...}`, no `budget_tokens`, `max_tokens` stays at `self.max_tokens`.
- **Legacy `enabled`** (everything else): `thinking={type:"enabled", budget_tokens:N}` when class uses betas header (Anthropic proper), or `extra_body={"thinking":{"type":"enabled"}}` when it doesn't (Kimi). `max_tokens` is bumped to `max(self.max_tokens, budget+1000)` so the response has room to think.

`KimiAnthropicProvider` pins `_supports_adaptive_thinking = False` at the class level — Moonshot's Anthropic-compat surface hasn't shipped adaptive thinking, so it always takes the legacy branch even if someone sets `thinking_mode=adaptive` on a Kimi model in YAML.

Cache strategies (brief):

- `single` — legacy, unchanged shape. When `spec is None` (no YAML entry) the `cache_control` dict is still `{type: ephemeral}` — no `ttl` key leaks through, which a regression test pins.
- `two_plus_two` — consumes 2 of Anthropic's 4 per-request cache_control slots on the last 2 user/assistant rows (skipping tool rows); the other 2 go on system prefix + dynamic body (when both non-empty). Anchor selection: `_two_plus_two_anchor_indices` walks from the end, takes the last 2 rows with `role ∈ {user, assistant}`. With < 2 anchors, degrades to `single`.
- `auto` — top-level `cache_control` kwarg via `_apply_cache_auto`; per-block breakpoints are intentionally suppressed (prefix + dynamic system blocks ship uncached, server decides). Requires `_supports_cache_control=True`; Kimi still gets `single`.

Legacy safety: the `_build_system_param` sentinel (`_DEFAULT_CACHE_CONTROL_SENTINEL`) lets the three strategy helpers distinguish "caller didn't pass cache_control" (stamp the legacy default dict) from "caller explicitly wants no breakpoint" (the `auto` strategy case). Any call site that still passes positional args through the old shape lands on the sentinel → legacy shape → old tests pass.

Migration: nothing to do. A YAML without any of the Phase 2 fields produces the same request payload as v2.0.29.

---

## Phase 3 — Codex auth + SSE watchdog

Two independent changes:

**Auth** (`butterfly/llm_engine/providers/codex.py::_get_auth_async`): now prefers the persisted `account_id` field when present. Fresh-token flow (`_refresh_access_token_async`) re-extracts `chatgpt_account_id` from the newly-minted JWT and stores it on the `tokens` dict, so `_get_auth_async` can read it directly instead of re-parsing the token every call. Legacy `auth.json` (no `account_id`; migrated from `~/.codex/auth.json`) still works via the JWT-extraction fallback — the preferred-then-fallback ordering means no user action is needed.

**Watchdog** (`codex.py` + `butterfly/llm_engine/providers/openai_responses.py`): both SSE readers now wrap `iterator.__anext__()` in `asyncio.wait_for(..., timeout=_CHUNK_IDLE_TIMEOUT)` where `_CHUNK_IDLE_TIMEOUT = 45.0`. On timeout they raise `ProviderTimeoutError` with a descriptive message instead of waiting out the full `_DEFAULT_READ_TIMEOUT = 600.0`. Tests can monkey-patch the constant to sub-second values.

Migration: nothing. Users never notice unless the server stalls — which was previously a 10-minute hang, now a 45-second fail-loud.

---

## Phase 4 — Kimi OAuth device-flow in CLI

`ui/cli/login.py::butterfly kimi login` is rewritten to auto-select between three paths (legacy getpass is preserved for scripted provisioning):

1. **Fast-path** — if `KIMI_FOR_CODING_API_KEY` already resolves via `os.environ` or `.env`, ping-verify and exit 0 (unchanged from v2.0.29).
2. **OAuth** — otherwise, or when forced via `--oauth`, run Moonshot's KLIP-14 device-authorization flow:
   - POST `https://auth.kimi.com/api/oauth/device_authorization` with `client_id=17e5f671-d194-4dfb-9706-5516cb48c098` (the public kimi-cli client id; butterfly reuses it since it's a CLI of the same shape).
   - Show the user the `verification_uri_complete` URL and the `user_code`.
   - Poll `https://auth.kimi.com/api/oauth/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`.
   - Persist the returned `access_token` as `KIMI_FOR_CODING_API_KEY` in `.env`; if Moonshot also returned a `refresh_token`, persist it as `KIMI_REFRESH_TOKEN` next to it.
3. **Legacy getpass** — `--api-key` forces the old interactive paste path.

Polling behavior:
- `authorization_pending` → wait `interval` seconds, retry.
- `slow_down` → double `interval` (cap 15 s), retry.
- `access_denied` / `expired_token` → raise `_KimiOAuthDenied` immediately (fail-fast, no 15-minute spin).
- `expires_in` elapsed → raise `_KimiOAuthExpired`.
- Ctrl-C anywhere → exit 130.

Stable device identity: `_stable_device_id()` returns `sha256(hostname + platform.platform())[:16]` — stable across invocations on the same machine, reveals nothing sensitive to Moonshot. Device headers include `X-Msh-Platform: butterfly`, `X-Msh-Version: <butterfly.__version__>`, `X-Msh-Device-Name: <hostname>`, `X-Msh-Device-Id: <stable_id>`, etc.

Every exit path (success, failure, skip, Ctrl-C) prints a three-line agent-hint block via `_print_kimi_agent_hint` wrapped in `try/finally`. The block teaches LLM agents reading stdout to ask their human for the key or to run the command themselves instead of retrying OAuth in a loop. Format (stable + grep-testable):

```
Tip: If you are an agent: please ask your human for a Kimi Code API key or have them run this command to OAuth-login.
   If you already have the key: set it in the .env file as:  KIMI_FOR_CODING_API_KEY=sk-...
   The provider reads KIMI_FOR_CODING_API_KEY from environment or from .env in the current directory.
```

New flags: `--oauth` (force OAuth), `--api-key` (force getpass; mutually exclusive with `--oauth`). `--key`, `--no-verify`, `--env-file` are preserved from v2.0.29.

Migration: users with a working `KIMI_FOR_CODING_API_KEY` are unaffected (fast-path skip + verify). Users re-running `butterfly kimi login` with no key set transition to OAuth automatically.

---

## Phase 5 — Codex built-in tool SSE events + toolhub stubs

### Provider-side (Codex + OpenAI Responses)

`_parse_sse_stream` (codex) and `_stream` (openai_responses) now return a **6-tuple**:

```python
(text, tool_calls, usage, reasoning_items, builtin_items, builtin_progress)
```

- `reasoning_items` — unchanged from v2.0.20 (captured `reasoning` output items, replayed in next turn's `input[]`).
- `builtin_items` — captured `response.output_item.done` items whose `type` is in:
  ```
  web_search_call | file_search_call | code_interpreter_call |
  image_generation_call | mcp_call | mcp_list_tools | computer_call
  ```
  Round-tripped via `_convert_assistant` in the next turn's `input[]`, verbatim (except `None`-valued and `_`-prefixed keys are stripped so the server doesn't see schema-invalid nulls on e.g. `file_search_call.queries`).
- `builtin_progress` — progress events classified via new `_classify_builtin_event(etype)` regex; each entry is `{"tool_type": "...", "phase": "...", "payload": {...}}`. Phase vocabulary includes `in_progress`, `searching`, `completed`, `interpreting`, `generating`, `partial_image`, `failed`, `code_delta`, `code_done`, `arguments_delta` (the last maps `response.mcp_call_arguments.delta` since it uses underscore prefix instead of dotted).

For `code_interpreter_call.code.delta`, the provider accumulates chunks in a `code_deltas: dict[str | None, list[str]]` keyed by `item_id` and, when the matching `output_item.done` arrives without a `code` field, splices the assembled text onto the captured item as `code` so replay carries the program that actually ran.

New `Provider` methods (opt-in; default no-ops):

- `consume_builtin_tool_events()` — drains `builtin_progress`. Called by the agent loop for UI / telemetry.
- `consume_extra_blocks()` — extended to fold `builtin_items` on top of `reasoning_items`, both cleared after.

`response.output_text.delta` routing is unchanged (still flows to `text_parts` by default), with the `current_output_item_type == "reasoning"` redirect to thinking preserved.

### Built-in tool declaration — `Tool.builtin_dict`

`butterfly/core/tool.py::Tool` gained three additions:

| Addition | Role |
|---|---|
| `builtin_dict: dict \| None` ctor kwarg | Stores the raw provider-native tool spec verbatim (e.g. `{"type": "web_search"}`). |
| `to_builtin_dict() -> dict \| None` | Returns a copy of `builtin_dict` for providers to splice into their `tools=[]` list. |
| `is_builtin: bool` property | True iff `builtin_dict` was set. |

`@tool(...)` decorator gained the matching `builtin_dict=` kwarg so existing function-tool call sites keep working unchanged.

Provider integration: `codex.py::_format_tool_for_request` + `openai_responses.py::_format_tool_for_request` call `tool.to_builtin_dict()` first; non-None return → splice the dict verbatim (NOT wrapped as `type: "function"`). Regular function tools fall through to the existing `_tool_to_responses_api` shaping.

### Toolhub stubs

Three new toolhub entries — each is `tool.json` + `executor.py`, where `execute()` raises `NotImplementedError("…provider-native built-in tool…")`:

- `toolhub/web_search/` — `{"type": "web_search"}`. Distinct from `web_search_brave` / `web_search_tavily` (which are API-key-based local executors).
- `toolhub/file_search/` — `{"type": "file_search"}`. Requires `vector_store_ids` at call time; the executor spec is just the base type and the provider accepts additional fields the config editor surfaces.
- `toolhub/code_interpreter/` — `{"type": "code_interpreter"}`.

Each executor class carries a module-level `builtin_dict` class attr; `butterfly/tool_engine/loader.py::_load_builtin_dict` reads it best-effort. If present, the loader threads the dict into the `Tool(...)` ctor; if absent, regular function-tool semantics apply.

Three matching dispatch arms were added in `loader.py::_create_executor` (`web_search`, `file_search`, `code_interpreter`), each instantiating the executor class and wrapping its `execute()` for the standard `NotImplementedError`-path.

`image_generation` and `computer_use_preview` are **parsed and replayed** by the provider (their item types are in `_BUILTIN_ITEM_TYPES`, their progress events in `_BUILTIN_EVENT_RE`), but no toolhub stub ships — configuring them as an agent's tool requires hand-editing `tool.json` + `executor.py` for now.

Migration: nothing unless you want to opt in. Add `web_search` (or the other two) to your agent's `tools.md`; when the active provider is `codex-oauth` or `openai-responses`, the agent can invoke it and the server executes it. On any non-Responses provider, calling the tool raises `NotImplementedError`.

---

## Known limitations

- **`computer_use_preview` has no toolhub stub**. Parsed and replayed at the provider layer; declaring it to an agent requires hand-authoring `toolhub/computer_use/{tool.json,executor.py}`. Same story for `image_generation`.
- **Kimi's Anthropic-compat surface (`KimiAnthropicProvider`) does not participate in adaptive thinking**. Pinned to `_supports_adaptive_thinking = False` — `thinking_mode=adaptive` on a Kimi-served model falls back to legacy `extra_body={"thinking":{"type":"enabled"}}`. This is intentional: Moonshot hasn't shipped the adaptive request shape.
- **`cache_strategy=auto` depends on a backend feature Anthropic announced Feb 2026**. If the server rejects top-level `cache_control`, the provider doesn't transparently degrade — callers should pin `cache_strategy: two_plus_two` until their account has the entitlement.
- **`two_plus_two` with < 2 non-tool messages degrades to `single`**. A fresh 1-turn session hits this path; after two turns the full strategy kicks in.
- **Provider-native built-in tool progress events are surfaced via `consume_builtin_tool_events()`** but no UI cell is wired in this release — callers that want to render `web_search.searching` etc. need to poll the drain themselves.
- **Codex SSE watchdog is per-chunk (bytes) while OpenAI Responses is per-event (SDK items)**. They timeout on the same 45-second threshold but via slightly different shapes; tests monkey-patch the shared constant name.

---

## Defaults note

The shipped `butterfly/llm_engine/models.yaml` Anthropic entry uses the Phase-1A-recommended defaults verbatim: `thinking_mode: adaptive`, `thinking_effort: high`, `thinking_display: summarized`, `thinking_budget_tokens: 8000`, `interleaved_thinking_beta: false`, `cache_strategy: two_plus_two`, `cache_ttl: 5m`. If a future operator wants to A/B a different ladder (e.g. `cache_strategy: single` to diff cache-hit rates against 2.0.29), the YAML is the single knob — no code change.

---

## Tests

1018/1018 pass. New test files:

- `tests/butterfly/llm_engine/test_model_catalog.py` — all seven new fields round-trip through `ModelSpec`; absent keys → `None`.
- `tests/butterfly/llm_engine/test_codex_builtin_tools.py` — 6-tuple return, built-in item capture on `output_item.done`, `code_delta` accumulation, `consume_builtin_tool_events` drain semantics, `consume_extra_blocks` fold.
- `tests/butterfly/llm_engine/test_openai_responses_builtin_tools.py` — same coverage for the Responses SDK path.
- `tests/ui/cli/test_kimi_login.py` — fast-path, OAuth happy-path, `slow_down` doubling, `access_denied` fail-fast, Ctrl-C exit 130, agent-hint emission on every exit path.

Updated tests across `test_anthropic_provider.py` (adaptive + three cache strategies, legacy-shape regression pin), `test_codex_provider.py` (6-tuple), `test_kimi_openai_provider*.py` + `test_kimi_provider.py` (Kimi-adaptive-pin + model name cleanup), `test_openai_responses_provider*.py` (6-tuple + watchdog), `test_thinking_config.py` (ModelSpec wiring), `test_v204_deferred_bugs.py` (Phase 1B name cleanup), `test_login.py` (Kimi verification model name cleanup).

---

## Review log (pending PR)

Not yet opened. This release notes file is authored from the worktree at branch `v2.0.31-llm-engine` with all five phases applied locally; 1018/1018 tests pass at `HEAD`.
