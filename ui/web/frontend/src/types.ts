export interface Session {
  id: string;
  agent: string;
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

export interface ModelCatalogEntry {
  name: string;
  max_context_tokens: number;
  exposes_reasoning_tokens: boolean;
  default: boolean;
}

export interface ProviderCatalogEntry {
  provider: string;
  label: string;
  env: string[];
  supports_thinking: boolean;
  default_model: string;
  models: ModelCatalogEntry[];
}

export interface ModelsCatalog {
  providers: ProviderCatalogEntry[];
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
  result?: string;
  result_truncated?: boolean;
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
  // llm_call_usage (v2.0.19): per-LLM-call accounting for the HUD. Token
  // counts are nested under ``usage`` (same shape as loop_end) — the
  // top-level ``input`` field on this event is NOT a token count (it's
  // reserved for the tool_call event's input record).
  iteration?: number;
  context_tokens?: number;
  toks_per_s?: number | null;
  // thinking_tokens_update (v2.0.19): credits the provider-reported
  // reasoning_tokens for one LLM call to a specific thinking block so the
  // cell label flips from "Thought Xs" to "Thought Xs for N tokens".
  reasoning_tokens?: number;
  // v2.0.20: persisted thinking block whose turn ended via interrupt before
  // on_thinking_end closed it — history replay renders these as
  // "Thinking interrupted" instead of the normal "Thought" label.
  interrupted?: boolean;
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
