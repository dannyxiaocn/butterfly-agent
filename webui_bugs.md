# Web UI Bug Report

Reviewed commits: `dc99d84` → `f6644f1` (5 commits, web UI SSE / history / streaming fixes).

All 4 bugs resolved.

---

## Bug 1 — ✅ HIGH: `lastRenderedContextOffset` not updated from SSE events → duplicate messages on tab focus

**Files**: `ui/web/frontend/src/main.ts`

Resolution (commit `f6920fc`):
- SSE callback now reads `(event as any)._ctx` and advances `lastRenderedContextOffset = Math.max(last, _ctx)` on every event.
- `_ctx` is stripped from the event before passing to handler (see Bug 3 fix), so the advancement is done before the strip.

---

## Bug 2 — ✅ MEDIUM: `reconnectWithOffsets` overwrites offsets unconditionally — no `Math.max` guard

**Files**: `ui/web/frontend/src/sse.ts`

Resolution:
- `reconnectWithOffsets` now uses `Math.max` for both offsets:
  ```typescript
  this.contextSince = Math.max(this.contextSince, contextSince);
  this.eventsSince  = Math.max(this.eventsSince,  eventsSince);
  ```
- SSE's already-advanced offsets can never be rolled back by a stale history response.

---

## Bug 3 — ✅ LOW: `_ctx`/`_evt` meta-fields leaked into `DisplayEvent` objects

**Files**: `ui/web/frontend/src/sse.ts`

Resolution:
- In the SSE event listener, after extracting offsets, meta-fields are stripped before passing to the handler:
  ```typescript
  const { _ctx, _evt, ...cleanData } = data as any;
  this.handler?.(cleanData as DisplayEvent);
  ```
- Offset advancement uses the raw `_ctx`/`_evt` values before the strip, so no information is lost.

---

## Bug 4 — ✅ LOW: `thinking` events from `for_history=True` path have no `id` field

**Files**: `nutshell/runtime/ipc.py`

Resolution:
- Removed the `if not for_history:` guard around the thinking `id` assignment.
- Thinking events always carry `id = f"thinking:{ts}:{thinking_idx}"` regardless of history vs SSE mode.
- This enables client-side `seenIds` dedup for thinking blocks returned by the history endpoint, preventing repeat renders on `visibilitychange`.
