import './style.css';
import { api } from './api';
import { store } from './store';
import { sseConn } from './sse';
import type { DisplayEvent } from './types';
import { createHeader } from './components/header';
import { createSidebar } from './components/sidebar';
import { createChat } from './components/chat';
import { createPanel } from './components/panel';

// Build the main layout
const app = document.getElementById('app')!;

const header = createHeader();
app.appendChild(header);

const layout = document.createElement('div');
layout.id = 'layout';
app.appendChild(layout);

const sidebar = createSidebar();
const chat = createChat();
const panel = createPanel();

const leftResizer = createLayoutResizer('sidebar');
const rightResizer = createLayoutResizer('panel');

layout.appendChild(sidebar);
layout.appendChild(leftResizer);
layout.appendChild(chat);
layout.appendChild(rightResizer);
layout.appendChild(panel);

restoreColumnWidths();

/** Build a 4-px gutter between two layout columns. Dragging it updates the
 *  corresponding CSS var on ``#layout`` so sidebar / panel grow or shrink
 *  in step. Widths are clamped and persisted to localStorage so the layout
 *  survives a reload. ``which`` selects which column the gutter resizes —
 *  "sidebar" (left gutter) or "panel" (right gutter).
 */
function createLayoutResizer(which: 'sidebar' | 'panel'): HTMLElement {
  const el = document.createElement('div');
  el.className = 'layout-resizer';
  el.dataset.target = which;
  el.setAttribute('role', 'separator');
  el.setAttribute('aria-orientation', 'vertical');

  const MIN = 160;
  const MAX_FRACTION = 0.6; // never let one gutter eat >60% of the viewport
  const cssVar = which === 'sidebar' ? '--sidebar-width' : '--panel-width';
  const storageKey = which === 'sidebar' ? 'butterfly.layout.sidebarWidth' : 'butterfly.layout.panelWidth';

  let startX = 0;
  let startWidth = 0;

  const readCurrentPx = (): number => {
    const cs = getComputedStyle(layout).getPropertyValue(cssVar).trim();
    const n = parseFloat(cs);
    return Number.isFinite(n) && n > 0 ? n : (which === 'sidebar' ? 240 : 300);
  };

  const onPointerMove = (ev: PointerEvent) => {
    const dx = ev.clientX - startX;
    const delta = which === 'sidebar' ? dx : -dx;
    const viewport = window.innerWidth || 1200;
    const next = Math.max(MIN, Math.min(viewport * MAX_FRACTION, startWidth + delta));
    layout.style.setProperty(cssVar, `${Math.round(next)}px`);
  };

  const onPointerUp = (ev: PointerEvent) => {
    el.releasePointerCapture(ev.pointerId);
    el.removeEventListener('pointermove', onPointerMove);
    el.removeEventListener('pointerup', onPointerUp);
    el.classList.remove('dragging');
    document.body.classList.remove('resizing');
    const finalPx = readCurrentPx();
    try { localStorage.setItem(storageKey, String(Math.round(finalPx))); } catch {}
  };

  el.addEventListener('pointerdown', (ev: PointerEvent) => {
    ev.preventDefault();
    startX = ev.clientX;
    startWidth = readCurrentPx();
    el.setPointerCapture(ev.pointerId);
    el.classList.add('dragging');
    document.body.classList.add('resizing');
    el.addEventListener('pointermove', onPointerMove);
    el.addEventListener('pointerup', onPointerUp);
  });

  return el;
}

function restoreColumnWidths() {
  try {
    const saved = {
      sidebar: localStorage.getItem('butterfly.layout.sidebarWidth'),
      panel: localStorage.getItem('butterfly.layout.panelWidth'),
    };
    if (saved.sidebar) layout.style.setProperty('--sidebar-width', `${parseInt(saved.sidebar, 10)}px`);
    if (saved.panel) layout.style.setProperty('--panel-width', `${parseInt(saved.panel, 10)}px`);
  } catch {
    // localStorage may be unavailable in private-browsing edge cases —
    // fall through to the CSS defaults.
  }
}

// Typed accessor for chat methods
interface ChatEl extends HTMLElement {
  clearMessages(): void;
  appendEvent(e: DisplayEvent): void;
  handleEvent(e: DisplayEvent): void;
  refreshHud(id: string): Promise<void>;
}

function getChatEl(): ChatEl {
  return chat as ChatEl;
}

// ====== Session attach (exported for sidebar) ======

// Monotonic attach token: each attachSession() increments this.
// Every async step checks the token is still current before applying results,
// preventing race conditions when the user switches sessions quickly (Problem 3).
let attachVersion = 0;

// Track the latest rendered context offset so visibilitychange can fetch
// only new events rather than the full history (Problem 2).
let lastRenderedContextOffset = 0;

export async function attachSession(id: string): Promise<void> {
  const version = ++attachVersion;

  // Update active session immediately so sidebar re-renders
  store.currentSessionId = id;
  store.emit('currentSession');

  // Clear chat and reset per-session state so panel doesn't show stale data
  getChatEl().clearMessages();
  lastRenderedContextOffset = 0;
  store.taskCards = [];
  store.panelEntries = [];
  store.currentParams = null;
  store.emit('tasks');
  store.emit('panel');
  store.emit('config');

  // Load history first, then open SSE from returned offsets
  let contextOffset = 0;
  let eventsOffset = 0;

  try {
    const history = await api.getHistory(id);
    if (attachVersion !== version) return; // stale — user switched again
    for (const event of history.events) {
      getChatEl().appendEvent(event);
    }
    contextOffset = history.context_offset;
    eventsOffset = history.events_offset;
    lastRenderedContextOffset = contextOffset;
  } catch (e) {
    if (attachVersion !== version) return;
    console.error('Failed to load history:', e);
  }

  // Load tasks
  try {
    const tasks = await api.getTasks(id);
    if (attachVersion !== version) return;
    store.taskCards = tasks.cards;
    store.emit('tasks');
  } catch (e) {
    if (attachVersion !== version) return;
    console.error('Failed to load tasks:', e);
  }

  // Load config / params
  try {
    const cfg = await api.getConfig(id);
    if (attachVersion !== version) return;
    store.currentParams = cfg.params;
    store.emit('config');
    // Also update the session's params in store.sessions for meta detection
    const sessIdx = store.sessions.findIndex(s => s.id === id);
    if (sessIdx >= 0) {
      store.sessions[sessIdx] = { ...store.sessions[sessIdx], params: cfg.params };
      store.emit('sessions');
      store.emit('currentSession');
    }
  } catch (e) {
    if (attachVersion !== version) return;
    console.error('Failed to load config:', e);
  }

  // Final freshness check before opening SSE (must be last side effect)
  if (attachVersion !== version) return;

  // Refresh HUD on attach
  getChatEl().refreshHud(id).catch(() => {});

  // Open SSE from history offsets
  sseConn.attach(id, contextOffset, eventsOffset, (event: DisplayEvent) => {
    if (store.currentSessionId !== id) return; // stale SSE
    getChatEl().handleEvent(event);

    // Advance lastRenderedContextOffset from SSE's internal contextSince tracker.
    // _ctx is stripped from the event before the handler is called (Bug 3 fix),
    // so we read the offset via the public getter instead of (event as any)._ctx.
    const latest = sseConn.latestContextOffset;
    if (latest > lastRenderedContextOffset) {
      lastRenderedContextOffset = latest;
    }

    // Update model state for header / sidebar
    if (event.type === 'model_status') {
      // Refresh session list to update status dots
      api.listSessions().then(sessions => {
        store.sessions = sessions;
        store.emit('sessions');
      }).catch(console.error);
    }

    // v2.0.30: Tasks tab refresh is now on-event.
    //   * `task_card_changed` → agent CRUD tool or API edit
    //   * `task_check` / `task_check_error` → script poll just ran (may
    //     have flipped status or updated last_checked_at)
    //   * `task_wakeup` / `task_finished` → card transitioned
    //     working ↔ pending around an agent turn
    if (
      event.type === 'task_card_changed' ||
      event.type === 'task_check' ||
      event.type === 'task_check_error' ||
      event.type === 'task_wakeup' ||
      event.type === 'task_finished'
    ) {
      api.getTasks(id).then(res => {
        if (store.currentSessionId !== id) return; // stale
        store.taskCards = res.cards;
        store.emit('tasks');
      }).catch(() => {
        // Non-fatal — next event triggers another refresh.
      });
    }
  });
}

// ====== Bootstrap ======
async function init(): Promise<void> {
  // Load sessions
  try {
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  } catch (e) {
    console.error('Failed to load sessions:', e);
  }

  // Poll weixin status every 5s
  async function pollWeixin() {
    try {
      const status = await api.getWeixinStatus();
      store.weixinStatus = status;
      store.emit('weixin');
    } catch {
      // ignore
    }
  }
  pollWeixin();
  setInterval(pollWeixin, 5000);

  // Poll sessions list every 3s to update status dots
  setInterval(async () => {
    try {
      const sessions = await api.listSessions();
      store.sessions = sessions;
      store.emit('sessions');
    } catch {
      // ignore
    }
  }, 3000);

  // v2.0.30: no more 15 s Tasks polling — the SSE handler below reloads
  // store.taskCards in response to `task_card_changed`, `task_check`,
  // `task_check_error`, `task_wakeup`, and `task_finished`. That covers
  // agent-driven CRUD, script-poll transitions, and the "wakeup landed"
  // round-trip.

  // Refresh HUD every 10s when a session is active
  setInterval(async () => {
    if (!store.currentSessionId) return;
    getChatEl().refreshHud(store.currentSessionId).catch(() => {});
  }, 10000);

  // Re-sync SSE when tab regains focus (browser throttles/drops SSE in background).
  // Also renders any events that completed while the tab was hidden (Problem 2).
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    const id = store.currentSessionId;
    if (!id) return;
    try {
      // Fetch only history AFTER last rendered offset — render new completed events
      const history = await api.getHistory(id, lastRenderedContextOffset);
      if (store.currentSessionId !== id) return; // session changed during await (Problem 5)
      for (const event of history.events) {
        getChatEl().appendEvent(event);
      }
      lastRenderedContextOffset = history.context_offset;
      sseConn.reconnectWithOffsets(id, history.context_offset, history.events_offset);
    } catch {
      // ignore — SSE reconnect is best-effort
    }
  });

  // Keyboard shortcut: Cmd+K / Ctrl+K to focus chat input
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      const input = document.getElementById('chat-input') as HTMLTextAreaElement | null;
      if (input && !input.disabled) {
        input.focus();
      }
    }
  });

  // Auto-attach first session if any
  if (store.sessions.length > 0) {
    await attachSession(store.sessions[0].id);
  }
}

init().catch(console.error);
