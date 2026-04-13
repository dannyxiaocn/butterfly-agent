import { api } from '../api';
import type { TaskCard } from '../types';
import { escapeHtml } from '../markdown';

export function renderTaskEditor(card: TaskCard | null, sessionId: string, onDone: () => void): HTMLElement {
  const isNew = card === null;

  const el = document.createElement('div');
  el.className = 'task-editor';

  const intervalVal = card?.interval ?? (isNew ? '' : '');
  const startsVal = card?.start_at ? toDatetimeLocal(card.start_at) : '';
  const endsVal = card?.end_at ? toDatetimeLocal(card.end_at) : '';
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
      <label>Interval (seconds, blank = one-shot)</label>
      <input id="te-interval" type="number" value="${intervalVal}" placeholder="e.g. 7200" min="1" />
    </div>
    <div class="form-row">
      <div class="form-field flex1">
        <label>Starts at</label>
        <input id="te-starts" type="datetime-local" value="${escapeHtml(startsVal)}" />
      </div>
      <div class="form-field flex1">
        <label>Ends at</label>
        <input id="te-ends" type="datetime-local" value="${escapeHtml(endsVal)}" />
      </div>
    </div>
    <div class="form-field">
      <label>Content</label>
      <textarea id="te-content" class="task-content-textarea" rows="10">${escapeHtml(card?.description ?? '')}</textarea>
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
    const startsEl = el.querySelector('#te-starts') as HTMLInputElement;
    const endsEl = el.querySelector('#te-ends') as HTMLInputElement;
    const contentEl = el.querySelector('#te-content') as HTMLTextAreaElement;

    const name = nameEl.value.trim();
    if (!name) { showError('Task name is required'); return; }

    const intervalRaw = intervalEl.value.trim();
    let interval: number | null = null;
    if (intervalRaw) {
      interval = parseFloat(intervalRaw);
      if (isNaN(interval) || interval < 1) { showError('Interval must be at least 1 second'); return; }
    }
    const startAt = startsEl.value ? fromDatetimeLocal(startsEl.value) : null;
    const endAt = endsEl.value ? fromDatetimeLocal(endsEl.value) : null;

    const body: Partial<TaskCard> & { previous_name?: string } = {
      name,
      status: statusEl.value as TaskCard['status'],
      interval,
      start_at: startAt,
      end_at: endAt,
      description: contentEl.value,
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

function toDatetimeLocal(iso: string): string {
  try {
    const d = new Date(iso);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return '';
  }
}

function fromDatetimeLocal(local: string): string {
  try {
    return new Date(local).toISOString();
  } catch {
    return local;
  }
}
