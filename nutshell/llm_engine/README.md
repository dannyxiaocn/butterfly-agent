# llm_engine

LLM provider implementations. `registry.py` maps provider keys → classes.

| Key | Class | File | Auth |
|-----|-------|------|------|
| `anthropic` | `AnthropicProvider` | `anthropic.py` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` | `openai_api.py` | `OPENAI_API_KEY`; optional `OPENAI_BASE_URL` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `kimi.py` | `KIMI_FOR_CODING_API_KEY` (fallback: `KIMI_API_KEY`) |
| `codex-oauth` | `CodexProvider` | `codex.py` | `~/.codex/auth.json` (run `codex login`) |

Switch provider per-session via `sessions/<id>/core/params.json`:
```json
{"provider": "openai", "model": "gpt-4o"}
```

`providers/_common.py` — shared `_parse_json_args()` used by `openai_api` and `codex`.

---

## Thinking / Extended Reasoning

Enable via `params.json`:
```json
{"thinking": true, "thinking_budget": 8000, "thinking_effort": "high"}
```

| Provider | Supported | Mechanism | `thinking_budget` | `thinking_effort` |
|----------|-----------|-----------|-------------------|-------------------|
| `anthropic` | ✅ | `betas: ["interleaved-thinking-2025-05-14"]` + `thinking: {type: "enabled", budget_tokens: N}` | ✅ used | ignored |
| `kimi-coding-plan` | ✅ | `extra_body: {"thinking": {"type": "enabled"}}` — no budget_tokens | ignored | ignored |
| `openai` | ❌ | Ignored — standard GPT models have no explicit thinking toggle | ignored | ignored |
| `codex-oauth` | ✅ | `reasoning: {effort: "<level>"}` in request body | ignored | ✅ used: `none\|minimal\|low\|medium\|high\|xhigh` (default: `high`) |

---

## Provider Notes

### AnthropicProvider

- Uses Anthropic Messages API (`/v1/messages`).
- Prompt caching: static prefix (`system.md` + `session.md`) sent with `cache_control: ephemeral`; last stable turn also cached when history exists.
- Thinking uses the `interleaved-thinking-2025-05-14` beta; `thinking_budget` controls `budget_tokens`.
- Streaming: text via `text_delta`, thinking via `thinking_delta`.

### KimiForCodingProvider (`kimi-coding-plan`)

Thin wrapper over `AnthropicProvider` pointing at `https://api.kimi.com/coding/`.

**Key differences from standard Anthropic:**
- `_supports_cache_control = False` — Kimi's endpoint does not accept `cache_control` blocks.
- `_thinking_uses_betas = False` — thinking is enabled via `extra_body: {"thinking": {"type": "enabled"}}`, **not** via Anthropic's `betas` header. This matches the mechanism used in the official [kimi-cli](https://github.com/MoonshotAI/kimi-cli).
- No `budget_tokens` in thinking config — Kimi controls thinking depth via `reasoning_effort` (legacy param, not exposed in nutshell; `extra_body.thinking.type` is the primary control per kimi-cli docs).
- Thinking content returns as Anthropic-format `thinking` blocks (not OpenAI-style `reasoning_content` — that's the `api.moonshot.ai/v1` endpoint which is a different API).
- Multi-turn thinking: thinking blocks are preserved in conversation history as-is (Anthropic format), so reasoning context carries forward correctly.

### OpenAIProvider

- Uses OpenAI Chat Completions API (`/v1/chat/completions`).
- Works with any OpenAI-compatible endpoint via `OPENAI_BASE_URL`.
- `thinking` param is accepted but ignored (`_supports_thinking = False`); standard GPT models have no equivalent feature. For o1/o3 reasoning models, thinking is always-on and not controllable via this flag.
- Tool calls: standard `function` type with `arguments` as JSON string.

### CodexProvider (`codex-oauth`)

Uses the **OpenAI Responses API** (`https://chatgpt.com/backend-api/codex/responses`) over SSE, authenticated via ChatGPT Plus OAuth. This is **not** the standard OpenAI API.

**Setup:** Run `codex login` (from the official [openai/codex](https://github.com/openai/codex) CLI). Credentials are stored at `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`).

**Key differences from standard OpenAI Chat Completions:**

| Feature | Chat Completions | Codex Responses API |
|---------|-----------------|---------------------|
| System prompt | `role: "system"` message | `instructions` field (top-level) |
| Message format | `{role, content: string}` | `ResponseItem` — tagged union: `message`, `function_call`, `function_call_output` |
| User content | `content: string` | `content: [{type: "input_text", text: "..."}]` |
| Assistant content | `content: string` | `content: [{type: "output_text", text: "..."}]` |
| Function args | object | **JSON string** (must be `json.dumps`-ed) |
| Tool call ID field | `tool_call_id` (in tool result) | `call_id` (in both function_call and function_call_output) |
| Streaming events | `choices[].delta` chunks | Named SSE events (`response.output_text.delta`, etc.) |
| Thinking content | N/A | `response.reasoning_text.delta` events (forwarded to `on_text_chunk`) |
| `max_tokens` | supported | **not supported** — model decides output length |
| Response storage | N/A | `store: false` (set explicitly to avoid server-side storage) |

**SSE event types handled:**

| Event | Action |
|-------|--------|
| `response.output_text.delta` | Append to text, forward to `on_text_chunk` |
| `response.reasoning_text.delta` | Forward to `on_text_chunk` (thinking content) |
| `response.output_item.added` | Register new function_call in `tc_map` |
| `response.function_call_arguments.delta` | Accumulate arguments by `call_id` |
| `response.output_item.done` | Finalize function_call arguments |
| `response.completed` / `response.done` | Extract token usage |
| `error` / `response.failed` | Raise `RuntimeError` |

**Token refresh:** Access tokens expire; `_refresh_access_token()` posts JSON to `https://auth.openai.com/oauth/token` with `{"grant_type": "refresh_token", "refresh_token": "...", "client_id": "..."}` — official Codex uses JSON body (not form-urlencoded).

**Not implemented (vs official Codex CLI):**
- WebSocket transport (we use HTTP/SSE only)
- `x-codex-turn-state` sticky routing header
- `reasoning.encrypted_content` for cross-request reasoning state preservation
- `response.reasoning_summary_text.delta` (summary of reasoning, separate from full reasoning text)
- Rate limit header parsing (`x-ratelimit-*`)
