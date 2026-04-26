// Pure reducer: store + event -> store. Used by both live SSE and
// history replay (DESIGN.md §7.3 "History = SSE, minus the tail.").
//
// The reducer only touches store fields that are directly derivable from
// the event stream. It DOES NOT call the backend; fetches that refresh
// tasks/panel/terminal snapshots live in main.ts, triggered by emit() of
// the relevant signal below. This keeps the reducer synchronous and pure.
import { EVT } from './types';
import type { BfEvent } from './types';
import { store, type Card } from './store';

/** Dispatch one event. Returns the list of signal names to emit after
 *  the caller batches its updates. We return instead of emit-in-place so
 *  the history bootstrap can dispatch many events then emit once. */
export function reduce(event: BfEvent): string[] {
  const signals = new Set<string>();

  // Cards (one event -> one card, with tool_use pairing exception)
  const cardAction = cardActionFor(event);
  if (cardAction === 'append') {
    const card: Card = { event };
    store.cards.push(card);
    const tid = event.payload?.tool_use_id;
    if (event.type === EVT.AGENT_TOOL_CALL && typeof tid === 'string') {
      store.cardByToolUseId.set(tid, card);
    }
    signals.add('cards');
  } else if (cardAction === 'upgrade-tool-result') {
    const tid = event.payload?.tool_use_id;
    if (typeof tid === 'string') {
      const target = store.cardByToolUseId.get(tid);
      if (target) {
        target.resultEvent = event;
        signals.add('card:' + tid);
      } else {
        // Orphan result (no matching call) — render as its own system card.
        store.cards.push({ event });
        signals.add('cards');
      }
    }
  }
  // Other event types don't render their own card; they still trigger
  // targeted refreshes below.

  // Cursor
  if (event.id > store.cursor) store.cursor = event.id;

  // Lateral side-signals
  switch (event.type) {
    case EVT.LLM_CALL_USAGE:
    case EVT.MODEL_STATUS:
      signals.add('hud');
      break;
    case EVT.TASK_CARD_CHANGED:
    case EVT.TASK_SCRIPT_CHECK:
    case EVT.TASK_SCRIPT_ERROR:
    case EVT.TASK_FINISHED:
      signals.add('tasks');
      break;
    case EVT.TODO_LIST_CHANGED:
      signals.add('todoList');
      signals.add('hud');
      break;
    case EVT.PANEL_ENTRY_CHANGED:
    case EVT.TOOL_PROGRESS:
    case EVT.SUB_AGENT_COUNT:
      signals.add('panel');
      signals.add('hud');
      break;
    case EVT.TERMINAL_LOG:
    case EVT.TERMINAL_STATE:
    case EVT.TERMINAL_INPUT:
      signals.add('terminal');
      break;
    case EVT.CONFIG_CHANGED:
      signals.add('config');
      break;
    case EVT.SESSION_CREATED:
    case EVT.SESSION_STARTED:
    case EVT.SESSION_STOPPED:
    case EVT.SESSION_DELETED:
      signals.add('sessions');
      break;
  }

  return Array.from(signals);
}

/** Classify what the event does to the cards array. */
function cardActionFor(event: BfEvent): 'append' | 'upgrade-tool-result' | 'ignore' {
  switch (event.type) {
    case EVT.USER_INPUT:
      return 'append';
    case EVT.USER_INTERRUPT:
      // Only render a card when it carries text (per DESIGN.md §3.3
      // "Always appears in LLM context if text is set"). Bare interrupts
      // are cancellation signals — no user-visible card.
      return typeof event.payload?.text === 'string' && event.payload.text.length > 0
        ? 'append'
        : 'ignore';
    case EVT.AGENT_TEXT:
    case EVT.AGENT_THINKING:
    case EVT.AGENT_TOOL_CALL:
      return 'append';
    case EVT.AGENT_TOOL_RESULT:
      return 'upgrade-tool-result';
    case EVT.SYSTEM_NOTICE:
    case EVT.ERROR:
      return 'append';
    default:
      return 'ignore';
  }
}

/** Apply a list of events and emit the union of signals once. Used by
 *  the history bootstrap — one re-render instead of N. */
export function reduceMany(events: BfEvent[]): void {
  const union = new Set<string>();
  for (const ev of events) {
    for (const sig of reduce(ev)) union.add(sig);
  }
  for (const sig of union) store.emit(sig);
  // Always emit 'cards' once at the end as a safety net — most events
  // touch it. Cheap because subscribers are idempotent.
  store.emit('cards');
}
