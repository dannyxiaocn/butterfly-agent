// SSE client. One Event -> one handler() call. Server sends the Event
// id as SSE id:, so browser auto-reconnect via Last-Event-ID restarts
// from the right place (DESIGN.md §7.2).
import type { BfEvent } from './types';

type SSEHandler = (event: BfEvent) => void;

export class SSEConnection {
  private es: EventSource | null = null;
  private sessionId: string | null = null;
  private handler: SSEHandler | null = null;
  private cursor = 0;
  private closed = false;

  /** Open / reopen the SSE stream. Caller passes the cursor they've
   *  already rendered through the reducer; server replays id > cursor. */
  attach(sessionId: string, cursor: number, handler: SSEHandler): void {
    this.close();
    this.closed = false;
    this.sessionId = sessionId;
    this.cursor = cursor;
    this.handler = handler;
    this._connect();
  }

  close(): void {
    this.closed = true;
    if (this.es) {
      this.es.close();
      this.es = null;
    }
  }

  private _connect(): void {
    if (this.closed || !this.sessionId) return;
    const url = `/api/sessions/${encodeURIComponent(this.sessionId)}/events/stream?cursor=${this.cursor}`;
    this.es = new EventSource(url);

    // The server emits SSE frames with a typed event: field matching the
    // Event.type. Use 'message' as a catch-all because EventSource only
    // delivers untyped frames through onmessage, and typed listeners
    // swallow unknowns. The server always sets event:, but being
    // defensive costs nothing.
    const onFrame = (e: MessageEvent) => {
      try {
        const data: BfEvent = JSON.parse(e.data);
        if (typeof data?.id !== 'number') return;
        if (data.id <= this.cursor) return; // dedup on reconnect
        this.cursor = data.id;
        this.handler?.(data);
      } catch {
        // ignore parse errors
      }
    };

    // Attach the same handler on 'message' plus every known event: type.
    // EventSource only delivers to 'message' for frames without a custom
    // event: field; typed frames go to their named listener. We register
    // both so schema drift (unknown types) still surfaces via 'message'.
    this.es.addEventListener('message', onFrame as EventListener);
    const knownTypes = [
      'user_input', 'user_interrupt',
      'agent_text', 'agent_thinking', 'agent_tool_call', 'agent_tool_result',
      'session_created', 'session_started', 'session_stopped', 'session_deleted',
      'model_status', 'llm_call_usage', 'tool_progress',
      'task_card_changed', 'task_script_check', 'task_script_error', 'task_finished',
      'todo_list_changed',
      'terminal_log', 'terminal_state', 'terminal_input',
      'panel_entry_changed', 'sub_agent_count',
      'config_changed', 'prompt_changed', 'asset_changed',
      'system_notice', 'error',
      'control_interrupt', 'control_start', 'control_stop',
    ];
    for (const type of knownTypes) {
      this.es.addEventListener(type, onFrame as EventListener);
    }

    this.es.onerror = () => {
      // EventSource auto-reconnects with Last-Event-ID built from the
      // last id: frame it received. We leave that to the browser; if the
      // connection is permanently dead, we'll reopen on the next attach().
      if (this.closed) return;
    };
  }
}

export const sseConn = new SSEConnection();
