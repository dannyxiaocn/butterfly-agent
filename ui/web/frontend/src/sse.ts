import type { DisplayEvent } from './types';

type SSEHandler = (event: DisplayEvent) => void;

export class SSEConnection {
  private es: EventSource | null = null;
  private sessionId: string | null = null;
  private handler: SSEHandler | null = null;
  // Dedup by event data 'id' field (not SSE seq number which resets each connection).
  // seenIds is only cleared on attach() (new session), NOT on reconnect — so events
  // already delivered are never shown twice, even after a drop+reconnect.
  private seenIds = new Set<string>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private contextSince = 0;
  private eventsSince = 0;
  private closed = false;

  /** Latest context.jsonl byte offset processed by the live SSE stream.
   * Used by main.ts to keep lastRenderedContextOffset in sync without
   * needing _ctx to survive the clean-event strip (Bug 1 + Bug 3 interaction). */
  get latestContextOffset(): number { return this.contextSince; }

  attach(sessionId: string, contextSince: number, eventsSince: number, handler: SSEHandler): void {
    this.close();
    this.closed = false;
    this.sessionId = sessionId;
    this.contextSince = contextSince;
    this.eventsSince = eventsSince;
    this.handler = handler;
    this.seenIds.clear(); // clear only when switching sessions
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

  /** Re-connect immediately with fresh offsets (e.g. after tab regains focus).
   * sessionId must match the currently attached session; otherwise no-op.
   * Uses Math.max so SSE's already-advanced offsets are never rolled back (Bug 2). */
  reconnectWithOffsets(sessionId: string, contextSince: number, eventsSince: number): void {
    if (this.closed || !this.sessionId || this.sessionId !== sessionId) return;
    this.contextSince = Math.max(this.contextSince, contextSince);
    this.eventsSince = Math.max(this.eventsSince, eventsSince);
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
    // seenIds is NOT cleared here — it persists across reconnects so already-seen
    // events (user messages, agent responses) are not duplicated after a drop+reconnect.
    const url = `/api/sessions/${encodeURIComponent(this.sessionId)}/events`
      + `?context_since=${this.contextSince}&events_since=${this.eventsSince}`;
    this.es = new EventSource(url);

    const eventTypes = [
      'agent', 'user', 'tool', 'thinking', 'model_status', 'partial_text',
      'tool_done', 'thinking_start', 'thinking_done',
      'loop_start', 'loop_end',
      'task_wakeup', 'task_finished', 'status', 'error', 'message',
      'system_notice',
      // Sub-agent / background-tool UI events
      'tool_progress', 'tool_finalize', 'sub_agent_count', 'panel_update',
    ];

    for (const type of eventTypes) {
      this.es.addEventListener(type, (e: Event) => {
        const me = e as MessageEvent;
        try {
          const data: DisplayEvent = JSON.parse(me.data);

          // Advance resume offsets from server-embedded _ctx/_evt fields.
          // This ensures reconnects start from where we left off rather than
          // replaying the full backlog (Problem 11).
          if (typeof (data as any)._ctx === 'number') {
            this.contextSince = Math.max(this.contextSince, (data as any)._ctx);
          }
          if (typeof (data as any)._evt === 'number') {
            this.eventsSince = Math.max(this.eventsSince, (data as any)._evt);
          }

          // Dedup by event data 'id' field (only permanent events carry an id).
          // Ephemeral events (partial_text, model_status, tool) have no id and
          // always pass through — their handlers are idempotent.
          // Strip meta-fields before passing to handler so they don't leak
          // into DisplayEvent objects seen by renderEvent/handleEvent (Bug 3).
          const { _ctx, _evt, ...cleanData } = data as any;
          const cleanEvent = cleanData as DisplayEvent;

          const eventId = cleanEvent.id;
          if (eventId) {
            if (this.seenIds.has(eventId)) return;
            this.seenIds.add(eventId);
            // Trim ring buffer
            if (this.seenIds.size > 2000) {
              const arr = Array.from(this.seenIds);
              this.seenIds = new Set(arr.slice(arr.length - 1000));
            }
          }
          this.handler?.(cleanEvent);
        } catch {
          // ignore parse errors
        }
      });
    }

    this.es.onerror = () => {
      if (this.closed) return;
      this.es?.close();
      this.es = null;
      // reconnect after 3s using latest advanced offsets — seenIds prevents
      // already-seen permanent events from duplicating on reconnect.
      this.reconnectTimer = setTimeout(() => {
        if (!this.closed) this._connect();
      }, 3000);
    };
  }
}

export const sseConn = new SSEConnection();
