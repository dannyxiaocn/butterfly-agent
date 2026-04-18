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

  function renderAgentOptions(): string {
    if (agentOptions === null) return '<option value="">Loading…</option>';
    if (!agentOptions.length) return '<option value="">(no agents found)</option>';
    return agentOptions
      .map(a => `<option value="${escHtml(a)}">${escHtml(a)}</option>`)
      .join('');
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
        // Re-render so the dropdown surfaces the fetched options even if
        // the sidebar was mid-rebuild when the promise resolved.
        render();
        return r.agents;
      })
      .catch(e => {
        console.error('listAgents failed:', e);
        // Clear both caches so the next `+` click (or next render) retries
        // rather than sticking on the failed state forever.
        agentOptions = null;
        agentOptionsPromise = null;
        return [];
      });
    return agentOptionsPromise;
  }

  function render() {
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
      const modeChip = s.mode
        ? `<span class="session-mode-chip" title="sub-agent mode">${escHtml(s.mode)}</span>`
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

    el.innerHTML = `
      <div class="sidebar-header">
        <span class="sidebar-title">Sessions</span>
        <button class="btn-icon" id="btn-new-session" title="New session">+</button>
      </div>
      <div class="session-list" id="session-list">
        ${listHtml || '<div style="padding:12px 8px;font-size:12px;color:var(--dimmed)">No sessions</div>'}
      </div>
      <div class="sidebar-footer">
        <button class="btn-sm btn-start" id="btn-start" title="Resume session">▶ Start</button>
        <button class="btn-sm btn-stop" id="btn-stop" title="Pause session">⏸ Stop</button>
        <button class="btn-sm btn-danger" id="btn-delete" title="Delete session">🗑</button>
      </div>
      <div id="new-session-form" class="new-session-form${formVisible ? '' : ' hidden'}">
        <div class="form-field">
          <label>Display name</label>
          <input id="ns-display-name" type="text" placeholder="e.g. audit auth flow (optional)" maxlength="40" />
        </div>
        <div class="form-field">
          <label>Agent</label>
          <select id="ns-agent">${renderAgentOptions()}</select>
        </div>
        <div class="form-row">
          <button class="btn-sm btn-primary" id="ns-create">Create</button>
          <button class="btn-sm" id="ns-cancel">Cancel</button>
        </div>
      </div>
    `;

    // Set the select's current value after DOM insertion so the cached
    // choice survives each re-render (the sidebar refreshes on every
    // sessions poll).
    const select = el.querySelector('#ns-agent') as HTMLSelectElement | null;
    if (select && agentOptions && agentOptions.includes(selectedAgent)) {
      select.value = selectedAgent;
    }
    select?.addEventListener('change', () => {
      selectedAgent = select.value;
    });

    // bind events
    el.querySelector('#btn-new-session')?.addEventListener('click', () => {
      formVisible = !formVisible;
      el.querySelector('#new-session-form')?.classList.toggle('hidden', !formVisible);
      if (formVisible) ensureAgents();
    });

    el.querySelector('#ns-cancel')?.addEventListener('click', () => {
      formVisible = false;
      el.querySelector('#new-session-form')?.classList.add('hidden');
    });

    el.querySelector('#ns-create')?.addEventListener('click', async () => {
      const nameEl = el.querySelector('#ns-display-name') as HTMLInputElement;
      const agentEl = el.querySelector('#ns-agent') as HTMLSelectElement;
      // Agent dropdown (PR #36) holds either "agent" or "agenthub/agent" —
      // normalize to the fully-qualified form the service expects.
      const agentName = (agentEl.value || 'agent').trim();
      // session_id is always server-generated; the form only collects the
      // user-facing display_name + agent choice.
      const body: { agent: string; display_name?: string } = {
        agent: agentName.startsWith('agenthub/') ? agentName : `agenthub/${agentName}`,
      };
      const trimmedName = nameEl.value.trim();
      if (trimmedName) body.display_name = trimmedName;
      try {
        const res = await api.createSession(body);
        formVisible = false;
        el.querySelector('#new-session-form')?.classList.add('hidden');
        nameEl.value = '';
        // Refresh sessions list
        const sessions = await api.listSessions();
        store.sessions = sessions;
        store.emit('sessions');
        await attachSession(res.id);
      } catch (e) {
        alert(`Failed to create session: ${e}`);
      }
    });

    el.querySelector('#btn-start')?.addEventListener('click', async () => {
      if (!store.currentSessionId) return;
      await api.startSession(store.currentSessionId).catch(console.error);
      const sessions = await api.listSessions();
      store.sessions = sessions;
      store.emit('sessions');
    });

    el.querySelector('#btn-stop')?.addEventListener('click', async () => {
      if (!store.currentSessionId) return;
      await api.stopSession(store.currentSessionId).catch(console.error);
      const sessions = await api.listSessions();
      store.sessions = sessions;
      store.emit('sessions');
    });

    el.querySelector('#btn-delete')?.addEventListener('click', async () => {
      if (!store.currentSessionId) return;
      if (!confirm(`Delete session "${store.currentSessionId}"?`)) return;
      await api.deleteSession(store.currentSessionId).catch(console.error);
      store.currentSessionId = null;
      store.emit('currentSession');
      const sessions = await api.listSessions();
      store.sessions = sessions;
      store.emit('sessions');
    });

    el.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', () => {
        const id = (item as HTMLElement).dataset.id;
        if (id) attachSession(id);
      });
    });
  }

  store.on('sessions', render);
  store.on('currentSession', render);
  store.on('weixin', render);
  render();
  // Warm the agents cache so the "+ New session" dropdown is pre-populated.
  ensureAgents();
  return el;
}

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
