# Review Findings for `origin/main..HEAD`

Reviewed range: `origin/main..HEAD`

Scope: local commits not yet pushed to `origin`, excluding unrelated uncommitted changes in the worktree.

Status legend: ✅ Fixed | ⏳ Deferred

---

## 1. ✅ High: `/api/sessions/{session_id}/history` currently crashes on every request

Files:
- `ui/web/app.py:30`

Details:
- `get_history()` called `read_session_status(system_dir)` but only `write_session_status` was imported.

Resolution:
- Added `read_session_status` to the import on line 30: `from nutshell.session_engine.session_status import read_session_status, write_session_status`.
- Also added early `404` when `system_dir` does not exist.
- Committed in `b142785` + `f6644f1`.

---

## 2. ✅ High: tab re-attach still loses replies that finished while the tab was hidden

Files:
- `ui/web/frontend/src/main.ts`
- `ui/web/app.py`
- `ui/web/frontend/src/api.ts`

Details:
- `visibilitychange` only reconnected SSE offsets without rendering new completed events.

Resolution:
- History endpoint (`app.py`) now accepts `?context_since=N` and returns only events from that byte offset onward.
- `api.ts` `getHistory(id, contextSince)` passes the param.
- `main.ts` tracks `lastRenderedContextOffset`; `visibilitychange` fetches `getHistory(id, lastRenderedContextOffset)`, appends the delta events, then calls `reconnectWithOffsets`.
- Committed in `f6644f1`.

---

## 3. ✅ High: session switching is race-prone and can paint or stream the wrong session

Files:
- `ui/web/frontend/src/main.ts`

Details:
- Late-resolving history/task/config fetches from a previous `attachSession` call could overwrite the current session's state.

Resolution:
- Introduced monotonic `attachVersion` counter in `main.ts`. Each `attachSession(id)` increments it and captures `const version = ++attachVersion`. After every `await`, the function bails if `attachVersion !== version`.
- `sseConn.attach()` is the last side effect, only called after all awaits pass the freshness guard.
- Periodic task/HUD refreshes also capture and check `store.currentSessionId` before applying results.
- Committed in `f6644f1`.

---

## 4. ✅ High: debounced message batching can send text to the wrong session or drop it entirely

Files:
- `ui/web/frontend/src/components/chat.ts`

Details:
- `flushPendingMessages()` resolved the destination from `store.currentSessionId` at flush time — switching sessions mid-debounce could redirect the message.

Resolution:
- Added `pendingSessionId: string | null` captured at enqueue time in `sendMessage()`.
- `flushPendingMessages()` uses the captured `pendingSessionId`, not `store.currentSessionId`.
- If `sendMessage()` is called with a different session while a batch is pending, the old batch is flushed immediately to its original session before starting a new batch.
- Committed in `f6644f1`.

---

## 5. ✅ Medium: focus-triggered SSE reconnect can reuse offsets from an old session after a quick switch

Files:
- `ui/web/frontend/src/main.ts`
- `ui/web/frontend/src/sse.ts`

Details:
- `visibilitychange` awaited `getHistory(id)` then called `reconnectWithOffsets()` without rechecking the active session.

Resolution:
- `reconnectWithOffsets(sessionId, ctx, evt)` now takes an explicit `sessionId` and no-ops if it doesn't match the currently attached session (`this.sessionId !== sessionId`).
- `visibilitychange` checks `store.currentSessionId !== id` after the `await` before calling reconnect.
- Committed in `f6644f1`.

---

## 6. ✅ Medium: multiple `thinking` blocks from one turn are silently truncated in live delivery

Files:
- `nutshell/runtime/ipc.py`

Details:
- All thinking blocks from the same turn shared the ID `thinking:{ts}`, so only the first survived `BridgeSession.iter_events()` dedup.

Resolution:
- Changed to per-block IDs: `thinking:{ts}:{idx}` where `idx` is incremented for each thinking block found across all assistant messages in the turn.
- Committed in `f6644f1`.

---

## 7. ⏳ Medium: markdown rendering is a stored XSS sink

Files:
- `ui/web/frontend/src/markdown.ts`
- `ui/web/frontend/src/components/chat.ts`

Details:
- `renderMarkdown()` returns raw `marked.parse(text)` assigned to `innerHTML` with no sanitization.

Deferred:
- This is a real risk but low urgency for a local/personal-use tool. Fix requires adding `DOMPurify` as a dependency and wrapping all `innerHTML` assignments: `DOMPurify.sanitize(marked.parse(text))`. Deferred to a dedicated security pass.

---

## 8. ⏳ Medium: HUD adds avoidable O(file size) and subprocess overhead on every poll

Files:
- `ui/web/app.py` (HUD endpoint)
- `ui/web/frontend/src/main.ts`

Details:
- Every 10s HUD poll runs `git diff --shortstat` subprocess and reads the full `context.jsonl` to find the latest `turn.usage`.

Deferred:
- Recommended fix: cache git root at app startup; persist latest usage to `status.json` at turn end; reduce polling interval or go event-driven. Deferred — HUD is best-effort and overhead is acceptable for single-user local use.

---

## 9. ✅ Medium: renaming a task to an existing name silently overwrites the target task

Files:
- `ui/web/app.py`

Details:
- `set_tasks()` deleted the old file and saved to the new name without checking if the target already existed.

Resolution:
- Added conflict check in `set_tasks()`: when `previous_name != name`, calls `load_card(tasks_dir, name)` first. If a card already exists at the target name, returns `HTTP 409 Conflict` and leaves both files untouched.
- Committed in `f6644f1`.

---

## 10. ⏳ Medium: streaming markdown rendering does full re-parse and DOM replacement on every chunk

Files:
- `ui/web/frontend/src/components/chat.ts`

Details:
- `partial_text` handler re-parses the full accumulated text via `renderMarkdown()` and replaces `body.innerHTML` on every chunk.

Deferred:
- Recommended fix: render plain escaped text during streaming; only do a full markdown parse on the final `agent` event. Deferred — acceptable for typical output lengths; will revisit if jank is reported on very long generations.

---

## 11. ✅ High: ordinary SSE reconnects replay from stale offsets forever

Files:
- `ui/web/frontend/src/sse.ts`
- `ui/web/app.py`

Details:
- `contextSince`/`eventsSince` were never advanced as events arrived, so every reconnect replayed from the original attach point. Runtime events (`tool`, `status`, `partial_text`, etc.) have no stable `id` and are not deduplicated, causing visible re-renders on every reconnect.

Resolution:
- `_sse_format()` in `app.py` now embeds `_ctx` and `_evt` byte offsets into every event's JSON payload (alongside the event data, using a shallow copy so the original dict is not mutated).
- The SSE streaming generator passes `ctx=_ctx, evt=_evt` from `async_iter_events` to `_sse_format`.
- `sse.ts` reads `data._ctx` and `data._evt` on every received event and updates `this.contextSince`/`this.eventsSince` with `Math.max(...)`. On reconnect, `_connect()` uses these advanced offsets, not the original attach point.
- Committed in `f6644f1`.
