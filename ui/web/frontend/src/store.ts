// Minimal pub-sub store. No derivation, no signals library — the
// reducer writes here, components subscribe and re-render.
import type {
  BfEvent,
  HudSnapshot,
  PanelEntry,
  Params,
  Session,
  TaskCard,
  TerminalState,
  TodoListSnapshot,
} from './types';

// One Card per Event. Cards are ordered by event id (monotonic per session).
// The tool_use ↔ tool_result pairing (DESIGN.md §8.1) is the ONLY
// cross-event rule: we key the call's card by tool_use_id so the matching
// result can upgrade it in place.
export interface Card {
  event: BfEvent;
  // When a tool_result arrives, we mutate the matching call-card's
  // resultPayload rather than appending a second card.
  resultEvent?: BfEvent | null;
}

type Listener = () => void;

export interface StoreShape {
  currentSessionId: string | null;
  sessions: Session[];
  currentParams: Params | null;
  cards: Card[];
  cardByToolUseId: Map<string, Card>;
  hud: HudSnapshot | null;
  tasks: TaskCard[];
  todoList: TodoListSnapshot | null;
  panel: PanelEntry[];
  terminal: TerminalState | null;
  weixinStatus: { status: string; error?: string; session?: string; account?: string };
  // Highest event id applied through the reducer for the current session.
  // SSE / history consumers use this as a cursor.
  cursor: number;
}

class Store implements StoreShape {
  currentSessionId: string | null = null;
  sessions: Session[] = [];
  currentParams: Params | null = null;
  cards: Card[] = [];
  cardByToolUseId: Map<string, Card> = new Map();
  hud: HudSnapshot | null = null;
  tasks: TaskCard[] = [];
  todoList: TodoListSnapshot | null = null;
  panel: PanelEntry[] = [];
  terminal: TerminalState | null = null;
  weixinStatus: { status: string; error?: string; session?: string; account?: string } = { status: 'idle' };
  cursor = 0;

  private _listeners: Map<string, Set<Listener>> = new Map();

  on(event: string, fn: Listener): () => void {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event)!.add(fn);
    return () => this._listeners.get(event)?.delete(fn);
  }

  emit(event: string): void {
    this._listeners.get(event)?.forEach(fn => fn());
    this._listeners.get('*')?.forEach(fn => fn());
  }

  get currentSession(): Session | null {
    return this.sessions.find(s => s.id === this.currentSessionId) ?? null;
  }

  /** Reset per-session state. Called on session switch. The reducer
   *  always assumes a clean slate for a new session — live + replay both
   *  dispatch against this zero state. */
  resetSession(): void {
    this.cards = [];
    this.cardByToolUseId = new Map();
    this.hud = null;
    this.tasks = [];
    this.todoList = null;
    this.panel = [];
    this.terminal = null;
    this.cursor = 0;
  }
}

export const store = new Store();
