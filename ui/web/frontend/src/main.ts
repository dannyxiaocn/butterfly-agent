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
export async function attachSession(id: string): Promise<void> {
  // Update active session immediately so sidebar re-renders
  store.currentSessionId = id;
  store.emit('currentSession');

  // Clear chat
  getChatEl().clearMessages();

  // Load history first, then open SSE from returned offsets
  let contextOffset = 0;
  let eventsOffset = 0;

  try {
    const history = await api.getHistory(id);
    for (const event of history.events) {
      getChatEl().appendEvent(event);
    }
    contextOffset = history.context_offset;
    eventsOffset = history.events_offset;
  } catch (e) {
    console.error('Failed to load history:', e);
  }

  // Load tasks
  try {
    const tasks = await api.getTasks(id);
    store.taskCards = tasks.cards;
    store.emit('tasks');
  } catch (e) {
    console.error('Failed to load tasks:', e);
  }

  // Load config / params
  try {
    const cfg = await api.getConfig(id);
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
    console.error('Failed to load config:', e);
  }

  // Refresh HUD on attach
  getChatEl().refreshHud(id).catch(() => {});

  // Open SSE from history offsets
  sseConn.attach(id, contextOffset, eventsOffset, (event: DisplayEvent) => {
    if (store.currentSessionId !== id) return; // stale SSE
    getChatEl().handleEvent(event);

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
    if (!store.currentSessionId) return;
    try {
      const tasks = await api.getTasks(store.currentSessionId);
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
