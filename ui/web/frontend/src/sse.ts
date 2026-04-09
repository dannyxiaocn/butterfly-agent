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

  /** Re-connect immediately with fresh offsets (e.g. after tab regains focus). */
  reconnectWithOffsets(contextSince: number, eventsSince: number): void {
    if (this.closed || !this.sessionId) return;
    this.contextSince = contextSince;
    this.eventsSince = eventsSince;
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
      'agent', 'user', 'tool', 'model_status', 'partial_text',
      'heartbeat_trigger', 'heartbeat_finished', 'status', 'error', 'message'
    ];

    for (const type of eventTypes) {
      this.es.addEventListener(type, (e: Event) => {
        const me = e as MessageEvent;
        try {
          const data: DisplayEvent = JSON.parse(me.data);
          // Dedup by event data 'id' field (only permanent events carry an id).
          // Ephemeral events (partial_text, model_status, tool) have no id and
          // always pass through — their handlers are idempotent.
          const eventId = data.id;
          if (eventId) {
            if (this.seenIds.has(eventId)) return;
            this.seenIds.add(eventId);
            // Trim ring buffer
            if (this.seenIds.size > 2000) {
              const arr = Array.from(this.seenIds);
              this.seenIds = new Set(arr.slice(arr.length - 1000));
            }
          }
          this.handler?.(data);
        } catch {
          // ignore parse errors
        }
      });
    }

    this.es.onerror = () => {
      if (this.closed) return;
      this.es?.close();
      this.es = null;
      // reconnect after 3s using same offsets — server-side BridgeSession is fresh
      // each connection, so events.jsonl+context.jsonl are re-read from these offsets.
      // seenIds prevents already-seen permanent events from duplicating.
      this.reconnectTimer = setTimeout(() => {
        if (!this.closed) this._connect();
      }, 3000);
    };
  }
}

export const sseConn = new SSEConnection();
