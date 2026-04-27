import type { DisplayEvent } from './types';
import { bfToDisplay, type BfEvent } from './eventAdapter';

type SSEHandler = (event: DisplayEvent) => void;

// ── SSE shim for the post-PR-#57 backend ────────────────────────────────────
//
// New server contract (DESIGN.md §7.2): ``GET /api/sessions/{id}/events/stream
// ?cursor=N`` emits one SSE frame per events_v1 entry, with ``id:`` = the
// event's monotonic integer id, ``event:`` = the event type, ``data:`` =
// the BfEvent envelope ``{id, ts, type, for_llm, payload}``. Browser
// auto-reconnect uses Last-Event-ID to resume.
//
// The old frontend wants DisplayEvent shapes (``{type: 'agent', content,
// ts, ...}``) and tracked TWO byte offsets (context.jsonl + events.jsonl).
// We adapt at this seam: a single integer cursor (event id) replaces the
// two offsets — both contextSince and eventsSince are reused as event ids;
// when the old UI passes them back to ``reconnectWithOffsets`` we just take
// max() so we never roll backwards.

const NEW_EVENT_TYPES = [
  'user_input', 'user_interrupt',
  'agent_text', 'agent_thinking', 'agent_tool_call', 'agent_tool_result',
  'session_created', 'session_started', 'session_stopped', 'session_deleted',
  'model_status', 'llm_call_usage',
  'tool_progress',
  'task_card_changed', 'task_script_check', 'task_script_error', 'task_finished',
  'todo_list_changed',
  'terminal_log', 'terminal_state', 'terminal_input',
  'panel_entry_changed', 'sub_agent_count',
  'config_changed', 'prompt_changed', 'asset_changed',
  'system_notice', 'error',
  'control_interrupt', 'control_start', 'control_stop',
];

export class SSEConnection {
  private es: EventSource | null = null;
  private sessionId: string | null = null;
  private handler: SSEHandler | null = null;
  private seenIds = new Set<number>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private cursor = 0;
  private closed = false;

  /** Latest events_v1 id processed by the live stream. Old main.ts reads this
   *  as ``latestContextOffset`` to keep its replay cursor in sync; we now
   *  return the unified event-id cursor. The naming stays for callsite
   *  stability. */
  get latestContextOffset(): number { return this.cursor; }

  attach(sessionId: string, contextSince: number, eventsSince: number, handler: SSEHandler): void {
    this.close();
    this.closed = false;
    this.sessionId = sessionId;
    // The old main.ts feeds two offsets but they're just "resume points" —
    // pick the larger so we never replay anything already seen.
    this.cursor = Math.max(0, contextSince || 0, eventsSince || 0);
    this.handler = handler;
    this.seenIds.clear();
    this._connect();
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.es) {
      this.es.close();
      this.es = null;
    }
  }

  /** Re-connect with a fresh cursor (e.g. after tab regains focus). The two
   *  offsets are folded into a single max(); we never roll back the cursor. */
  reconnectWithOffsets(sessionId: string, contextSince: number, eventsSince: number): void {
    if (this.closed || !this.sessionId || this.sessionId !== sessionId) return;
    const next = Math.max(this.cursor, contextSince || 0, eventsSince || 0);
    this.cursor = next;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.es?.close();
    this.es = null;
    this._connect();
  }

  private _connect(): void {
    if (this.closed || !this.sessionId) return;
    const url = `/api/sessions/${encodeURIComponent(this.sessionId)}/events/stream`
      + `?cursor=${this.cursor}`;
    this.es = new EventSource(url);

    const onMessage = (e: MessageEvent) => {
      try {
        const bf = JSON.parse(e.data) as BfEvent;
        if (typeof bf.id !== 'number') return;
        if (this.seenIds.has(bf.id)) return;
        this.seenIds.add(bf.id);
        if (bf.id > this.cursor) this.cursor = bf.id;
        // Trim ring buffer.
        if (this.seenIds.size > 4000) {
          const arr = Array.from(this.seenIds);
          this.seenIds = new Set(arr.slice(arr.length - 2000));
        }
        const display = bfToDisplay(bf);
        for (const d of display) this.handler?.(d);
      } catch {
        // ignore parse errors
      }
    };

    // The new server emits one SSE frame per event with ``event: <type>``;
    // we subscribe to every known type so EventSource invokes our handler.
    for (const type of NEW_EVENT_TYPES) {
      this.es.addEventListener(type, onMessage as EventListener);
    }
    // Generic ``message`` fallback for unknown types (forwards to the same
    // adapter, which has a default branch).
    this.es.addEventListener('message', onMessage as EventListener);

    this.es.onerror = () => {
      if (this.closed) return;
      this.es?.close();
      this.es = null;
      this.reconnectTimer = setTimeout(() => {
        if (!this.closed) this._connect();
      }, 3000);
    };
  }
}

export const sseConn = new SSEConnection();
