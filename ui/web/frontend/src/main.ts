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

layout.appendChild(sidebar);
layout.appendChild(chat);
layout.appendChild(panel);

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

  // Refresh task cards every 15s when a session is active
  setInterval(async () => {
    const id = store.currentSessionId;
    if (!id) return;
    try {
      const tasks = await api.getTasks(id);
      if (store.currentSessionId !== id) return; // stale
      store.taskCards = tasks.cards;
      store.emit('tasks');
    } catch {
      // ignore
    }
  }, 15000);

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

  startUpdateNotifier();
}

// ====== Auto-update notifier ======
//
// Polls `/api/update_status` every 30s. The server's auto-update worker
// writes this file when it detects new upstream commits:
//   - `applied: true` means a silent update landed → force-reload the page
//     so the browser picks up the rebuilt bundle.
//   - `dirty: true` + `available: true` means the tree has uncommitted local
//     changes and the worker refused to apply → show a top-right banner so
//     the user can commit/stash and run `butterfly update` manually.

function startUpdateNotifier() {
  // Baseline `applied_at` seen on the page's first observation — if we see a
  // different one later, the bundle is stale relative to what the server now
  // serves and we force-reload. Starts `null` so we can distinguish "no
  // status file on page load" from "file present, this is the baseline".
  let baselineAppliedAt: string | null = null;
  let baselineStamped = false;
  let bannerCommits = -1;
  let banner: HTMLElement | null = null;

  const renderBanner = (commitsBehind: number) => {
    const text = `🔔 ${commitsBehind} new commit${commitsBehind === 1 ? '' : 's'} upstream — commit local changes and run \`butterfly update\` to apply.`;
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'update-banner';
      document.body.appendChild(banner);
    }
    banner.textContent = text;
    bannerCommits = commitsBehind;
  };

  const hideBanner = () => {
    if (!banner) return;
    banner.remove();
    banner = null;
    bannerCommits = -1;
  };

  const poll = async () => {
    try {
      const s = await api.getUpdateStatus();
      if (s.applied && s.applied_at) {
        // Explicit reload signal from the worker always wins — the server
        // just finished `execvp`-ing, so the cached bundle is stale.
        if (s.reload) {
          window.location.reload();
          return;
        }
        // First observation baselines the value; later polls that see a
        // different `applied_at` indicate the server silently respawned
        // while we weren't watching (e.g. worker cleared and re-wrote).
        if (!baselineStamped) {
          baselineAppliedAt = s.applied_at;
          baselineStamped = true;
          hideBanner();
          return;
        }
        if (baselineAppliedAt !== s.applied_at) {
          window.location.reload();
          return;
        }
        hideBanner();
      } else if (s.dirty && s.available && s.commits_behind) {
        // Stamp baseline even on dirty so a later clean apply is detected.
        if (!baselineStamped) baselineStamped = true;
        // Re-render when commit count changes so users see fresh counts.
        if (s.commits_behind !== bannerCommits) renderBanner(s.commits_behind);
      } else {
        if (!baselineStamped) baselineStamped = true;
        hideBanner();
      }
    } catch {
      // ignore — server may be restarting during auto-update respawn
    }
  };

  void poll();
  setInterval(poll, 30_000);
}

init().catch(console.error);
