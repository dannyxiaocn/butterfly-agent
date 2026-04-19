import { api } from '../api';
import type { TaskCard } from '../types';
import { escapeHtml } from '../markdown';

export function renderTaskEditor(card: TaskCard | null, sessionId: string, onDone: () => void): HTMLElement {
  const isNew = card === null;

  const el = document.createElement('div');
  el.className = 'task-editor';

  const intervalVal = card?.check_interval ?? 3600;
  const triggerVal = card?.trigger_script ?? '#!/bin/bash\necho [start]\n';
  const endVal = card?.end_script ?? '';
  const statusOptions = ['pending', 'working', 'finished', 'paused']
    .map(s => `<option value="${s}"${(card?.status ?? 'pending') === s ? ' selected' : ''}>${s}</option>`)
    .join('');

  el.innerHTML = `
    <div class="task-editor-header">
      <strong>${isNew ? 'New Task' : `Edit: ${escapeHtml(card!.name)}`}</strong>
    </div>
    <div class="form-field">
      <label>Task name</label>
      <input id="te-name" type="text" value="${escapeHtml(card?.name ?? '')}" placeholder="task-name" />
    </div>
    <div class="form-field">
      <label>Status</label>
      <select id="te-status">${statusOptions}</select>
    </div>
    <div class="form-field">
      <label>Check interval (seconds — how often to run trigger_script)</label>
      <input id="te-interval" type="number" value="${intervalVal}" placeholder="3600" min="1" />
    </div>
    <div class="form-field">
      <label>Description</label>
      <textarea id="te-content" class="task-content-textarea" rows="5">${escapeHtml(card?.description ?? '')}</textarea>
    </div>
    <div class="form-field">
      <label>Trigger script (last line: <code>[start]</code> / <code>[start] &lt;msg&gt;</code> / <code>[skip]</code>)</label>
      <textarea id="te-trigger" class="task-content-textarea" rows="6">${escapeHtml(triggerVal)}</textarea>
    </div>
    <div class="form-field">
      <label>End script — optional (last line: <code>[done]</code> / <code>[not_done]</code>)</label>
      <textarea id="te-end" class="task-content-textarea" rows="4">${escapeHtml(endVal)}</textarea>
    </div>
    <div class="form-row task-editor-actions">
      <button class="btn-primary" id="te-save">Save</button>
      ${!isNew ? `<button class="btn-danger" id="te-delete">Delete</button>` : ''}
      <button class="btn-sm" id="te-cancel">Cancel</button>
    </div>
    <div id="te-error" class="form-error hidden"></div>
  `;

  const errorEl = el.querySelector('#te-error') as HTMLDivElement;

  function showError(msg: string) {
    errorEl.textContent = msg;
    errorEl.classList.remove('hidden');
  }

  el.querySelector('#te-save')?.addEventListener('click', async () => {
    const nameEl = el.querySelector('#te-name') as HTMLInputElement;
    const statusEl = el.querySelector('#te-status') as HTMLSelectElement;
    const intervalEl = el.querySelector('#te-interval') as HTMLInputElement;
    const contentEl = el.querySelector('#te-content') as HTMLTextAreaElement;
    const triggerEl = el.querySelector('#te-trigger') as HTMLTextAreaElement;
    const endEl = el.querySelector('#te-end') as HTMLTextAreaElement;

    const name = nameEl.value.trim();
    if (!name) { showError('Task name is required'); return; }

    const intervalRaw = intervalEl.value.trim();
    const interval = intervalRaw ? parseFloat(intervalRaw) : 3600;
    if (isNaN(interval) || interval < 1) { showError('check_interval must be at least 1 second'); return; }
    if (!triggerEl.value.trim()) { showError('trigger_script cannot be empty'); return; }

    const body: Partial<TaskCard> & { previous_name?: string } = {
      name,
      status: statusEl.value as TaskCard['status'],
      check_interval: interval,
      description: contentEl.value,
      trigger_script: triggerEl.value,
      end_script: endEl.value.trim() ? endEl.value : null,
    };
    if (!isNew && card!.name !== name) {
      body.previous_name = card!.name;
    }

    try {
      await api.upsertTask(sessionId, body);
      onDone();
    } catch (e) {
      showError(`Save failed: ${e}`);
    }
  });

  el.querySelector('#te-delete')?.addEventListener('click', async () => {
    if (!card || !confirm(`Delete task "${card.name}"?`)) return;
    try {
      await api.deleteTask(sessionId, card.name);
      onDone();
    } catch (e) {
      showError(`Delete failed: ${e}`);
    }
  });

  el.querySelector('#te-cancel')?.addEventListener('click', onDone);

  return el;
}
