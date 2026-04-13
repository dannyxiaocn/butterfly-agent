export interface Session {
  id: string;
  entity: string;
  status: 'active' | 'stopped';
  pid: number | null;
  pid_alive: boolean;
  model_state: 'idle' | 'running';
  model_source: string | null;
  last_run_at: string | null;
  created_at: string | null;
  stopped_at: string | null;
  persistent: boolean;
  has_tasks: boolean;
  heartbeat_interval: number;
  params?: Params;
}

export interface Params {
  heartbeat_interval: number;
  model: string | null;
  provider: string | null;
  fallback_model: string | null;
  fallback_provider: string | null;
  tool_providers: Record<string, string>;
  session_type: 'ephemeral' | 'default' | 'persistent';
  thinking: boolean;
  thinking_budget: number;
  thinking_effort: string;
  is_meta_session?: boolean;
  [key: string]: unknown;
}

export interface TaskCard {
  name: string;
  description: string;
  interval: number | null;
  start_at: string | null;
  end_at: string | null;
  status: 'pending' | 'working' | 'finished' | 'paused';
  last_started_at: string | null;
  last_finished_at: string | null;
  created_at: string;
  comments: string;
  progress: string;
}

export interface DisplayEvent {
  type: string;
  content?: string;
  ts?: string;
  name?: string;
  input?: Record<string, unknown>;
  state?: string;
  source?: string;
  value?: string;
  triggered_by?: string;
  usage?: {
    input?: number;
    output?: number;
    cache_read?: number;
    cache_write?: number;
  };
  result_len?: number;
  iterations?: number;
  id?: string;
}

export type SessionTone = 'running' | 'napping' | 'persistent' | 'stopped' | 'idle' | 'meta';

export function sessionTone(sess: Session): SessionTone {
  if (sess.id.endsWith('_meta') || sess.params?.is_meta_session) return 'meta';
  if (sess.pid_alive && sess.model_state === 'running' && sess.status !== 'stopped') return 'running';
  if (sess.pid_alive && sess.has_tasks && sess.status !== 'stopped') return 'napping';
  if (sess.persistent && sess.status !== 'stopped') return 'persistent';
  if (sess.status === 'stopped') return 'stopped';
  return 'idle';
}

export function toneColor(tone: SessionTone): string {
  switch (tone) {
    case 'running': return 'var(--green)';
    case 'napping': return 'var(--yellow)';
    case 'persistent': return 'var(--yellow)';
    case 'stopped': return 'var(--red)';
    case 'meta': return '#a371f7';
    case 'idle': return 'var(--muted)';
  }
}

export function toneLabel(tone: SessionTone): string {
  switch (tone) {
    case 'running': return 'running';
    case 'napping': return 'napping';
    case 'persistent': return 'persistent';
    case 'stopped': return 'stopped';
    case 'meta': return 'meta';
    case 'idle': return 'idle';
  }
}
