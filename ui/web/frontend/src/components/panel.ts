// Right-column panel: Tasks, Panel (bg work), Config. Each tab is a
// small render function over store.tasks / store.panel / store.currentParams.
// Native <details> handles expansion — no explicit expanded-state Set.
import { api } from '../api';
import { store } from '../store';
import type { TaskCard, TodoListSnapshot, PanelEntry, Params } from '../types';
import { escapeHtml, renderMarkdown, formatInterval, formatRelative } from '../markdown';
import { renderTaskEditor } from './taskEditor';
import { terminalController } from './terminal';

type PanelTab = 'tasks' | 'panel' | 'terminal' | 'config';

export function createPanel(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'panel';

  let activeTab: PanelTab = 'tasks';
  let editingTask: TaskCard | null | 'new' = null;

  function render() {
    if (!store.currentSessionId) {
      el.innerHTML = `<div class="panel-empty">Select a session</div>`;
      return;
    }
    el.innerHTML = `
      <div class="panel-tabs">
        ${tabBtn('tasks', 'Tasks')}
        ${tabBtn('panel', 'Panel')}
        ${tabBtn('terminal', 'Terminal')}
        ${tabBtn('config', 'Config')}
      </div>
      <div class="panel-content" id="panel-content"></div>
    `;
    el.querySelectorAll<HTMLButtonElement>('.panel-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        activeTab = btn.dataset.tab as PanelTab;
        render();
      });
    });
    renderActiveTab();
  }

  function tabBtn(tab: PanelTab, label: string): string {
    const active = activeTab === tab ? ' active' : '';
    return `<button class="panel-tab${active}" data-tab="${tab}">${escapeHtml(label)}</button>`;
  }

  function renderActiveTab() {
    const content = el.querySelector('#panel-content') as HTMLDivElement;
    if (!content) return;
    content.innerHTML = '';
    if (activeTab === 'tasks') renderTasks(content);
    else if (activeTab === 'panel') renderPanel(content);
    else if (activeTab === 'terminal') renderTerminal(content);
    else renderConfig(content);
  }

  // ── Tasks tab ─────────────────────────────────────────────────────────

  function renderTasks(content: HTMLDivElement) {
    if (editingTask !== null) {
      const card = editingTask === 'new' ? null : editingTask;
      const editor = renderTaskEditor(card, store.currentSessionId!, () => {
        editingTask = null;
        // Re-fetch tasks so edits are reflected.
        api.getTasks(store.currentSessionId!).then(r => {
          store.tasks = r.cards;
          store.emit('tasks');
        }).catch(() => {});
      });
      content.appendChild(editor);
      return;
    }
    const todoHtml = renderTodoList(store.todoList);
    const tasks = store.tasks ?? [];
    const cardsHtml = tasks.length
      ? tasks.map(renderTaskCardSummary).join('')
      : `<div class="panel-empty-hint">No task cards yet</div>`;
    content.innerHTML = `
      ${todoHtml}
      <div class="panel-header-row">
        <span class="panel-section-title">Task cards</span>
        <button class="btn-sm btn-primary" id="btn-new-task">+ New</button>
      </div>
      <div class="task-cards">${cardsHtml}</div>
    `;
    content.querySelector('#btn-new-task')?.addEventListener('click', () => {
      editingTask = 'new';
      renderActiveTab();
    });
    content.querySelectorAll<HTMLElement>('[data-task-edit]').forEach(el => {
      el.addEventListener('click', () => {
        const name = el.dataset.taskEdit!;
        editingTask = (store.tasks ?? []).find(t => t.name === name) ?? null;
        renderActiveTab();
      });
    });
  }

  function renderTaskCardSummary(t: TaskCard): string {
    const status = t.status;
    const interval = formatInterval(t.check_interval);
    const last = formatRelative(t.last_checked_at);
    const desc = t.description ? renderMarkdown(t.description) : '<em>(no description)</em>';
    return `
      <details class="card task-card" data-task="${escapeHtml(t.name)}">
        <summary>
          <span class="task-status status-${escapeHtml(status)}">${escapeHtml(status)}</span>
          <span class="task-name">${escapeHtml(t.name)}</span>
          <span class="task-meta">${escapeHtml(interval)} · last ${escapeHtml(last)}</span>
          <button class="btn-xs" data-task-edit="${escapeHtml(t.name)}">Edit</button>
        </summary>
        <div class="task-body">${desc}</div>
      </details>
    `;
  }

  function renderTodoList(todo: TodoListSnapshot | null): string {
    if (!todo || todo.total === 0) return '';
    const items = todo.items.map((i, idx) => `
      <li class="todo-item todo-${escapeHtml(i.status)}${idx === todo.active_index ? ' active' : ''}">
        <span class="todo-mark">${i.status === 'completed' ? '✓' : i.status === 'in_progress' ? '▸' : '·'}</span>
        <span class="todo-text">${escapeHtml(i.status === 'in_progress' ? i.activeForm : i.content)}</span>
      </li>
    `).join('');
    return `
      <details class="card todo-card" open>
        <summary>
          <span class="todo-line">${escapeHtml(todo.progress_line)}</span>
        </summary>
        <ol class="todo-list">${items}</ol>
      </details>
    `;
  }

  // ── Panel tab ─────────────────────────────────────────────────────────

  function renderPanel(content: HTMLDivElement) {
    const entries = store.panel ?? [];
    if (!entries.length) {
      content.innerHTML = `<div class="panel-empty-hint">No background work</div>`;
      return;
    }
    content.innerHTML = entries.map(renderPanelEntry).join('');
    content.querySelectorAll<HTMLButtonElement>('[data-kill-tid]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.preventDefault();
        const tid = btn.dataset.killTid!;
        try { await api.killPanelEntry(store.currentSessionId!, tid); } catch {}
      });
    });
  }

  function renderPanelEntry(e: PanelEntry): string {
    const hasKill = e.status === 'running';
    const input = safeJson(e.input);
    return `
      <details class="card panel-entry status-${escapeHtml(e.status)}">
        <summary>
          <span class="panel-entry-tool">${escapeHtml(e.tool_name)}</span>
          <span class="panel-entry-status">${escapeHtml(e.status)}</span>
          <span class="panel-entry-tid" title="tid">${escapeHtml(e.tid)}</span>
          ${hasKill ? `<button class="btn-xs btn-danger" data-kill-tid="${escapeHtml(e.tid)}">Kill</button>` : ''}
        </summary>
        <pre class="card-pre">${escapeHtml(input)}</pre>
      </details>
    `;
  }

  // ── Terminal tab ──────────────────────────────────────────────────────

  function renderTerminal(content: HTMLDivElement) {
    // Delegate to the retained terminalController — it owns its own DOM
    // shape via renderHtml() + rebind() (see terminal.ts docstring).
    content.innerHTML = terminalController.renderHtml();
    const root = content.querySelector('.terminal-container') as HTMLElement | null;
    if (root) terminalController.rebind(root);
  }

  // ── Config tab ────────────────────────────────────────────────────────

  function renderConfig(content: HTMLDivElement) {
    const p: Params | null = store.currentParams;
    if (!p) {
      content.innerHTML = `<div class="panel-empty-hint">Loading config…</div>`;
      return;
    }
    // Simple read-only summary. Power users edit via CLI (DESIGN.md §12
    // non-goals: config editor is CLI-only in the refactor).
    const rows = Object.entries(p)
      .filter(([k]) => !k.startsWith('_'))
      .map(([k, v]) => `
        <tr>
          <td class="config-key">${escapeHtml(k)}</td>
          <td class="config-val">${escapeHtml(formatConfigVal(v))}</td>
        </tr>`)
      .join('');
    content.innerHTML = `
      <div class="config-view">
        <div class="panel-section-title">Config (read-only — edit via CLI)</div>
        <table class="config-table"><tbody>${rows}</tbody></table>
      </div>
    `;
  }

  function formatConfigVal(v: unknown): string {
    if (v === null) return '—';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'object') {
      try { return JSON.stringify(v); } catch { return String(v); }
    }
    return String(v);
  }

  function safeJson(v: unknown): string {
    try { return JSON.stringify(v, null, 2); } catch { return String(v); }
  }

  // Signals → re-render the relevant tab only.
  store.on('tasks', () => { if (activeTab === 'tasks') renderActiveTab(); });
  store.on('todoList', () => { if (activeTab === 'tasks') renderActiveTab(); });
  store.on('panel', () => { if (activeTab === 'panel') renderActiveTab(); });
  store.on('config', () => { if (activeTab === 'config') renderActiveTab(); });
  store.on('currentSession', render);
  store.on('terminal', () => { if (activeTab === 'terminal') renderActiveTab(); });

  render();
  return el;
}
