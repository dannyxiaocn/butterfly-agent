import { api } from '../api';
import { store } from '../store';
import { sessionTone, toneColor } from '../types';
import { attachSession } from '../main';

export function createSidebar(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'sidebar';

  function render() {
    const sessions = store.sessions;
    const current = store.currentSessionId;

    const weixinSession = store.weixinStatus.status === 'running' ? (store.weixinStatus.session ?? null) : null;
    const listHtml = sessions.map(s => {
      const tone = sessionTone(s);
      const color = toneColor(tone);
      const active = s.id === current ? ' active' : '';
      const isRunning = tone === 'running' && s.id === current;
      const pulseClass = isRunning ? ' running-pulse' : '';
      const dotPulse = tone === 'running' ? ' pulse' : '';
      // Derive a short entity label
      const entityLabel = s.entity.replace(/^entity\//, '');
      const isWeixinLinked = s.id === weixinSession;
      // Weixin-linked session: replace breathing dot with solid green ⇄ symbol
      const dotHtml = isWeixinLinked
        ? `<span class="session-dot weixin-dot" title="WeChat linked">⇄</span>`
        : `<span class="session-dot${dotPulse}" style="background:${color}"></span>`;
      return `
        <div class="session-item${active}${pulseClass}" data-id="${escHtml(s.id)}" title="${escHtml(s.id)} · ${escHtml(s.entity)}">
          ${dotHtml}
          <span class="session-item-info">
            <span class="session-item-name">${escHtml(s.id)}</span>
            <span class="session-item-entity">${escHtml(entityLabel)}</span>
          </span>
        </div>
      `;
    }).join('');

    el.innerHTML = `
      <div class="sidebar-header">
        <span class="sidebar-title">Sessions</span>
        <button class="btn-icon" id="btn-new-session" title="New session">+</button>
      </div>
      <div class="session-list" id="session-list">
        ${listHtml || '<div style="padding:12px 8px;font-size:12px;color:var(--dimmed)">No sessions</div>'}
      </div>
      <div class="sidebar-footer">
        <button class="btn-sm btn-start" id="btn-start" title="Resume heartbeat">▶ Start</button>
        <button class="btn-sm btn-stop" id="btn-stop" title="Pause heartbeat">⏸ Stop</button>
        <button class="btn-sm btn-danger" id="btn-delete" title="Delete session">🗑</button>
      </div>
      <div id="new-session-form" class="new-session-form hidden">
        <div class="form-field">
          <label>Session ID</label>
          <input id="ns-id" type="text" placeholder="my-session (optional)" />
        </div>
        <div class="form-field">
          <label>Entity</label>
          <input id="ns-entity" type="text" value="entity/agent" />
        </div>
        <div class="form-field">
          <label>Heartbeat (s)</label>
          <input id="ns-heartbeat" type="number" value="7200" />
        </div>
        <div class="form-row">
          <button class="btn-sm btn-primary" id="ns-create">Create</button>
          <button class="btn-sm" id="ns-cancel">Cancel</button>
        </div>
      </div>
    `;

    // bind events
    el.querySelector('#btn-new-session')?.addEventListener('click', () => {
      el.querySelector('#new-session-form')?.classList.toggle('hidden');
    });

    el.querySelector('#ns-cancel')?.addEventListener('click', () => {
      el.querySelector('#new-session-form')?.classList.add('hidden');
    });

    el.querySelector('#ns-create')?.addEventListener('click', async () => {
      const idEl = el.querySelector('#ns-id') as HTMLInputElement;
      const entityEl = el.querySelector('#ns-entity') as HTMLInputElement;
      const hbEl = el.querySelector('#ns-heartbeat') as HTMLInputElement;
      const body: { id?: string; entity: string; heartbeat?: number } = {
        entity: entityEl.value.trim() || 'entity/agent',
        heartbeat: parseFloat(hbEl.value) || 7200,
      };
      if (idEl.value.trim()) body.id = idEl.value.trim();
      try {
        const res = await api.createSession(body);
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
