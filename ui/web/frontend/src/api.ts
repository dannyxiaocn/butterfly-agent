// HTTP surface — one thin request() helper + named wrappers per route.
// Routes match ui/web/app.py (DESIGN.md §7.1). No business logic here.
import type {
  BfEvent,
  HudSnapshot,
  ModelsCatalog,
  Params,
  PanelEntry,
  PanelEntryDetail,
  Session,
  TaskCard,
  TerminalState,
  TodoListSnapshot,
} from './types';

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${method} ${path} -> ${res.status}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  // ── sessions ──
  listSessions: (): Promise<Session[]> =>
    request('GET', '/api/sessions'),
  getSession: (id: string): Promise<Session & { params: Params }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}`),
  createSession: (body: { agent: string; display_name?: string }): Promise<{ id: string; agent: string; display_name?: string | null }> =>
    request('POST', '/api/sessions', body),
  deleteSession: (id: string): Promise<void> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}`),
  startSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/start`),
  stopSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/stop`),
  interruptSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/interrupt`),

  // ── input ──
  sendMessage: (id: string, content: string): Promise<{ id: number; event_id: number }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/messages`, { content }),

  // ── events ──
  /** History replay: returns the full event list for the session. The
   *  frontend reducer consumes the same event shapes as the live SSE
   *  stream — one code path (DESIGN.md §7.3). */
  readEvents: (id: string, sinceId = 0): Promise<{ events: BfEvent[] }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/events?since_id=${sinceId}`),

  /** Display history: identical to readEvents for the frontend's purposes,
   *  but the backend filters to for_llm + UI-visible system events. Use
   *  this for initial paint; use readEvents for raw audit needs. */
  readHistory: (id: string, sinceId = 0): Promise<{ events: BfEvent[] }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/history?since_id=${sinceId}`),

  // ── HUD ──
  getHud: (id: string): Promise<HudSnapshot> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/hud`),

  // ── tasks / todo ──
  getTasks: (id: string): Promise<{ cards: TaskCard[] }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/tasks`),
  upsertTask: (id: string, body: Partial<TaskCard> & { previous_name?: string }): Promise<{ ok: boolean }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/tasks`, body),
  deleteTask: (id: string, name: string): Promise<{ ok: boolean }> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}/tasks/${encodeURIComponent(name)}`),
  getTodoList: (id: string): Promise<{ todo_list: TodoListSnapshot | null }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/todo_list`),

  // ── config / prompts / assets ──
  getConfig: (id: string): Promise<{ params: Params }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/config`),
  setConfig: (id: string, params: Params): Promise<{ ok: boolean; params: Params }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/config`, { params }),
  getPromptMd: (id: string, name: 'system' | 'task' | 'env'): Promise<{ text: string }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/prompts/${name}`),
  setPromptMd: (id: string, name: 'system' | 'task' | 'env', text: string): Promise<{ ok: boolean; text: string }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/prompts/${name}`, { text }),
  getAssetMd: (id: string, name: 'tools' | 'skills'): Promise<{ text: string }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/assets/${name}`),
  setAssetMd: (id: string, name: 'tools' | 'skills', text: string): Promise<{ ok: boolean; text: string }> =>
    request('PUT', `/api/sessions/${encodeURIComponent(id)}/assets/${name}`, { text }),

  // ── panel ──
  getPanel: (id: string): Promise<PanelEntry[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel`),
  getPanelEntry: (id: string, tid: string): Promise<PanelEntryDetail> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}`),
  killPanelEntry: (id: string, tid: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}/kill`),

  // ── terminal ──
  // ``tail`` is accepted for back-compat with the retained terminal.ts
  // controller (Phase 8 kept the terminal component as-is). The backend
  // currently ignores it — it always returns the full in-memory log.
  getTerminal: (id: string, _tail = 500): Promise<{
    state: TerminalState;
    log: Array<{ ts: number; source: string; text: string; seq?: number }>;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/terminal`),
  getTerminalLog: (id: string, offset = 0): Promise<{
    log: Array<{ ts: number; source: string; text: string; seq?: number }>;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/terminal/log?offset=${offset}`),
  postTerminalInput: (id: string, content: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/terminal/input`, { content }),
  postTerminalInterrupt: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/terminal/interrupt`),

  // ── catalogs ──
  getModels: (): Promise<ModelsCatalog> =>
    request('GET', '/api/models'),
  listAgents: (): Promise<{ agents: string[] }> =>
    request('GET', '/api/agents'),

  // ── weixin bridge (retained) ──
  getWeixinStatus: (): Promise<{ status: string; error?: string; session?: string; account?: string }> =>
    request('GET', '/api/weixin/status'),
};
