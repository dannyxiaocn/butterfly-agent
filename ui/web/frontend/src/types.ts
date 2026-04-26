// Frontend types — mirror butterfly/runtime/events.py schema (DESIGN.md §3).
// One event envelope: {id, ts, type, for_llm, payload}. Payload shape varies
// by type; we stay loose (payload: any) at the boundary and narrow inside
// eventToCardProps().

// ── Event type literals (mirror butterfly/runtime/events.py) ────────────────
export const EVT = {
  USER_INPUT: 'user_input',
  USER_INTERRUPT: 'user_interrupt',
  AGENT_TEXT: 'agent_text',
  AGENT_THINKING: 'agent_thinking',
  AGENT_TOOL_CALL: 'agent_tool_call',
  AGENT_TOOL_RESULT: 'agent_tool_result',
  SESSION_CREATED: 'session_created',
  SESSION_STARTED: 'session_started',
  SESSION_STOPPED: 'session_stopped',
  SESSION_DELETED: 'session_deleted',
  MODEL_STATUS: 'model_status',
  LLM_CALL_USAGE: 'llm_call_usage',
  TOOL_PROGRESS: 'tool_progress',
  TASK_CARD_CHANGED: 'task_card_changed',
  TASK_SCRIPT_CHECK: 'task_script_check',
  TASK_SCRIPT_ERROR: 'task_script_error',
  TASK_FINISHED: 'task_finished',
  TODO_LIST_CHANGED: 'todo_list_changed',
  TERMINAL_LOG: 'terminal_log',
  TERMINAL_STATE: 'terminal_state',
  TERMINAL_INPUT: 'terminal_input',
  PANEL_ENTRY_CHANGED: 'panel_entry_changed',
  SUB_AGENT_COUNT: 'sub_agent_count',
  CONFIG_CHANGED: 'config_changed',
  PROMPT_CHANGED: 'prompt_changed',
  ASSET_CHANGED: 'asset_changed',
  SYSTEM_NOTICE: 'system_notice',
  ERROR: 'error',
  CONTROL_INTERRUPT: 'control_interrupt',
  CONTROL_START: 'control_start',
  CONTROL_STOP: 'control_stop',
} as const;

export type EventType = typeof EVT[keyof typeof EVT] | string;

export interface BfEvent {
  id: number;
  ts: number;
  type: EventType;
  for_llm: boolean;
  payload: Record<string, any>;
}

// ── Session / UI types ──────────────────────────────────────────────────────

export interface Params {
  model: string | null;
  provider: string | null;
  fallback_model?: string | null;
  fallback_provider?: string | null;
  thinking?: boolean;
  thinking_budget?: number;
  thinking_effort?: string;
  is_meta_session?: boolean;
  [key: string]: unknown;
}

export interface Session {
  id: string;
  agent: string;
  status: 'active' | 'stopped' | string;
  pid: number | null;
  pid_alive: boolean;
  model_state: 'idle' | 'running' | string;
  model_source: string | null;
  last_run_at: string | null;
  created_at: string | null;
  stopped_at: string | null;
  persistent: boolean;
  has_tasks: boolean;
  params?: Params;
  parent_session_id?: string | null;
  mode?: string | null;
  display_name?: string | null;
}

export interface TaskCard {
  name: string;
  description: string;
  check_interval: number;
  status: 'pending' | 'working' | 'finished' | 'paused';
  last_checked_at: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  created_at: string;
  comments: string;
  progress: string;
  script?: string | null;
}

export interface TodoListItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  activeForm: string;
}

export interface TodoListSnapshot {
  progress_line: string;
  progress: string;
  comments: string;
  active_index: number | null;
  total: number;
  pending_count: number;
  all_done: boolean;
  iters_since_seen: number;
  threshold: number;
  updated_at: string | null;
  items: TodoListItem[];
}

export type PanelEntryStatus =
  | 'running'
  | 'completed'
  | 'stalled'
  | 'killed'
  | 'killed_by_restart'
  | string;

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

export interface TerminalState {
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
}

export interface HudSnapshot {
  cwd?: string;
  context_bytes?: number;
  context_tokens?: number | null;
  max_context_tokens?: number;
  toks_per_s?: number | null;
  model?: string | null;
  thinking?: boolean;
  thinking_effort?: string | null;
  git?: { files: number; added: number; deleted: number };
  usage?: { input?: number; output?: number; cache_read?: number; cache_write?: number; reasoning?: number } | null;
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
    items: TodoListItem[];
  } | null;
}

// ── Model catalog ───────────────────────────────────────────────────────────

export interface ModelCatalogEntry {
  name: string;
  max_context_tokens: number;
  exposes_reasoning_tokens: boolean;
  default: boolean;
  thinking_mode?: string | null;
  thinking_effort?: string | null;
  thinking_display?: string | null;
  thinking_budget_tokens?: number | null;
  interleaved_thinking_beta?: boolean | null;
  cache_strategy?: string | null;
  cache_ttl?: string | null;
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

// ── Session tone helpers (reused from pre-refactor UI) ──────────────────────

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
  return tone;
}
