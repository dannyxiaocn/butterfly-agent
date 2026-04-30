import type {
  DisplayEvent,
  ModelsCatalog,
  Params,
  PanelEntry,
  PanelEntryDetail,
  Session,
  TaskCard,
  TodoListSnapshot,
} from './types';
import { bfToDisplay, type BfEvent } from './eventAdapter';

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${method} ${path} → ${res.status}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** Fold a list of BfEvents through ``bfToDisplay`` and surface the largest
 *  event id seen — the old frontend uses that as both ``context_offset`` and
 *  ``events_offset`` (the two were merged into one cursor when PR #57 collapsed
 *  context.jsonl + events.jsonl into events_v1.jsonl). */
function adaptEvents(events: BfEvent[]): { events: DisplayEvent[]; offset: number } {
  const out: DisplayEvent[] = [];
  let maxId = 0;
  for (const bf of events) {
    if (typeof bf.id === 'number' && bf.id > maxId) maxId = bf.id;
    for (const d of bfToDisplay(bf)) out.push(d);
  }
  return { events: out, offset: maxId };
}

export const api = {
  listSessions: (): Promise<Session[]> =>
    request('GET', '/api/sessions'),

  getSession: (id: string): Promise<Session> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}`),

  createSession: (body: { agent: string; display_name?: string }): Promise<{ id: string; agent: string; display_name?: string | null }> =>
    request('POST', '/api/sessions', body),

  deleteSession: (id: string): Promise<void> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}`),

  sendMessage: (
    id: string,
    content: string,
    mode: 'interrupt' | 'wait' = 'interrupt',
  ): Promise<{ id: string; mode: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/messages`, { content, mode }),

  getHistory: async (id: string, contextSince = 0): Promise<{ events: DisplayEvent[]; context_offset: number; events_offset: number }> => {
    // PR #57 unified the two old streams into one events_v1.jsonl whose
    // /history endpoint returns ``{events: BfEvent[]}`` keyed by ``since_id``.
    // We reuse ``contextSince`` as the event-id cursor (its semantic in the
    // restored UI: "resume after this point") and project the largest seen
    // id back into both legacy offset slots.
    const r = await request<{ events: BfEvent[] }>(
      'GET',
      `/api/sessions/${encodeURIComponent(id)}/history?since_id=${contextSince}`,
    );
    const { events, offset } = adaptEvents(r.events ?? []);
    return { events, context_offset: offset, events_offset: offset };
  },

  getTasks: (id: string): Promise<{ cards: TaskCard[] }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/tasks`),

  upsertTask: (id: string, body: Partial<TaskCard> & { previous_name?: string }): Promise<{ ok: boolean }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/tasks`, body),

  deleteTask: (id: string, name: string): Promise<{ ok: boolean }> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}/tasks/${encodeURIComponent(name)}`),

  getConfig: (id: string): Promise<{ params: Params }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/config`),

  setConfig: (id: string, params: Params): Promise<{ ok: boolean; params: Params }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/config`, { params }),

  getAssetMd: (id: string, name: 'tools' | 'skills'): Promise<{ text: string }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/assets/${name}`),

  setAssetMd: (id: string, name: 'tools' | 'skills', text: string): Promise<{ ok: boolean; text: string }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/assets/${name}`, { text }),

  getPromptMd: (id: string, name: 'system' | 'task' | 'env'): Promise<{ text: string }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/prompts/${name}`),

  setPromptMd: (id: string, name: 'system' | 'task' | 'env', text: string): Promise<{ ok: boolean; text: string }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/prompts/${name}`, { text }),

  getModels: (): Promise<ModelsCatalog> =>
    request('GET', '/api/models'),

  listAgents: (): Promise<{ agents: string[] }> =>
    request('GET', '/api/agents'),

  startSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/start`),

  stopSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/stop`),

  interruptSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/interrupt`),

  getWeixinStatus: (): Promise<{ status: string; error?: string; session?: string; account?: string }> =>
    request('GET', '/api/weixin/status'),

  getHud: (id: string): Promise<{
    cwd: string;
    context_bytes: number;
    context_tokens: number | null;
    max_context_tokens: number;
    toks_per_s: number | null;
    model: string | null;
    thinking?: boolean;
    thinking_effort?: string | null;
    git: { files: number; added: number; deleted: number };
    usage: { input?: number; output?: number; cache_read?: number; cache_write?: number; reasoning?: number } | null;
    sub_agents_running?: number;
    bash_running?: number;
    todo?: {
      progress_line: string;
      active_index: number | null;
      total: number;
      pending_count: number;
      all_done: boolean;
      iters_since_seen: number;
      threshold: number;
      items: Array<{
        content: string;
        status: 'pending' | 'in_progress' | 'completed';
        activeForm: string;
      }>;
    } | null;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/hud`),

  getTodoList: (id: string): Promise<{ todo_list: TodoListSnapshot | null }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/todo_list`),

  getPanel: (id: string): Promise<PanelEntry[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel`),

  getPanelEntry: (id: string, tid: string): Promise<PanelEntryDetail> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}`),

  killPanelEntry: (id: string, tid: string): Promise<{ status: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}/kill`),

  /** Last `n` events from a session's events_v1.jsonl. Used by the sub-agent
   *  panel card to show what the child is doing right now. PR #57 dropped the
   *  dedicated ``/events_tail`` endpoint; we now read the full event list and
   *  take the tail client-side. Cheap for the typical case where the child
   *  only has a handful of events; the sub-agent card refreshes on a slow
   *  cadence so a full read is fine. */
  getEventsTail: async (id: string, n: number = 5): Promise<Array<Record<string, unknown>>> => {
    const r = await request<{ events: BfEvent[] }>(
      'GET',
      `/api/sessions/${encodeURIComponent(id)}/events`,
    );
    const all = r.events ?? [];
    const tail = all.slice(-Math.max(1, n));
    // Project to the loose dict shape the sub-agent card consumes; the only
    // field it cares about is ``type`` and a couple of payload keys.
    return tail.map(e => ({
      id: e.id,
      ts: e.ts,
      type: e.type,
      ...(e.payload || {}),
    }));
  },

  // ── Terminal panel ────────────────────────────────────────────────

  getTerminal: (id: string, tail: number = 500): Promise<{
    state: {
      active: boolean;
      cwd: string | null;
      cwd_display: string | null;
      home: string | null;
      venv: string | null;
      git_branch: string | null;
      git_dirty: boolean | null;
      last_active_at: number | null;
      foreground_pid: number | null;
      foreground_cmd: string | null;
      locked_by: 'agent' | 'user' | null;
      shell_pid: number | null;
    };
    log: Array<{ ts: number; source: string; text: string; seq?: number }>;
    log_offset: number;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/terminal?tail=${tail}`),

  getTerminalLog: (id: string, offset: number): Promise<{
    log: Array<{ ts: number; source: string; text: string; seq?: number }>;
    log_offset: number;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/terminal/log?offset=${offset}`),

  postTerminalInput: (id: string, content: string): Promise<{ id: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/terminal/input`, { content }),

  postTerminalInterrupt: (id: string): Promise<{ id: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/terminal/interrupt`),
};
