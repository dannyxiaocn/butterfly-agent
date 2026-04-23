import type { DisplayEvent, PanelEntry, Params, Session, TaskCard, TodoListSnapshot } from './types';

type Listener = () => void;

class Store {
  sessions: Session[] = [];
  currentSessionId: string | null = null;
  currentParams: Params | null = null;
  modelState: { state: string; source: string | null } = { state: 'idle', source: null };
  taskCards: TaskCard[] = [];
  todoList: TodoListSnapshot | null = null;
  panelEntries: PanelEntry[] = [];
  weixinStatus: { status: string; error?: string; session?: string; account?: string } = { status: 'idle' };
  chatEvents: DisplayEvent[] = [];

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
}

export const store = new Store();
