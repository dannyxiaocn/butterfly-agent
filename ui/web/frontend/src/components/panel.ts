import { store } from '../store';
import { api } from '../api';
import type { Params, TaskCard } from '../types';
import { formatInterval, formatRelative } from '../markdown';
import { renderTaskEditor } from './taskEditor';

type PanelTab = 'tasks' | 'config';

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function createPanel(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'panel';

  let activeTab: PanelTab = 'tasks';
  let editingTask: TaskCard | null | 'new' = null; // null = not editing, 'new' = new task
  let editingConfig = false;

  function render() {
    if (!store.currentSessionId) {
      el.innerHTML = `<div class="panel-empty">Select a session</div>`;
      return;
    }

    const tabsHtml = `
      <div class="panel-tabs">
        <button class="panel-tab${activeTab === 'tasks' ? ' active' : ''}" data-tab="tasks">Tasks</button>
        <button class="panel-tab${activeTab === 'config' ? ' active' : ''}" data-tab="config">Config</button>
      </div>
    `;

    let contentHtml = '';

    if (activeTab === 'tasks') {
      contentHtml = renderTasksTab();
    } else {
      contentHtml = renderConfigTab();
    }

    el.innerHTML = `
      ${tabsHtml}
      <div class="panel-content" id="panel-content">
        ${contentHtml}
      </div>
    `;

    // Bind tab buttons
    el.querySelectorAll('.panel-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        activeTab = (btn as HTMLElement).dataset.tab as PanelTab;
        editingTask = null;
        editingConfig = false;
        render();
      });
    });

    // Bind task actions
    if (activeTab === 'tasks') {
      bindTasksTab();
    } else {
      bindConfigTab();
    }
  }

  function renderTasksTab(): string {
    if (editingTask !== null) return ''; // replaced by editor below

    const cards = store.taskCards;
    if (!cards.length) {
      return `
        <div class="tasks-empty">No task cards yet.</div>
        <button class="btn-sm btn-primary" id="btn-new-task">+ New Task</button>
      `;
    }

    const cardsHtml = cards.map(card => renderTaskCard(card)).join('');
    return `
      <div class="task-cards">${cardsHtml}</div>
      <div class="tasks-footer">
        <button class="btn-sm btn-primary" id="btn-new-task">+ New Task</button>
        <button class="btn-sm" id="btn-refresh-tasks">↻ Refresh</button>
      </div>
    `;
  }

  function renderTaskCard(card: TaskCard): string {
    const isHb = card.name === 'heartbeat';
    const hbPill = isHb ? `<span class="hb-pill">heartbeat</span>` : '';
    const intervalStr = formatInterval(card.interval);
    const lastRun = formatRelative(card.last_run_at);
    const statusClass = `task-status-${card.status}`;

    // Content preview: first 3 non-empty lines
    const preview = card.content.split('\n').filter(l => l.trim()).slice(0, 3).join('\n');

    return `
      <div class="task-card" data-name="${escHtml(card.name)}">
        <div class="task-card-header">
          <span class="task-name">${escHtml(card.name)}</span>
          ${hbPill}
          <span class="task-status-badge ${statusClass}">${card.status}</span>
        </div>
        <div class="task-card-meta">
          <span class="task-interval">${escHtml(intervalStr)}</span>
          ${card.starts_at || card.ends_at ? `<span class="task-window">window: ${escHtml(card.starts_at ?? '∞')} → ${escHtml(card.ends_at ?? '∞')}</span>` : ''}
          <span class="task-last-run">last run: ${escHtml(lastRun)}</span>
        </div>
        ${preview ? `<div class="task-preview">${escHtml(preview)}</div>` : ''}
        <div class="task-card-actions">
          <button class="btn-sm btn-edit" data-name="${escHtml(card.name)}">Edit</button>
        </div>
      </div>
    `;
  }

  function bindTasksTab() {
    el.querySelector('#btn-new-task')?.addEventListener('click', () => {
      editingTask = 'new';
      showTaskEditor(null);
    });

    el.querySelector('#btn-refresh-tasks')?.addEventListener('click', async () => {
      if (store.currentSessionId) {
        const res = await api.getTasks(store.currentSessionId).catch(console.error);
        if (res) {
          store.taskCards = res.cards;
          store.emit('tasks');
          render();
        }
      }
    });

    el.querySelectorAll('.btn-edit').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = (btn as HTMLElement).dataset.name;
        const card = store.taskCards.find(c => c.name === name) ?? null;
        editingTask = card;
        showTaskEditor(card);
      });
    });
  }

  function showTaskEditor(card: TaskCard | null) {
    const content = el.querySelector('#panel-content');
    if (!content || !store.currentSessionId) return;
    const sessionId = store.currentSessionId;

    const editor = renderTaskEditor(card, sessionId, async () => {
      editingTask = null;
      // Refresh tasks
      const res = await api.getTasks(sessionId).catch(console.error);
      if (res) {
        store.taskCards = res.cards;
        store.emit('tasks');
      }
      render();
    });

    content.innerHTML = '';
    content.appendChild(editor);
  }

  function renderConfigTab(): string {
    if (editingConfig) return ''; // replaced by textarea below

    const params = store.currentParams;
    if (!params) return '<div class="config-empty">No config loaded.</div>';

    const excluded = new Set(['default_task', 'is_meta_session']);
    const rows = Object.entries(params)
      .filter(([k]) => !excluded.has(k))
      .map(([k, v]) => {
        let displayVal: string;
        if (v === null || v === undefined) {
          displayVal = '<span class="cfg-null">— (default)</span>';
        } else if (typeof v === 'object') {
          displayVal = `<code>${escHtml(JSON.stringify(v))}</code>`;
        } else if (typeof v === 'boolean') {
          displayVal = v ? '<span class="cfg-true">true</span>' : '<span class="cfg-false">false</span>';
        } else {
          displayVal = escHtml(String(v));
        }
        return `<tr><td class="cfg-key">${escHtml(k)}</td><td class="cfg-val">${displayVal}</td></tr>`;
      })
      .join('');

    return `
      <table class="config-table">
        <tbody>${rows}</tbody>
      </table>
      <div class="config-footer">
        <button class="btn-sm btn-primary" id="btn-edit-config">Edit JSON</button>
      </div>
    `;
  }

  function bindConfigTab() {
    el.querySelector('#btn-edit-config')?.addEventListener('click', () => {
      editingConfig = true;
      showConfigEditor();
    });
  }

  function showConfigEditor() {
    const content = el.querySelector('#panel-content');
    if (!content || !store.currentSessionId) return;
    const sessionId = store.currentSessionId;
    const params = store.currentParams;

    const textarea = document.createElement('textarea');
    textarea.className = 'config-json-editor';
    textarea.rows = 20;
    textarea.value = JSON.stringify(params, null, 2);

    const errorEl = document.createElement('div');
    errorEl.className = 'form-error hidden';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn-primary';
    saveBtn.textContent = 'Save';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn-sm';
    cancelBtn.textContent = 'Cancel';

    saveBtn.addEventListener('click', async () => {
      try {
        const parsed = JSON.parse(textarea.value) as Params;
        const res = await api.setConfig(sessionId, parsed);
        store.currentParams = res.params;
        store.emit('config');
        editingConfig = false;
        render();
      } catch (e) {
        errorEl.textContent = `Error: ${e}`;
        errorEl.classList.remove('hidden');
      }
    });

    cancelBtn.addEventListener('click', () => {
      editingConfig = false;
      render();
    });

    const actions = document.createElement('div');
    actions.className = 'form-row';
    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);

    content.innerHTML = '';
    content.appendChild(textarea);
    content.appendChild(errorEl);
    content.appendChild(actions);
  }

  store.on('tasks', () => {
    if (activeTab === 'tasks' && editingTask === null) render();
  });
  store.on('config', () => {
    if (activeTab === 'config' && !editingConfig) render();
  });
  store.on('currentSession', () => {
    editingTask = null;
    editingConfig = false;
    render();
  });

  render();
  return el;
}


