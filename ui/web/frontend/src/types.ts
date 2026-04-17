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
  params?: Params;
  // Sub-agent hierarchy: when set, this session was spawned by another and
  // the sidebar renders it indented under its parent.
  parent_session_id?: string | null;
  // Sub-agent permission mode (explorer / executor) — surfaced in sidebar
  // chip and panel cards.
  mode?: string | null;
}

export interface Params {
  model: string | null;
  provider: string | null;
  fallback_model: string | null;
  fallback_provider: string | null;
  tool_providers: Record<string, string>;
  thinking: boolean;
  thinking_budget: number;
  thinking_effort: string;
  is_meta_session?: boolean;
  [key: string]: unknown;
}

export type PanelEntryStatus =
  | 'running'
  | 'completed'
  | 'stalled'
  | 'killed'
  | 'killed_by_restart';

export interface PanelEntry {
  tid: string;
  type: string;
  tool_name: string;
  input: Record<string, unknown>;
  status: PanelEntryStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  polling_interval: number | null;
  last_delivered_bytes: number;
  last_activity_at: number | null;
  pid: number | null;
  exit_code: number | null;
  output_file: string | null;
  output_bytes: number;
  meta: Record<string, unknown>;
}

export interface PanelEntryDetail extends PanelEntry {
  output_tail: string | null;
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

export interface ProviderCatalogEntry {
  provider: string;
  label: string;
  env: string[];
  supports_thinking: boolean;
  thinking_style: 'budget' | 'effort' | 'extra_body' | null;
  /** Effort vocabulary this provider accepts. Empty for providers using
   *  budget / no thinking style (Anthropic, Kimi, plain OpenAI). */
  supported_efforts: string[];
  default_model: string;
  models: string[];
}

export interface ModelsCatalog {
  providers: ProviderCatalogEntry[];
  thinking_efforts: string[];
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
    reasoning?: number;
  };
  result_len?: number;
  iterations?: number;
  id?: string;
  card?: string;
  message?: string;
  // thinking_start / thinking_done
  block_id?: string;
  text?: string;
  duration_ms?: number;
  // Background-spawn tagging on tool_done so the UI keeps the cell yellow
  // until tool_finalize arrives (sub_agent + bash background).
  is_background?: boolean;
  tid?: string;
  // tool_progress: latest one-line summary (e.g. "running tool: bash").
  summary?: string;
  // tool_finalize: terminal kind from BackgroundEvent.
  kind?: string;
  exit_code?: number | null;
  // sub_agent_count: HUD badge tally.
  running?: number;
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
