import { api } from '../api';
import { store } from '../store';
import { Session, sessionTone, toneColor } from '../types';
import { attachSession } from '../main';

export function createSidebar(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'sidebar';

  // Persist new-session form state across re-renders. `render()` rebuilds
  // the whole sidebar DOM via innerHTML on every sessions/weixin poll
  // (3-5 s cadence), which would otherwise wipe the agent input back to its
  // default and make "type → create" impossible (Bug 5). We snapshot the
  // current values + focus before rebuilding and restore them after.
  let formVisible = false;
  let formState: { id: string; agent: string; focus: string | null } = {
    id: '',
    agent: 'agenthub/agent',
    focus: null,
  };

  function render() {
    const sessions = store.sessions;
    const current = store.currentSessionId;

    const weixinSession = store.weixinStatus.status === 'running' ? (store.weixinStatus.session ?? null) : null;

    // Snapshot in-DOM state before we blow it away — scroll position of the
    // session list (Bug 6) and form inputs (Bug 5).
    const prevList = el.querySelector('#session-list') as HTMLElement | null;
    const prevScrollTop = prevList ? prevList.scrollTop : 0;
    const prevIdInput = el.querySelector('#ns-id') as HTMLInputElement | null;
    const prevAgentInput = el.querySelector('#ns-agent') as HTMLInputElement | null;
    const activeEl = document.activeElement;
    if (prevIdInput) formState.id = prevIdInput.value;
    if (prevAgentInput) formState.agent = prevAgentInput.value;
    if (activeEl === prevIdInput) formState.focus = 'id';
    else if (activeEl === prevAgentInput) formState.focus = 'agent';
    else formState.focus = null;
    const prevIdSel = prevIdInput ? [prevIdInput.selectionStart, prevIdInput.selectionEnd] : null;
    const prevAgentSel = prevAgentInput ? [prevAgentInput.selectionStart, prevAgentInput.selectionEnd] : null;

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
      const own = `
        <div class="session-item${active}${pulseClass}${childClass}" data-id="${escHtml(s.id)}" data-depth="${depth}" title="${escHtml(s.id)} · ${escHtml(s.agent)}">
          ${indent}
          ${dotHtml}
          <span class="session-item-info">
            <span class="session-item-name">${escHtml(s.id)}${modeChip}</span>
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
          <label>Session ID</label>
          <input id="ns-id" type="text" placeholder="my-session (optional)" value="${escHtml(formState.id)}" />
        </div>
        <div class="form-field">
          <label>Agent</label>
          <input id="ns-agent" type="text" value="${escHtml(formState.agent || 'agenthub/agent')}" />
        </div>
        <div class="form-row">
          <button class="btn-sm btn-primary" id="ns-create">Create</button>
          <button class="btn-sm" id="ns-cancel">Cancel</button>
        </div>
      </div>
    `;

    // Restore scroll + focus so the 3-5 s background poll doesn't yank the
    // user's place in the session list or steal the cursor out of a form
    // input mid-typing (Bug 5 + Bug 6).
    const nextList = el.querySelector('#session-list') as HTMLElement | null;
    if (nextList && prevScrollTop > 0) nextList.scrollTop = prevScrollTop;
    if (formVisible) {
      const newIdInput = el.querySelector('#ns-id') as HTMLInputElement | null;
      const newAgentInput = el.querySelector('#ns-agent') as HTMLInputElement | null;
      // Wire input → state so each keystroke is captured even if the next
      // render fires before the user tabs away.
      newIdInput?.addEventListener('input', () => { formState.id = newIdInput.value; });
      newAgentInput?.addEventListener('input', () => { formState.agent = newAgentInput.value; });
      if (formState.focus === 'id' && newIdInput) {
        newIdInput.focus();
        if (prevIdSel && prevIdSel[0] != null && prevIdSel[1] != null) {
          newIdInput.setSelectionRange(prevIdSel[0], prevIdSel[1]);
        }
      } else if (formState.focus === 'agent' && newAgentInput) {
        newAgentInput.focus();
        if (prevAgentSel && prevAgentSel[0] != null && prevAgentSel[1] != null) {
          newAgentInput.setSelectionRange(prevAgentSel[0], prevAgentSel[1]);
        }
      }
    }

    // bind events
    el.querySelector('#btn-new-session')?.addEventListener('click', () => {
      formVisible = !formVisible;
      el.querySelector('#new-session-form')?.classList.toggle('hidden', !formVisible);
      if (formVisible) {
        const agentInput = el.querySelector('#ns-agent') as HTMLInputElement | null;
        agentInput?.focus();
        agentInput?.select();
      }
    });

    el.querySelector('#ns-cancel')?.addEventListener('click', () => {
      formVisible = false;
      formState = { id: '', agent: 'agenthub/agent', focus: null };
      el.querySelector('#new-session-form')?.classList.add('hidden');
    });

    el.querySelector('#ns-create')?.addEventListener('click', async () => {
      const idEl = el.querySelector('#ns-id') as HTMLInputElement;
      const agentEl = el.querySelector('#ns-agent') as HTMLInputElement;
      const body: { id?: string; agent: string } = {
        agent: agentEl.value.trim() || 'agenthub/agent',
      };
      if (idEl.value.trim()) body.id = idEl.value.trim();
      try {
        const res = await api.createSession(body);
        formVisible = false;
        formState = { id: '', agent: 'agenthub/agent', focus: null };
        el.querySelector('#new-session-form')?.classList.add('hidden');
        idEl.value = '';
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
  return el;
}

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
