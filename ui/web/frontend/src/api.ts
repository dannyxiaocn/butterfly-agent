import type {
  DisplayEvent,
  ModelsCatalog,
  Params,
  PanelEntry,
  PanelEntryDetail,
  Session,
  TaskCard,
} from './types';

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

export const api = {
  listSessions: (): Promise<Session[]> =>
    request('GET', '/api/sessions'),

  getSession: (id: string): Promise<Session> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}`),

  createSession: (body: { id?: string; agent: string }): Promise<{ id: string; agent: string }> =>
    request('POST', '/api/sessions', body),

  getUpdateStatus: (): Promise<{
    applied?: boolean;
    available?: boolean;
    dirty?: boolean;
    commits_behind?: number;
    local_head?: string;
    remote_head?: string;
    applied_at?: string;
    checked_at?: string;
    new_head?: string;
    reload?: boolean;
  }> => request('GET', '/api/update_status'),

  deleteSession: (id: string): Promise<void> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}`),

  sendMessage: (
    id: string,
    content: string,
    mode: 'interrupt' | 'wait' = 'interrupt',
  ): Promise<{ id: string; mode: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/messages`, { content, mode }),

  getHistory: (id: string, contextSince = 0): Promise<{ events: DisplayEvent[]; context_offset: number; events_offset: number }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/history?context_since=${contextSince}`),

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
    git: { files: number; added: number; deleted: number };
    usage: { input?: number; output?: number; cache_read?: number; cache_write?: number; reasoning?: number } | null;
    sub_agents_running?: number;
  }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/hud`),

  getPanel: (id: string): Promise<PanelEntry[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel`),

  getPanelEntry: (id: string, tid: string): Promise<PanelEntryDetail> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}`),

  killPanelEntry: (id: string, tid: string): Promise<{ status: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/panel/${encodeURIComponent(tid)}/kill`),

  /** Last `n` events from a session's events.jsonl. Used by the sub-agent
   *  panel card to show what the child is doing right now. */
  getEventsTail: (id: string, n: number = 5): Promise<Array<Record<string, unknown>>> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/events_tail?n=${encodeURIComponent(n)}`),
};
