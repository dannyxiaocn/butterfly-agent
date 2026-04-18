import { store } from '../store';
import { sessionTone, toneColor, toneLabel } from '../types';

export function createHeader(): HTMLElement {
  const el = document.createElement('header');
  el.id = 'header';

  function render() {
    const sess = store.currentSession;
    const anyAlive = store.sessions.some(s => s.pid_alive);
    const wx = store.weixinStatus;

    const serverDot = anyAlive ? 'var(--green)' : 'var(--red)';
    const serverLabel = anyAlive ? 'server online' : 'server offline';

    let wxIcon = '≈';
    let wxLabel = 'WeChat';
    let wxColor = 'var(--muted)';
    switch (wx.status) {
      case 'running':
        wxIcon = '⇄'; wxLabel = 'WeChat'; wxColor = 'var(--green)'; break;
      case 'no_account':
        wxIcon = '×'; wxLabel = 'WeChat unavailable'; wxColor = 'var(--red)'; break;
      case 'error':
        wxIcon = '!'; wxLabel = 'WeChat error'; wxColor = 'var(--red)'; break;
      case 'idle':
        wxIcon = '≈'; wxLabel = 'WeChat'; wxColor = 'var(--muted)'; break;
      case 'stopped':
        wxIcon = '∥'; wxLabel = 'WeChat paused'; wxColor = 'var(--yellow)'; break;
    }

    let sessionInfo = '';
    if (sess) {
      const tone = sessionTone(sess);
      const color = toneColor(tone);
      const label = toneLabel(tone);
      const displayLabel = (sess.display_name && sess.display_name.trim()) ? sess.display_name : sess.id;
      const headerTitle = displayLabel === sess.id ? sess.id : `${displayLabel} · ${sess.id}`;
      sessionInfo = `
        <div class="header-session" title="${escHtml(headerTitle)}">
          <span class="session-name">${escHtml(displayLabel)}</span>
          <span class="status-pill" style="background:${color}22;color:${color};border-color:${color}44">
            <span class="dot" style="background:${color}"></span>${label}
          </span>
        </div>
      `;
    }

    el.innerHTML = `
      <div class="header-left">
        <span class="logo">🦋 butterfly</span>
        <span class="indicator" title="${escHtml(serverLabel)}">
          <span class="dot" style="background:${serverDot}"></span>
          <span>${escHtml(serverLabel)}</span>
        </span>
        <span class="indicator wx-indicator" title="${escHtml(wx.error ?? wx.session ?? '')}" style="color:${wxColor}">
          <span>${wxIcon}</span>
          <span>${escHtml(wxLabel)}</span>
        </span>
      </div>
      <div class="header-right">
        ${sessionInfo}
      </div>
    `;
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
