// Top bar: logo + session info + weixin / server indicators.
import { store } from '../store';
import { sessionTone, toneColor, toneLabel } from '../types';

export function createHeader(): HTMLElement {
  const el = document.createElement('header');
  el.id = 'header';

  function render() {
    const sess = store.currentSession;
    const anyAlive = store.sessions.some(s => s.pid_alive);
    const wx = store.weixinStatus;

    const serverColor = anyAlive ? 'var(--green)' : 'var(--red)';
    const serverLabel = anyAlive ? 'server online' : 'server offline';

    let wxColor = 'var(--muted)';
    let wxLabel = 'WeChat';
    switch (wx.status) {
      case 'running': wxColor = 'var(--green)'; wxLabel = 'WeChat'; break;
      case 'error': wxColor = 'var(--red)'; wxLabel = 'WeChat error'; break;
      case 'stopped': wxColor = 'var(--yellow)'; wxLabel = 'WeChat paused'; break;
    }

    let sessInfo = '';
    if (sess) {
      const tone = sessionTone(sess);
      const color = toneColor(tone);
      const label = toneLabel(tone);
      const displayLabel = (sess.display_name && sess.display_name.trim()) ? sess.display_name : sess.id;
      sessInfo = `
        <div class="header-session">
          <span class="session-name">${esc(displayLabel)}</span>
          <span class="status-pill" style="background:${color}22;color:${color};border-color:${color}44">
            <span class="dot" style="background:${color}"></span>${esc(label)}
          </span>
        </div>
      `;
    }

    el.innerHTML = `
      <div class="header-left">
        <span class="logo">🦋 butterfly</span>
        <span class="indicator" title="${esc(serverLabel)}">
          <span class="dot" style="background:${serverColor}"></span>
          <span>${esc(serverLabel)}</span>
        </span>
        <span class="indicator" style="color:${wxColor}">
          <span>${esc(wxLabel)}</span>
        </span>
      </div>
      <div class="header-right">${sessInfo}</div>
    `;
  }

  store.on('sessions', render);
  store.on('currentSession', render);
  store.on('weixin', render);
  render();
  return el;
}

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
