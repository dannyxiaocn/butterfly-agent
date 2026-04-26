# Frontend testing — TODO (Phase 11)

Phase 9 left the Python suite aligned with the refactored event model,
but did not stand up a frontend test runner. This file is the handoff
so a follow-up phase can add unit coverage for the TypeScript modules
under `src/`.

## Why this wasn't done in Phase 9

The frontend today has zero tests. Adding a runner is scope beyond
"purge obsolete tests + add load-bearing new ones" — it would require
a new dev dependency, a test config, a test script entry in
`package.json`, and a CI runbook update. Deferring to Phase 11 keeps
Phase 9 self-contained.

## Suggested setup (vitest)

`package.json` diff:

```jsonc
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^1.6.0",
    "jsdom": "^24.0.0",
    "@testing-library/dom": "^10.0.0"
  }
}
```

`vitest.config.ts` (sibling to `vite.config.ts`):

```ts
import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.ts'],
  },
});
```

## Initial test cases — high priority

Order by load-bearingness (each protects a specific invariant from
DESIGN.md §8):

1. **`reducer.test.ts`** — `reduce()` purity + `reduceMany()` idempotence.
   - Given event list E, `reduceMany(E); reduceMany(E)` = same store as
     `reduceMany(E)` alone (no double-count).
   - Every event type in `types.ts` EVT dispatches without crashing
     (smoke-drive every branch).
   - `AGENT_TOOL_CALL` followed by `AGENT_TOOL_RESULT` with matching
     `tool_use_id` upgrades the card in-place (cards count stays same,
     `resultEvent` set). Mirrors the Python-side `_reduce_cards`
     assertion in `tests/butterfly/integration/test_e2e_cli_to_web.py`.
   - `USER_INTERRUPT` with `text=null` produces no card (bare ⚡).
   - Out-of-order result for unknown `tool_use_id` renders as its own
     card (orphan path — the fallback in `reducer.ts:37`).

2. **`card.test.ts`** — `eventToCardProps()` is pure + total.
   - Every EVT.* value maps to a non-null `CardProps`.
   - Unknown event type → error-kind card (default branch is
     observable + deterministic).

3. **`sse.test.ts`** — reconnect-on-close keeps cursor monotonic.
   - Mock `EventSource`; assert on "close" event the client resumes
     from the highest id it already acked, not 0.

4. **`store.test.ts`** — `emit(sig)` + `subscribe(sig, cb)` contract.
   - Subscribers fire in registration order; unsubscribe removes the
     callback; no double-fire on `emit()` inside a subscriber.

## Long-term

Drop a JSON fixture (`fixtures/example-session-events.json`) captured
from a real session and use it in an end-to-end-ish reducer test that
mirrors the Python-side `test_e2e_cli_to_web.py` money test. That gives
cross-stack alignment coverage — if the Python event model and the TS
reducer ever disagree on a specific sequence, the fixture catches it on
both sides.

## Non-goals

- **Visual regression / snapshot tests**. Too fragile given the pure
  imperative DOM rendering in `card.ts`. DOM-shape assertions in the
  reducer tests are enough.
- **End-to-end browser tests**. Out of scope until Phase 12+; the
  Python SSE E2E already covers the wire contract.
