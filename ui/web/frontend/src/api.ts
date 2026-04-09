import type { DisplayEvent, Params, Session, TaskCard } from './types';

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

  createSession: (body: { id?: string; entity: string; heartbeat?: number }): Promise<{ id: string; entity: string }> =>
    request('POST', '/api/sessions', body),

  deleteSession: (id: string): Promise<void> =>
    request('DELETE', `/api/sessions/${encodeURIComponent(id)}`),

  sendMessage: (id: string, content: string): Promise<{ id: string }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/messages`, { content }),

  getHistory: (id: string): Promise<{ events: DisplayEvent[]; context_offset: number; events_offset: number }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/history`),

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

  startSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/start`),

  stopSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/stop`),

  interruptSession: (id: string): Promise<{ ok: boolean }> =>
    request('POST', `/api/sessions/${encodeURIComponent(id)}/interrupt`),

  getWeixinStatus: (): Promise<{ status: string; error?: string; session?: string; account?: string }> =>
    request('GET', '/api/weixin/status'),

  getHud: (id: string): Promise<{ cwd: string; context_bytes: number; git: { files: number; added: number; deleted: number }; usage: { input?: number; output?: number; cache_read?: number; cache_write?: number } | null }> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/hud`),
};
