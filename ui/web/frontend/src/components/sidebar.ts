import { api } from '../api';
import { store } from '../store';
import { Session, sessionTone, toneColor } from '../types';
import { attachSession } from '../main';

export function createSidebar(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'sidebar';

  let formVisible = false;
  let agentOptions: string[] | null = null;
  let agentOptionsPromise: Promise<string[]> | null = null;
  let selectedAgent = 'agent';

  // Build the sidebar shell ONCE. The form input + static buttons live in
  // persistent DOM nodes so `store.emit('sessions'/'currentSession'/'weixin')`
  // (which fires on every task/session event in v2.0.32) only rebuilds the
  // dynamic list area. Previously the whole aside re-ran `innerHTML = ...`,
  // destroying the <input> mid-keystroke — the cursor reset to column 0 on
  // every character and typing a session name was impossible.
  el.innerHTML = `
    <div class="sidebar-header">
      <span class="sidebar-title">Sessions</span>
      <button class="btn-icon" id="btn-new-session" title="New session">+</button>
    </div>
    <div class="session-list">
      <div id="new-session-form" class="new-session-card hidden">
        <input
          id="ns-display-name"
          class="ns-name-input"
          type="text"
          placeholder="Enter session name…"
          maxlength="40"
          autocomplete="off"
        />
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
      <button class="btn-sm btn-start" id="btn-start" title="Resume session">▶ Start</button>
      <button class="btn-sm btn-stop" id="btn-stop" title="Pause session">⏸ Stop</button>
      <button class="btn-sm btn-danger" id="btn-delete" title="Delete session">🗑</button>
    </div>
  `;

  const formEl = el.querySelector('#new-session-form') as HTMLDivElement;
  const nameInput = el.querySelector('#ns-display-name') as HTMLInputElement;
  const agentSelect = el.querySelector('#ns-agent') as HTMLSelectElement;
  const listItemsEl = el.querySelector('#session-list-items') as HTMLDivElement;

  function renderAgentOptions(): string {
    if (agentOptions === null) return '<option value="">Loading…</option>';
    if (!agentOptions.length) return '<option value="">(no agents found)</option>';
    // Entries whose name starts with `create_` are agenthub "creator" agents
    // (e.g. `create_team`, `create_workflow`) — interactive sessions that
    // interview the user and author a new team/workflow on disk. Group them
    // at the top with a friendly "Create <thing>" label so they read like a
    // session-type chooser rather than just another agent name.
    const creators = agentOptions.filter(a => a.startsWith('create_'));
    const regular = agentOptions.filter(a => !a.startsWith('create_'));
    const opt = (a: string, label?: string) =>
      `<option value="${escHtml(a)}">${escHtml(label ?? a)}</option>`;
    const creatorLabel = (a: string) => {
      const rest = a.slice('create_'.length).replace(/_/g, ' ');
      return rest ? `Create ${rest}` : a;
    };
    const out: string[] = [];
    if (creators.length) {
      out.push('<optgroup label="Create new">');
      for (const a of creators) out.push(opt(a, creatorLabel(a)));
      out.push('</optgroup>');
    }
    if (regular.length) {
      out.push('<optgroup label="Agents">');
      for (const a of regular) out.push(opt(a));
      out.push('</optgroup>');
    }
    return out.join('');
  }

  function updateAgentOptions() {
    const prev = agentSelect.value;
    agentSelect.innerHTML = renderAgentOptions();
    if (agentOptions && agentOptions.includes(prev)) {
      agentSelect.value = prev;
    } else if (agentOptions && agentOptions.includes(selectedAgent)) {
      agentSelect.value = selectedAgent;
    }
  }

  function ensureAgents(): Promise<string[]> {
    if (agentOptions) return Promise.resolve(agentOptions);
    if (agentOptionsPromise) return agentOptionsPromise;
    agentOptionsPromise = api.listAgents()
      .then(r => {
        agentOptions = r.agents;
        if (!agentOptions.includes(selectedAgent) && agentOptions.length) {
          selectedAgent = agentOptions[0];
        }
        updateAgentOptions();
        return r.agents;
      })
      .catch(e => {
        console.error('listAgents failed:', e);
        agentOptions = null;
        agentOptionsPromise = null;
        return [];
      });
    return agentOptionsPromise;
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
    // Agent dropdown (PR #36) holds either "agent" or "agenthub/agent" —
    // normalize to the fully-qualified form the service expects.
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
    if (formVisible) closeForm();
    else openForm();
  });
  el.querySelector('#ns-cancel')!.addEventListener('click', closeForm);
  el.querySelector('#ns-create')!.addEventListener('click', submitCreate);

  // Enter submits, Escape cancels — keep keyboard flow fast since the form
  // auto-focuses on open.
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitCreate();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closeForm();
    }
  });

  agentSelect.addEventListener('change', () => {
    selectedAgent = agentSelect.value;
  });

  el.querySelector('#btn-start')!.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    await api.startSession(store.currentSessionId).catch(console.error);
    const sessions = await api.listSessions();
    store.sessions = sessions;
    store.emit('sessions');
  });

  el.querySelector('#btn-stop')!.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    // v2.0.24: Stop must also fire interrupt. ``stop_session`` alone only
    // flips ``status=stopped`` on disk; the daemon's stopped-check runs
    // when new input arrives, so any in-flight agent loop kept executing
    // until it hit a natural break. Sending interrupt first cancels the
    // run + drops the inbox + cascades to background tools / sub-agents
    // (see Session._handle_explicit_interrupt), then stop pauses the
    // session so future task wakeups don't auto-resume work.
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

    const weixinSession = store.weixinStatus.status === 'running' ? (store.weixinStatus.session ?? null) : null;

    // Group by parent_session_id so children render indented under their
    // parent (markdown-list style). Orphans (parent missing from current
    // list) fall back to root so they remain reachable.
    const byParent = new Map<string, Session[]>();
    const ids = new Set(sessions.map(s => s.id));
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
    // Stable child order: oldest first so newer sub-agents fall to the bottom.
    for (const arr of byParent.values()) {
      arr.sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''));
    }

    function renderSession(s: Session, depth: number): string {
      const tone = sessionTone(s);
      const color = toneColor(tone);
      const active = s.id === current ? ' active' : '';
      const isRunning = tone === 'running' && s.id === current;
      const pulseClass = isRunning ? ' running-pulse' : '';
      const dotPulse = tone === 'running' ? ' pulse' : '';
      const childClass = depth > 0 ? ' child' : '';
      const agentLabel = s.agent.replace(/^agenthub\//, '');
      const isWeixinLinked = s.id === weixinSession;
      const dotHtml = isWeixinLinked
        ? `<span class="session-dot weixin-dot" title="WeChat linked">⇄</span>`
        : `<span class="session-dot${dotPulse}" style="background:${color}"></span>`;
      // Color-code the mode chip so explorer vs executor is immediately
      // scannable in a tree of many sub-agents. Unknown values fall back to
      // the neutral base chip.
      const modeClass = s.mode === 'explorer' || s.mode === 'executor'
        ? ` mode-${s.mode}`
        : '';
      const modeChip = s.mode
        ? `<span class="session-mode-chip${modeClass}" title="sub-agent mode">${escHtml(s.mode)}</span>`
        : '';
      const indent = depth > 0
        ? `<span class="session-indent" aria-hidden="true">↳</span>`
        : '';
      // Prefer the user-facing display_name (set by the new-session form or
      // by the sub_agent tool). Fall back to the raw session_id so unnamed
      // sessions still render. Tooltip carries both so the canonical id is
      // always discoverable.
      const displayLabel = (s.display_name && s.display_name.trim()) ? s.display_name : s.id;
      const tooltip = displayLabel === s.id
        ? `${s.id} · ${s.agent}`
        : `${displayLabel} · ${s.id} · ${s.agent}`;
      const own = `
        <div class="session-item${active}${pulseClass}${childClass}" data-id="${escHtml(s.id)}" data-depth="${depth}" title="${escHtml(tooltip)}">
          ${indent}
          ${dotHtml}
          <span class="session-item-info">
            <span class="session-item-name">${escHtml(displayLabel)}${modeChip}</span>
            <span class="session-item-agent">${escHtml(agentLabel)}</span>
          </span>
        </div>
      `;
      const kids = (byParent.get(s.id) ?? [])
        .map(child => renderSession(child, depth + 1))
        .join('');
      return own + kids;
    }

    const listHtml = roots.map(s => renderSession(s, 0)).join('');
    listItemsEl.innerHTML = listHtml || '<div style="padding:12px 8px;font-size:12px;color:var(--dimmed)">No sessions</div>';

    listItemsEl.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', () => {
        const id = (item as HTMLElement).dataset.id;
        if (id) attachSession(id);
      });
    });
  }

  store.on('sessions', renderList);
  store.on('currentSession', renderList);
  store.on('weixin', renderList);
  renderList();
  // Warm the agents cache so the "+ New session" dropdown is pre-populated.
  ensureAgents();
  return el;
}

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
