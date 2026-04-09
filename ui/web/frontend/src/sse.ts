import type { DisplayEvent } from './types';

type SSEHandler = (event: DisplayEvent) => void;

export class SSEConnection {
  private es: EventSource | null = null;
  private sessionId: string | null = null;
  private handler: SSEHandler | null = null;
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
    // Clear seenIds on each new connection — server seq restarts from 0 each time.
    this.seenIds.clear();
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
        // dedup by SSE id
        if (me.lastEventId && this.seenIds.has(me.lastEventId)) return;
        if (me.lastEventId) this.seenIds.add(me.lastEventId);
        // trim ring buffer
        if (this.seenIds.size > 2000) {
          const arr = Array.from(this.seenIds);
          this.seenIds = new Set(arr.slice(arr.length - 1000));
        }
        try {
          const data: DisplayEvent = JSON.parse(me.data);
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
      // reconnect after 3s
      this.reconnectTimer = setTimeout(() => {
        if (!this.closed) this._connect();
      }, 3000);
    };
  }
}

export const sseConn = new SSEConnection();
