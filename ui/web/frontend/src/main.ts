// Entry point + session bootstrap.
// The critical shape here: attachSession() fetches history -> runs
// reducer -> opens SSE -> routes every live event through the SAME
// reducer. Live and replay share a single code path (DESIGN.md §7.3).
import './style.css';
import { api } from './api';
import { store } from './store';
import { sseConn } from './sse';
import { reduce, reduceMany } from './reducer';
import type { BfEvent } from './types';
import { createHeader } from './components/header';
import { createSidebar } from './components/sidebar';
import { createChat, refreshHud } from './components/chat';
import { createPanel } from './components/panel';
import { terminalController } from './components/terminal';

// ── Layout ────────────────────────────────────────────────────────────────

const app = document.getElementById('app')!;
const header = createHeader();
app.appendChild(header);

const layout = document.createElement('div');
layout.id = 'layout';
app.appendChild(layout);

const sidebar = createSidebar();
const chat = createChat();
const panel = createPanel();
const leftResizer = createResizer('sidebar');
const rightResizer = createResizer('panel');

layout.appendChild(sidebar);
layout.appendChild(leftResizer);
layout.appendChild(chat);
layout.appendChild(rightResizer);
layout.appendChild(panel);
restoreColumnWidths();

function createResizer(which: 'sidebar' | 'panel'): HTMLElement {
  const el = document.createElement('div');
  el.className = 'layout-resizer';
  el.dataset.target = which;
  el.setAttribute('role', 'separator');
  el.setAttribute('aria-orientation', 'vertical');
  const cssVar = which === 'sidebar' ? '--sidebar-width' : '--panel-width';
  const storageKey = which === 'sidebar' ? 'butterfly.layout.sidebarWidth' : 'butterfly.layout.panelWidth';
  const MIN = 160;
  const MAX_FRACTION = 0.6;
  let startX = 0;
  let startW = 0;
  const readCurrentPx = (): number => {
    const cs = getComputedStyle(layout).getPropertyValue(cssVar).trim();
    const n = parseFloat(cs);
    return Number.isFinite(n) && n > 0 ? n : (which === 'sidebar' ? 240 : 300);
  };
  const onMove = (ev: PointerEvent) => {
    const dx = ev.clientX - startX;
    const delta = which === 'sidebar' ? dx : -dx;
    const vp = window.innerWidth || 1200;
    const next = Math.max(MIN, Math.min(vp * MAX_FRACTION, startW + delta));
    layout.style.setProperty(cssVar, `${Math.round(next)}px`);
  };
  const onUp = (ev: PointerEvent) => {
    el.releasePointerCapture(ev.pointerId);
    el.removeEventListener('pointermove', onMove);
    el.removeEventListener('pointerup', onUp);
    el.classList.remove('dragging');
    document.body.classList.remove('resizing');
    try { localStorage.setItem(storageKey, String(Math.round(readCurrentPx()))); } catch {}
  };
  el.addEventListener('pointerdown', (ev: PointerEvent) => {
    ev.preventDefault();
    startX = ev.clientX;
    startW = readCurrentPx();
    el.setPointerCapture(ev.pointerId);
    el.classList.add('dragging');
    document.body.classList.add('resizing');
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
  });
  return el;
}

function restoreColumnWidths() {
  try {
    const sb = localStorage.getItem('butterfly.layout.sidebarWidth');
    const pn = localStorage.getItem('butterfly.layout.panelWidth');
    if (sb) layout.style.setProperty('--sidebar-width', `${parseInt(sb, 10)}px`);
    if (pn) layout.style.setProperty('--panel-width', `${parseInt(pn, 10)}px`);
  } catch {}
}

// ── Session attach ────────────────────────────────────────────────────────

// Monotonic token so late async replies from a stale session get dropped.
let attachVersion = 0;

export async function attachSession(id: string): Promise<void> {
  const version = ++attachVersion;

  store.currentSessionId = id;
  store.resetSession();
  store.emit('currentSession');
  store.emit('cards');
  store.emit('tasks');
  store.emit('panel');

  // 1. Load history → feed through reducer (single code path).
  let events: BfEvent[] = [];
  try {
    const r = await api.readHistory(id, 0);
    if (attachVersion !== version) return;
    events = r.events ?? [];
    reduceMany(events);
  } catch (e) {
    if (attachVersion !== version) return;
    console.error('history load failed:', e);
  }

  // 2. Snapshots that aren't fully captured in the event stream yet
  //    (tasks / todo / config / panel / HUD / terminal). The events
  //    stream DOES carry task_card_changed, todo_list_changed, etc., but
  //    those only fire on mutation — initial values come from the
  //    snapshot endpoints.
  refreshSnapshots(id, version);

  if (attachVersion !== version) return;

  // 3. Open SSE from the high-water cursor the reducer applied.
  sseConn.attach(id, store.cursor, (event: BfEvent) => {
    if (store.currentSessionId !== id) return;
    // Live events go through the SAME reducer as history. No divergence.
    const signals = reduce(event);
    for (const sig of signals) store.emit(sig);
    // Always emit 'cards' for card-producing events even if the reducer
    // already did — subscribers are idempotent.
    if (signals.includes('cards')) store.emit('cards');
    // HUD refresh on llm_call_usage / model_status — snapshot endpoint
    // gives us the resolved percentages + context_tokens.
    if (event.type === 'llm_call_usage' || event.type === 'model_status') {
      refreshHud(id).catch(() => {});
    }
    // Tasks snapshot refresh on task_* transitions — snapshot gives us
    // the full TaskCard list with derived fields the bare event doesn't
    // carry (next run times, etc.).
    if (event.type === 'task_card_changed' || event.type === 'task_script_check'
        || event.type === 'task_finished') {
      api.getTasks(id).then(r => {
        if (store.currentSessionId !== id) return;
        store.tasks = r.cards;
        store.emit('tasks');
      }).catch(() => {});
    }
    if (event.type === 'todo_list_changed') {
      api.getTodoList(id).then(r => {
        if (store.currentSessionId !== id) return;
        store.todoList = r.todo_list;
        store.emit('todoList');
      }).catch(() => {});
    }
    if (event.type === 'panel_entry_changed' || event.type === 'tool_progress'
        || event.type === 'sub_agent_count') {
      api.getPanel(id).then(entries => {
        if (store.currentSessionId !== id) return;
        store.panel = entries;
        store.emit('panel');
      }).catch(() => {});
    }
    // Route terminal events to the retained terminalController.
    if (event.type === 'terminal_log' || event.type === 'terminal_state'
        || event.type === 'terminal_input') {
      terminalController.handleEvent(event as any);
    }
  });
}

async function refreshSnapshots(id: string, version: number) {
  try {
    const r = await api.getTasks(id);
    if (attachVersion !== version) return;
    store.tasks = r.cards;
    store.emit('tasks');
  } catch {}
  try {
    const r = await api.getTodoList(id);
    if (attachVersion !== version) return;
    store.todoList = r.todo_list;
    store.emit('todoList');
  } catch {}
  try {
    const r = await api.getConfig(id);
    if (attachVersion !== version) return;
    store.currentParams = r.params;
    store.emit('config');
  } catch {}
  try {
    const r = await api.getPanel(id);
    if (attachVersion !== version) return;
    store.panel = r;
    store.emit('panel');
  } catch {}
  try {
    await refreshHud(id);
  } catch {}
  // Terminal: leave its state management inside the controller.
  terminalController.attachSession(id).catch(() => {});
}

// ── Bootstrap ─────────────────────────────────────────────────────────────

async function init(): Promise<void> {
  try {
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  } catch (e) { console.error('listSessions failed:', e); }

  // Poll weixin status every 5s (small bridge health indicator — not an
  // event-stream participant, so a light poll is fine).
  async function pollWeixin() {
    try {
      store.weixinStatus = await api.getWeixinStatus();
      store.emit('weixin');
    } catch {}
  }
  pollWeixin();
  setInterval(pollWeixin, 5000);

  // Poll session list every 3s so status dots refresh independently of
  // the SSE stream — the sidebar's session_* reducer hits already update
  // on create / delete / start / stop, but pid_alive / model_state are
  // derived from live daemon probes in the backend.
  setInterval(async () => {
    try {
      store.sessions = await api.listSessions();
      store.emit('sessions');
    } catch {}
  }, 3000);

  // HUD tick — 10s, only when a session is attached.
  setInterval(() => {
    if (store.currentSessionId) refreshHud(store.currentSessionId).catch(() => {});
  }, 10000);

  // Re-sync on tab visibility regain: replay any events the browser may
  // have dropped while backgrounded. Dispatch through the reducer like
  // everything else.
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    const id = store.currentSessionId;
    if (!id) return;
    try {
      const r = await api.readHistory(id, store.cursor);
      if (store.currentSessionId !== id) return;
      if (r.events?.length) reduceMany(r.events);
      // Reopen SSE from the new cursor.
      sseConn.close();
      sseConn.attach(id, store.cursor, () => {});
      // Re-attach with the real handler by re-calling attachSession —
      // simplest and cheapest for an infrequent event.
      attachSession(id);
    } catch {}
  });

  // Cmd+K / Ctrl+K focuses chat input.
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      const input = document.getElementById('chat-input') as HTMLTextAreaElement | null;
      if (input && !input.disabled) input.focus();
    }
  });

  if (store.sessions.length > 0) {
    await attachSession(store.sessions[0].id);
  }
}

init().catch(console.error);

// Hint the 'chat' variable is used, so noUnusedLocals doesn't complain.
// (createChat attaches event listeners; we deliberately don't keep a
// strong typed ref since main.ts doesn't call into it directly anymore —
// all communication goes through the store.)
void chat;
