// Sessions sidebar: list + new / start / stop / delete buttons.
// Session list refreshes from store.sessions signals (main.ts polls list
// + the reducer emits 'sessions' on session_* events).
import { api } from '../api';
import { store } from '../store';
import { Session, sessionTone, toneColor } from '../types';
import { attachSession } from '../main';

export function createSidebar(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'sidebar';

  let formVisible = false;
  let agentOptions: string[] | null = null;
  let selectedAgent = 'agent';

  el.innerHTML = `
    <div class="sidebar-header">
      <span class="sidebar-title">Sessions</span>
      <button class="btn-icon" id="btn-new-session" title="New session">+</button>
    </div>
    <div class="session-list">
      <div id="new-session-form" class="new-session-card hidden">
        <input id="ns-display-name" class="ns-name-input" type="text" placeholder="Session name…" maxlength="40" autocomplete="off" />
        <div class="new-session-card-row2">
          <select id="ns-agent" class="ns-agent-select"><option value="">Loading…</option></select>
          <div class="new-session-card-actions">
            <button class="btn-sm btn-primary" id="ns-create">Create</button>
            <button class="btn-sm" id="ns-cancel">Cancel</button>
          </div>
        </div>
      </div>
      <div id="session-list-items"></div>
    </div>
    <div class="sidebar-footer">
      <button class="btn-sm btn-start" id="btn-start" title="Resume">▶</button>
      <button class="btn-sm btn-stop" id="btn-stop" title="Pause">⏸</button>
      <button class="btn-sm btn-danger" id="btn-delete" title="Delete">🗑</button>
    </div>
  `;

  const formEl = el.querySelector('#new-session-form') as HTMLDivElement;
  const nameInput = el.querySelector('#ns-display-name') as HTMLInputElement;
  const agentSelect = el.querySelector('#ns-agent') as HTMLSelectElement;
  const listItemsEl = el.querySelector('#session-list-items') as HTMLDivElement;

  function updateAgentOptions() {
    if (!agentOptions) { agentSelect.innerHTML = '<option value="">Loading…</option>'; return; }
    if (!agentOptions.length) { agentSelect.innerHTML = '<option value="">(no agents)</option>'; return; }
    agentSelect.innerHTML = agentOptions
      .map(a => `<option value="${esc(a)}"${a === selectedAgent ? ' selected' : ''}>${esc(a)}</option>`)
      .join('');
  }

  async function ensureAgents() {
    if (agentOptions) return;
    try {
      const r = await api.listAgents();
      agentOptions = r.agents;
      if (!agentOptions.includes(selectedAgent) && agentOptions.length) selectedAgent = agentOptions[0];
      updateAgentOptions();
    } catch (e) { console.error('listAgents failed:', e); }
  }

  function openForm() {
    formVisible = true;
    formEl.classList.remove('hidden');
    ensureAgents();
    queueMicrotask(() => nameInput.focus());
  }

  function closeForm() {
    formVisible = false;
    formEl.classList.add('hidden');
    nameInput.value = '';
  }

  async function submitCreate() {
    const agentName = (agentSelect.value || 'agent').trim();
    const body: { agent: string; display_name?: string } = {
      agent: agentName.startsWith('agenthub/') ? agentName : `agenthub/${agentName}`,
    };
    const trimmedName = nameInput.value.trim();
    if (trimmedName) body.display_name = trimmedName;
    try {
      const res = await api.createSession(body);
      closeForm();
      const sessions = await api.listSessions();
      store.sessions = sessions;
      store.emit('sessions');
      await attachSession(res.id);
    } catch (e) {
      alert(`Failed to create session: ${e}`);
    }
  }

  el.querySelector('#btn-new-session')!.addEventListener('click', () => {
    formVisible ? closeForm() : openForm();
  });
  el.querySelector('#ns-cancel')!.addEventListener('click', closeForm);
  el.querySelector('#ns-create')!.addEventListener('click', submitCreate);
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitCreate(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeForm(); }
  });
  agentSelect.addEventListener('change', () => { selectedAgent = agentSelect.value; });

  el.querySelector('#btn-start')!.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    await api.startSession(store.currentSessionId).catch(console.error);
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  });

  el.querySelector('#btn-stop')!.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    // Stop first fires an interrupt to unwind any in-flight tick; then
    // stop_session flips the daemon's run flag. Matches the pre-refactor
    // behaviour that v2.0.24 locked in.
    await api.interruptSession(store.currentSessionId).catch(console.error);
    await api.stopSession(store.currentSessionId).catch(console.error);
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  });

  el.querySelector('#btn-delete')!.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    if (!confirm(`Delete session "${store.currentSessionId}"?`)) return;
    await api.deleteSession(store.currentSessionId).catch(console.error);
    store.currentSessionId = null;
    store.emit('currentSession');
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  });

  function renderList() {
    const sessions = store.sessions;
    const current = store.currentSessionId;
    const ids = new Set(sessions.map(s => s.id));
    const byParent = new Map<string, Session[]>();
    const roots: Session[] = [];
    for (const s of sessions) {
      const parent = s.parent_session_id;
      if (parent && ids.has(parent)) {
        const arr = byParent.get(parent) ?? [];
        arr.push(s);
        byParent.set(parent, arr);
      } else {
        roots.push(s);
      }
    }
    for (const arr of byParent.values()) {
      arr.sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''));
    }

    function renderSession(s: Session, depth: number): string {
      const tone = sessionTone(s);
      const color = toneColor(tone);
      const active = s.id === current ? ' active' : '';
      const childClass = depth > 0 ? ' child' : '';
      const agentLabel = s.agent.replace(/^agenthub\//, '');
      const displayLabel = (s.display_name && s.display_name.trim()) ? s.display_name : s.id;
      const indent = depth > 0 ? `<span class="session-indent">↳</span>` : '';
      const own = `
        <div class="session-item${active}${childClass}" data-id="${esc(s.id)}" data-depth="${depth}">
          ${indent}
          <span class="session-dot" style="background:${color}"></span>
          <span class="session-item-info">
            <span class="session-item-name">${esc(displayLabel)}</span>
            <span class="session-item-agent">${esc(agentLabel)}</span>
          </span>
        </div>
      `;
      const kids = (byParent.get(s.id) ?? []).map(c => renderSession(c, depth + 1)).join('');
      return own + kids;
    }

    listItemsEl.innerHTML = roots.map(s => renderSession(s, 0)).join('')
      || '<div class="session-list-empty">No sessions</div>';
    listItemsEl.querySelectorAll<HTMLElement>('.session-item').forEach(item => {
      item.addEventListener('click', () => {
        const id = item.dataset.id;
        if (id) attachSession(id);
      });
    });
  }

  store.on('sessions', renderList);
  store.on('currentSession', renderList);
  renderList();
  ensureAgents();
  return el;
}

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
