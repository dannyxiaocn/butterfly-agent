import { store } from '../store';
import { api } from '../api';
import { attachSession } from '../main';
import type { ModelsCatalog, Params, PanelEntry, PanelEntryDetail, ProviderCatalogEntry, TaskCard } from '../types';
import { formatInterval, formatRelative } from '../markdown';
import { renderTaskEditor } from './taskEditor';

type PanelTab = 'tasks' | 'panel' | 'config';

function escHtml(s: string): string {
  // Escape single quotes too (defense-in-depth — no current sink uses
  // single-quoted attrs, but the cost is zero and future refactors can't
  // regress silently). Matches the canonical HTML escape set.
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function createPanel(): HTMLElement {
  const el = document.createElement('aside');
  el.id = 'panel';

  let activeTab: PanelTab = 'tasks';
  let editingTask: TaskCard | null | 'new' = null; // null = not editing, 'new' = new task
  type ConfigMode = 'view' | 'form' | 'yaml';
  let configMode: ConfigMode = 'view';
  let modelsCatalog: ModelsCatalog | null = null;
  let modelsCatalogPromise: Promise<ModelsCatalog | null> | null = null;
  const expandedPanel = new Set<string>(); // tids currently expanded
  const panelDetails = new Map<string, PanelEntryDetail>(); // tid → latest detail fetch
  // Sub-agent panel cards expand to show the child session's last 5 events.
  // Cached so repeated open/close doesn't refetch.
  const subAgentChildEvents = new Map<string, Array<Record<string, unknown>>>();
  let panelPollTimer: number | null = null;

  function ensureModelsCatalog(): Promise<ModelsCatalog | null> {
    if (modelsCatalog) return Promise.resolve(modelsCatalog);
    if (modelsCatalogPromise) return modelsCatalogPromise;
    modelsCatalogPromise = api.getModels().then(c => {
      modelsCatalog = c;
      return c;
    }).catch(err => {
      console.error('Failed to load /api/models:', err);
      return null;
    });
    return modelsCatalogPromise;
  }

  function render() {
    if (!store.currentSessionId) {
      el.innerHTML = `<div class="panel-empty">Select a session</div>`;
      return;
    }

    const tabsHtml = `
      <div class="panel-tabs">
        <button class="panel-tab${activeTab === 'tasks' ? ' active' : ''}" data-tab="tasks">Tasks</button>
        <button class="panel-tab${activeTab === 'panel' ? ' active' : ''}" data-tab="panel">Panel</button>
        <button class="panel-tab${activeTab === 'config' ? ' active' : ''}" data-tab="config">Config</button>
      </div>
    `;

    let contentHtml = '';

    if (activeTab === 'tasks') {
      contentHtml = renderTasksTab();
    } else if (activeTab === 'panel') {
      contentHtml = renderPanelTab();
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
        configMode = 'view';
        render();
      });
    });

    // Bind task actions
    if (activeTab === 'tasks') {
      bindTasksTab();
    } else if (activeTab === 'panel') {
      bindPanelTab();
    } else {
      bindConfigTab();
    }

    updatePanelPolling();
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
    const isDuty = card.name === 'duty';
    const dutyPill = isDuty ? `<span class="hb-pill">duty</span>` : '';
    const intervalStr = formatInterval(card.interval);
    const lastRun = formatRelative(card.last_finished_at);
    const statusClass = `task-status-${card.status}`;

    // Content preview: first 3 non-empty lines
    const preview = card.description.split('\n').filter(l => l.trim()).slice(0, 3).join('\n');

    return `
      <div class="task-card" data-name="${escHtml(card.name)}">
        <div class="task-card-header">
          <span class="task-name">${escHtml(card.name)}</span>
          ${dutyPill}
          <span class="task-status-badge ${statusClass}">${card.status}</span>
        </div>
        <div class="task-card-meta">
          <span class="task-interval">${escHtml(intervalStr)}</span>
          ${card.start_at || card.end_at ? `<span class="task-window">window: ${escHtml(card.start_at ?? '∞')} → ${escHtml(card.end_at ?? '∞')}</span>` : ''}
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
      const sid = store.currentSessionId;
      if (!sid) return;
      const res = await api.getTasks(sid).catch(console.error);
      if (store.currentSessionId !== sid) return; // stale guard (Problem 2)
      if (res) {
        store.taskCards = res.cards;
        store.emit('tasks');
        render();
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
      // Refresh tasks — guard against stale session (Problem 2)
      const res = await api.getTasks(sessionId).catch(console.error);
      if (store.currentSessionId !== sessionId) return;
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
    if (configMode !== 'view') return ''; // editor rendered imperatively below

    const params = store.currentParams;
    if (!params) return '<div class="config-empty">No config loaded.</div>';

    // Order the key config fields first, then everything else.
    const ordered: string[] = [
      'name', 'description',
      'provider', 'model',
      'fallback_provider', 'fallback_model',
      'max_iterations',
      'thinking', 'thinking_budget', 'thinking_effort',
      'tool_providers', 'prompts', 'tools', 'skills', 'duty',
    ];
    const excluded = new Set(['is_meta_session']);
    const seen = new Set<string>();
    const rows: string[] = [];
    const renderRow = (k: string, v: unknown): string => {
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
    };
    for (const k of ordered) {
      if (excluded.has(k)) continue;
      if (!(k in params)) continue;
      seen.add(k);
      rows.push(renderRow(k, (params as Record<string, unknown>)[k]));
    }
    for (const [k, v] of Object.entries(params)) {
      if (excluded.has(k) || seen.has(k)) continue;
      rows.push(renderRow(k, v));
    }

    return `
      <table class="config-table">
        <tbody>${rows.join('')}</tbody>
      </table>
      <div class="config-footer">
        <button class="btn-sm btn-primary" id="btn-edit-config-form">Edit</button>
        <button class="btn-sm" id="btn-edit-config-yaml">Edit raw YAML</button>
      </div>
    `;
  }

  function bindConfigTab() {
    el.querySelector('#btn-edit-config-form')?.addEventListener('click', () => {
      configMode = 'form';
      showConfigFormEditor();
    });
    el.querySelector('#btn-edit-config-yaml')?.addEventListener('click', () => {
      configMode = 'yaml';
      showConfigYamlEditor();
    });
  }

  /** Structured form: dropdowns for provider/model, text/number inputs, booleans,
   *  JSON textareas for complex fields (tool_providers, prompts, duty, tools, skills). */
  async function showConfigFormEditor() {
    const content = el.querySelector('#panel-content');
    if (!content || !store.currentSessionId) return;
    const sessionId = store.currentSessionId;
    const params = { ...(store.currentParams ?? {}) } as Record<string, unknown>;

    content.innerHTML = `<div class="config-empty">Loading model catalog…</div>`;
    const catalog = await ensureModelsCatalog();
    if (store.currentSessionId !== sessionId || configMode !== 'form') return;

    const providerOptions: ProviderCatalogEntry[] = catalog?.providers ?? [];
    const efforts = catalog?.thinking_efforts ?? ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];

    // Build the form as a real DOM tree so we can keep input handles and
    // re-render the model dropdown when the provider changes.
    const form = document.createElement('div');
    form.className = 'config-form';

    const err = document.createElement('div');
    err.className = 'form-error hidden';

    // ---- Text fields ----
    const nameInput = textRow(form, 'name', String(params.name ?? ''));
    const descInput = textRow(form, 'description', String(params.description ?? ''));

    // ---- Provider + model (paired) ----
    const providerSelect = selectRow(form, 'provider', providerNames(providerOptions), String(params.provider ?? ''), '(default)');
    const modelWrap = document.createElement('div');
    modelWrap.className = 'cfg-row';
    form.appendChild(modelWrap);

    function renderModelRow() {
      modelWrap.innerHTML = '';
      const providerKey = providerSelect.value;
      const entry = providerOptions.find(p => p.provider === providerKey) ?? null;
      const models = entry?.models ?? [];
      const current = String(params.model ?? '');
      const label = document.createElement('label');
      label.className = 'cfg-label';
      label.textContent = 'model';
      modelWrap.appendChild(label);

      const select = document.createElement('select');
      select.className = 'cfg-input';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = entry ? `(default: ${entry.default_model})` : '(default)';
      select.appendChild(blank);
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
      }
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = 'Custom…';
      select.appendChild(customOpt);

      const customInput = document.createElement('input');
      customInput.type = 'text';
      customInput.className = 'cfg-input cfg-input-custom';
      customInput.placeholder = 'custom model id';

      // Initial state
      if (current && models.includes(current)) {
        select.value = current;
        customInput.classList.add('hidden');
      } else if (current) {
        select.value = '__custom__';
        customInput.value = current;
      } else {
        select.value = '';
        customInput.classList.add('hidden');
      }

      select.addEventListener('change', () => {
        if (select.value === '__custom__') {
          customInput.classList.remove('hidden');
          customInput.focus();
        } else {
          customInput.classList.add('hidden');
          params.model = select.value || null;
        }
      });
      customInput.addEventListener('input', () => {
        params.model = customInput.value.trim() || null;
      });

      modelWrap.appendChild(select);
      modelWrap.appendChild(customInput);

      // Keep params.model up to date with initial dropdown state too
      params.model = select.value === '__custom__' ? (customInput.value.trim() || null) : (select.value || null);
    }
    renderModelRow();
    providerSelect.addEventListener('change', () => {
      params.provider = providerSelect.value || null;
      // When provider changes, clear model — user picks fresh from the new list
      params.model = null;
      renderModelRow();
    });

    // ---- Fallback provider + model ----
    // Model becomes a provider-keyed dropdown (mirrors the primary row) —
    // prevents mis-paired fallback_provider vs. fallback_model strings
    // that 500 at agent-start time (PR #24 review item 6).
    const fbProviderSelect = selectRow(form, 'fallback_provider', providerNames(providerOptions), String(params.fallback_provider ?? ''), '(none)');
    const fbModelWrap = document.createElement('div');
    fbModelWrap.className = 'cfg-row';
    form.appendChild(fbModelWrap);

    let fbSelect: HTMLSelectElement;
    let fbCustomInput: HTMLInputElement;
    function renderFallbackModelRow() {
      fbModelWrap.innerHTML = '';
      const providerKey = fbProviderSelect.value;
      const entry = providerOptions.find(p => p.provider === providerKey) ?? null;
      const models = entry?.models ?? [];
      const current = String(params.fallback_model ?? '');
      const label = document.createElement('label');
      label.className = 'cfg-label';
      label.textContent = 'fallback_model';
      fbModelWrap.appendChild(label);

      fbSelect = document.createElement('select');
      fbSelect.className = 'cfg-input';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = entry ? `(default: ${entry.default_model})` : '(none)';
      fbSelect.appendChild(blank);
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        fbSelect.appendChild(opt);
      }
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = 'Custom…';
      fbSelect.appendChild(customOpt);

      fbCustomInput = document.createElement('input');
      fbCustomInput.type = 'text';
      fbCustomInput.className = 'cfg-input cfg-input-custom';
      fbCustomInput.placeholder = 'custom model id';

      if (current && models.includes(current)) {
        fbSelect.value = current;
        fbCustomInput.classList.add('hidden');
      } else if (current) {
        fbSelect.value = '__custom__';
        fbCustomInput.value = current;
      } else {
        fbSelect.value = '';
        fbCustomInput.classList.add('hidden');
      }

      fbSelect.addEventListener('change', () => {
        if (fbSelect.value === '__custom__') {
          fbCustomInput.classList.remove('hidden');
          fbCustomInput.focus();
          params.fallback_model = fbCustomInput.value.trim() || null;
        } else {
          fbCustomInput.classList.add('hidden');
          params.fallback_model = fbSelect.value || null;
        }
      });
      fbCustomInput.addEventListener('input', () => {
        params.fallback_model = fbCustomInput.value.trim() || null;
      });

      fbModelWrap.appendChild(fbSelect);
      fbModelWrap.appendChild(fbCustomInput);
      params.fallback_model = fbSelect.value === '__custom__'
        ? (fbCustomInput.value.trim() || null)
        : (fbSelect.value || null);
    }
    renderFallbackModelRow();
    fbProviderSelect.addEventListener('change', () => {
      params.fallback_provider = fbProviderSelect.value || null;
      params.fallback_model = null;
      renderFallbackModelRow();
    });

    // ---- Numbers ----
    const maxIterInput = numberRow(form, 'max_iterations', Number(params.max_iterations ?? 20));

    // ---- Thinking ----
    // thinking_effort list is provider-specific. `xhigh` is codex-only (model
    // catalog flags it); without filtering we'd let the user persist an
    // invalid effort for e.g. openai-responses and it'd 400 at agent start
    // (PR #24 review items 7/13).
    const thinkingCheckbox = boolRow(form, 'thinking', Boolean(params.thinking));
    const thinkingBudgetInput = numberRow(form, 'thinking_budget', Number(params.thinking_budget ?? 8000));
    function effortsForProvider(key: string): string[] {
      const entry = providerOptions.find(p => p.provider === key);
      // Providers with no effort vocabulary (Anthropic/Kimi = budget-style,
      // plain OpenAI = no thinking) still accept the field on the YAML but
      // the value is ignored. Surface the full union so the form doesn't
      // look empty for those providers.
      const supported = entry?.supported_efforts ?? [];
      return supported.length ? supported : efforts.filter(e => e !== 'xhigh');
    }
    const initialProviderKey = String(params.provider ?? '');
    const effortOptions = effortsForProvider(initialProviderKey);
    const thinkingEffortSelect = selectRow(form, 'thinking_effort', effortOptions, String(params.thinking_effort ?? 'high'));
    // Re-filter when the primary provider flips mid-edit so an invalid
    // effort can't be persisted for a non-capable provider.
    providerSelect.addEventListener('change', () => {
      const newOptions = effortsForProvider(providerSelect.value);
      const current = thinkingEffortSelect.value;
      thinkingEffortSelect.innerHTML = '';
      for (const opt of newOptions) {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt;
        thinkingEffortSelect.appendChild(o);
      }
      thinkingEffortSelect.value = newOptions.includes(current) ? current : 'high';
    });

    // ---- JSON/YAML-ish complex fields as compact textareas ----
    const toolProvidersInput = jsonRow(form, 'tool_providers', params.tool_providers);
    const promptsInput = jsonRow(form, 'prompts', params.prompts);
    const toolsInput = jsonRow(form, 'tools', params.tools);
    const skillsInput = jsonRow(form, 'skills', params.skills);
    const dutyInput = jsonRow(form, 'duty', params.duty);

    // ---- Actions ----
    const actions = document.createElement('div');
    actions.className = 'form-row';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn-primary';
    saveBtn.textContent = 'Save';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn-sm';
    cancelBtn.textContent = 'Cancel';
    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);

    cancelBtn.addEventListener('click', () => {
      configMode = 'view';
      render();
    });

    saveBtn.addEventListener('click', async () => {
      err.classList.add('hidden');
      err.textContent = '';
      // Collect values
      const next: Record<string, unknown> = {
        name: nameInput.value,
        description: descInput.value,
        provider: providerSelect.value || null,
        model: params.model ?? null,
        fallback_provider: fbProviderSelect.value || null,
        fallback_model: (params.fallback_model as string | null) ?? null,
        max_iterations: Number(maxIterInput.value) || 20,
        thinking: thinkingCheckbox.checked,
        thinking_budget: Number(thinkingBudgetInput.value) || 8000,
        thinking_effort: thinkingEffortSelect.value,
      };
      try {
        next.tool_providers = parseJsonField(toolProvidersInput.value, 'tool_providers');
        next.prompts = parseJsonField(promptsInput.value, 'prompts');
        next.tools = parseJsonField(toolsInput.value, 'tools');
        next.skills = parseJsonField(skillsInput.value, 'skills');
        next.duty = parseJsonField(dutyInput.value, 'duty');
      } catch (e) {
        err.textContent = String(e);
        err.classList.remove('hidden');
        return;
      }
      try {
        const res = await api.setConfig(sessionId, next as Params);
        if (store.currentSessionId !== sessionId) return;
        store.currentParams = res.params;
        store.emit('config');
        configMode = 'view';
        render();
      } catch (e) {
        console.error('setConfig failed:', e);
        err.textContent = `Save failed: ${e}`;
        err.classList.remove('hidden');
      }
    });

    content.innerHTML = '';
    content.appendChild(form);
    content.appendChild(err);
    content.appendChild(actions);
  }

  /** Raw YAML editor — edits config.yaml text directly, round-trips via the
   *  /config/yaml endpoint. */
  async function showConfigYamlEditor() {
    const content = el.querySelector('#panel-content');
    if (!content || !store.currentSessionId) return;
    const sessionId = store.currentSessionId;

    content.innerHTML = `<div class="config-empty">Loading config.yaml…</div>`;
    let yamlText = '';
    try {
      const resp = await api.getConfigYaml(sessionId);
      if (store.currentSessionId !== sessionId || configMode !== 'yaml') return;
      yamlText = resp.yaml;
    } catch (e) {
      console.error('getConfigYaml failed:', e);
      content.innerHTML = `<div class="form-error">Failed to load config.yaml: ${escHtml(String(e))}</div>`;
      return;
    }

    const textarea = document.createElement('textarea');
    textarea.className = 'config-json-editor';
    textarea.rows = 22;
    textarea.value = yamlText;

    const err = document.createElement('div');
    err.className = 'form-error hidden';

    const actions = document.createElement('div');
    actions.className = 'form-row';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn-primary';
    saveBtn.textContent = 'Save YAML';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn-sm';
    cancelBtn.textContent = 'Cancel';
    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);

    cancelBtn.addEventListener('click', () => {
      configMode = 'view';
      render();
    });
    saveBtn.addEventListener('click', async () => {
      err.classList.add('hidden');
      try {
        const res = await api.setConfigYaml(sessionId, textarea.value);
        if (store.currentSessionId !== sessionId) return;
        store.currentParams = res.params;
        store.emit('config');
        configMode = 'view';
        render();
      } catch (e) {
        console.error('setConfigYaml failed:', e);
        err.textContent = `Save failed: ${e}`;
        err.classList.remove('hidden');
      }
    });

    const hint = document.createElement('div');
    hint.className = 'cfg-hint';
    hint.textContent = 'YAML is parsed server-side; comments will be dropped on save.';

    content.innerHTML = '';
    content.appendChild(hint);
    content.appendChild(textarea);
    content.appendChild(err);
    content.appendChild(actions);
  }

  function providerNames(options: ProviderCatalogEntry[]): string[] {
    return options.map(p => p.provider);
  }

  function textRow(parent: HTMLElement, key: string, value: string, placeholder = ''): HTMLInputElement {
    const row = document.createElement('div');
    row.className = 'cfg-row';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = key;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cfg-input';
    input.value = value;
    if (placeholder) input.placeholder = placeholder;
    row.appendChild(label);
    row.appendChild(input);
    parent.appendChild(row);
    return input;
  }

  function numberRow(parent: HTMLElement, key: string, value: number): HTMLInputElement {
    const row = document.createElement('div');
    row.className = 'cfg-row';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = key;
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'cfg-input';
    input.value = String(value);
    row.appendChild(label);
    row.appendChild(input);
    parent.appendChild(row);
    return input;
  }

  function boolRow(parent: HTMLElement, key: string, value: boolean): HTMLInputElement {
    const row = document.createElement('div');
    row.className = 'cfg-row';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = key;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'cfg-checkbox';
    input.checked = value;
    row.appendChild(label);
    row.appendChild(input);
    parent.appendChild(row);
    return input;
  }

  function selectRow(parent: HTMLElement, key: string, options: string[], value: string, blankLabel?: string): HTMLSelectElement {
    const row = document.createElement('div');
    row.className = 'cfg-row';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = key;
    const select = document.createElement('select');
    select.className = 'cfg-input';
    if (blankLabel !== undefined) {
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = blankLabel;
      select.appendChild(blank);
    }
    for (const opt of options) {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      select.appendChild(o);
    }
    // If the current value isn't in the list, append it so we don't silently drop it.
    if (value && !options.includes(value)) {
      const o = document.createElement('option');
      o.value = value;
      o.textContent = value;
      select.appendChild(o);
    }
    select.value = value;
    row.appendChild(label);
    row.appendChild(select);
    parent.appendChild(row);
    return select;
  }

  function jsonRow(parent: HTMLElement, key: string, value: unknown): HTMLTextAreaElement {
    const row = document.createElement('div');
    row.className = 'cfg-row cfg-row-block';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = `${key} (JSON)`;
    const textarea = document.createElement('textarea');
    textarea.className = 'cfg-input cfg-textarea';
    textarea.rows = 3;
    textarea.value = value === undefined || value === null ? 'null' : JSON.stringify(value, null, 2);
    row.appendChild(label);
    row.appendChild(textarea);
    parent.appendChild(row);
    return textarea;
  }

  function parseJsonField(raw: string, field: string): unknown {
    const text = raw.trim();
    if (!text || text === 'null') return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      throw new Error(`${field}: invalid JSON — ${(e as Error).message}`);
    }
  }

  // ================= PANEL TAB =================

  function renderPanelTab(): string {
    const entries = store.panelEntries;
    if (!entries.length) {
      return `
        <div class="tasks-empty">No panel entries yet.</div>
        <div class="tasks-footer">
          <button class="btn-sm" id="btn-refresh-panel">↻ Refresh</button>
        </div>
      `;
    }
    const rowsHtml = entries.map(e => renderPanelRow(e)).join('');
    return `
      <div class="task-cards">${rowsHtml}</div>
      <div class="tasks-footer">
        <button class="btn-sm" id="btn-refresh-panel">↻ Refresh</button>
      </div>
    `;
  }

  function renderPanelRow(entry: PanelEntry): string {
    if (entry.type === 'sub_agent') {
      return renderSubAgentRow(entry);
    }
    const statusClass = `panel-status-${entry.status}`;
    const expanded = expandedPanel.has(entry.tid);
    const detail = panelDetails.get(entry.tid);
    const tailOneLine = panelTailOneLine(entry, detail);
    const fullJsonHtml = expanded
      ? `<pre class="panel-json">${escHtml(JSON.stringify(entryToJsonView(entry, detail), null, 2))}</pre>`
      : '';
    const outputTailHtml = expanded
      ? renderOutputTailBlock(detail)
      : '';
    const actionsHtml = expanded
      ? `
        <div class="task-card-actions">
          <button class="btn-sm" data-panel-action="fetch" data-tid="${escHtml(entry.tid)}">Fetch full output</button>
          <button class="btn-sm" data-panel-action="kill" data-tid="${escHtml(entry.tid)}">Kill</button>
        </div>
      `
      : '';

    return `
      <div class="task-card panel-row${expanded ? ' expanded' : ''}" data-tid="${escHtml(entry.tid)}">
        <div class="task-card-header panel-row-header" data-tid="${escHtml(entry.tid)}">
          <span class="task-status-badge ${statusClass}">${escHtml(entry.status)}</span>
          <span class="task-name">${escHtml(entry.tool_name)}</span>
          <span class="hb-pill panel-tid">${escHtml(entry.tid)}</span>
        </div>
        <div class="task-preview panel-tail" title="${escHtml(tailOneLine)}">${escHtml(tailOneLine) || '<span class="cfg-null">(no output yet)</span>'}</div>
        ${fullJsonHtml}
        ${outputTailHtml}
        ${actionsHtml}
      </div>
    `;
  }

  function renderSubAgentRow(entry: PanelEntry): string {
    const expanded = expandedPanel.has(entry.tid);
    const meta = (entry.meta ?? {}) as Record<string, unknown>;
    const childId = String(meta.child_session_id ?? '');
    const mode = String(meta.mode ?? '?');
    const lastChildState = String(meta.last_child_state ?? '');
    // Prefer ``meta.display_name`` (written by SubAgentRunner for children
    // spawned post-v2.0.19). Fall back to the child session's own
    // manifest-side display_name from the store — covers the case where a
    // card is rendered before the runner has saved its meta update.
    const storeName = (() => {
      if (!childId) return '';
      const s = store.sessions.find(x => x.id === childId);
      return (s?.display_name ?? '').trim();
    })();
    const displayName = String(meta.display_name ?? '').trim() || storeName;
    const headerLabel = displayName || 'sub_agent';
    const statusClass = `panel-status-${entry.status}`;
    // Thumbnail line: child id + mode chip + current activity.
    const activityIcon = entry.status === 'running' ? '▶' : entry.status === 'completed' ? '✓' : '⚠';
    const thumb = lastChildState
      ? `${activityIcon} ${escHtml(lastChildState)}`
      : (entry.status === 'completed' ? '✓ done' : `${activityIcon} starting…`);

    let expandedHtml = '';
    if (expanded) {
      const events = subAgentChildEvents.get(entry.tid);
      const result = String(meta.result_text ?? '');
      const resultBlock = result
        ? `<details class="sub-agent-result" open><summary>Final reply</summary><pre>${escHtml(result)}</pre></details>`
        : '';
      let recentBlock: string;
      if (events === undefined) {
        recentBlock = `<div class="sub-agent-recent-empty">Loading recent events…</div>`;
      } else if (events.length === 0) {
        recentBlock = `<div class="sub-agent-recent-empty">No recent events.</div>`;
      } else {
        recentBlock = `<div class="sub-agent-recent">${events.map(formatChildEvent).join('')}</div>`;
      }
      const openLink = childId
        ? `<button class="btn-sm" data-sub-action="open-child" data-child-id="${escHtml(childId)}">Open child session</button>`
        : '';
      expandedHtml = `
        <div class="sub-agent-detail">
          <div class="sub-agent-detail-row"><span class="cfg-label">child:</span> <span class="hb-pill">${escHtml(childId)}</span></div>
          <div class="sub-agent-detail-row"><span class="cfg-label">recent activity (last 5):</span></div>
          ${recentBlock}
          ${resultBlock}
          <div class="task-card-actions">
            ${openLink}
            <button class="btn-sm" data-panel-action="kill" data-tid="${escHtml(entry.tid)}">Kill</button>
          </div>
        </div>
      `;
    }

    return `
      <div class="task-card panel-row sub-agent-row${expanded ? ' expanded' : ''}" data-tid="${escHtml(entry.tid)}">
        <div class="task-card-header panel-row-header" data-tid="${escHtml(entry.tid)}" title="${escHtml(childId || entry.tid)}">
          <span class="task-status-badge ${statusClass}">${escHtml(entry.status)}</span>
          <span class="task-name">${escHtml(headerLabel)}</span>
          <span class="session-mode-chip">${escHtml(mode)}</span>
          <span class="hb-pill panel-tid">${escHtml(entry.tid)}</span>
        </div>
        <div class="task-preview panel-tail" title="${escHtml(thumb)}">${thumb}</div>
        ${expandedHtml}
      </div>
    `;
  }

  function formatChildEvent(evt: Record<string, unknown>): string {
    const t = String(evt.type ?? '');
    if (t === 'tool_call' || t === 'tool') {
      const name = String(evt.name ?? evt.tool ?? '');
      return `<div class="sub-agent-evt"><span class="sub-agent-evt-icon">▶</span> tool: <code>${escHtml(name)}</code></div>`;
    }
    if (t === 'tool_done') {
      const name = String(evt.name ?? '');
      return `<div class="sub-agent-evt"><span class="sub-agent-evt-icon">✓</span> tool done: <code>${escHtml(name)}</code></div>`;
    }
    if (t === 'model_status') {
      const v = String(evt.value ?? evt.state ?? '');
      return `<div class="sub-agent-evt sub-agent-evt-quiet">model: ${escHtml(v)}</div>`;
    }
    if (t === 'thinking_start' || t === 'thinking_done') {
      return `<div class="sub-agent-evt sub-agent-evt-quiet">${escHtml(t.replace('_', ' '))}</div>`;
    }
    if (t === 'partial_text') {
      return '';
    }
    return `<div class="sub-agent-evt sub-agent-evt-quiet">${escHtml(t)}</div>`;
  }

  function entryToJsonView(entry: PanelEntry, detail: PanelEntryDetail | undefined): Record<string, unknown> {
    // Prefer the detail payload (same shape + output_tail) if loaded.
    return detail ? { ...detail } : { ...entry };
  }

  function renderOutputTailBlock(detail: PanelEntryDetail | undefined): string {
    if (!detail) {
      return `<div class="panel-output-tail panel-output-empty">Click “Fetch full output” to load the last 40 lines.</div>`;
    }
    if (detail.output_tail == null) {
      return `<div class="panel-output-tail panel-output-empty">(no output file)</div>`;
    }
    return `<pre class="panel-output-tail">${escHtml(detail.output_tail)}</pre>`;
  }

  function panelTailOneLine(_entry: PanelEntry, detail: PanelEntryDetail | undefined): string {
    const tail = detail?.output_tail;
    if (!tail) return '';
    const lines = tail.split('\n').filter(l => l.length > 0);
    return lines.length ? lines[lines.length - 1] : '';
  }

  function bindPanelTab() {
    el.querySelector('#btn-refresh-panel')?.addEventListener('click', () => {
      refreshPanel();
    });

    el.querySelectorAll('.panel-row-header').forEach(hdr => {
      hdr.addEventListener('click', async () => {
        const tid = (hdr as HTMLElement).dataset.tid;
        if (!tid) return;
        const opening = !expandedPanel.has(tid);
        if (opening) {
          expandedPanel.add(tid);
        } else {
          expandedPanel.delete(tid);
        }
        render();
        // Sub-agent rows: load the child's last 5 events on first open.
        if (opening) {
          const entry = (store.panelEntries as PanelEntry[]).find(e => e.tid === tid);
          const childId = entry && entry.type === 'sub_agent'
            ? String((entry.meta ?? {}).child_session_id ?? '')
            : '';
          if (childId) {
            try {
              const events = await api.getEventsTail(childId, 5);
              subAgentChildEvents.set(tid, events);
              if (expandedPanel.has(tid)) render();
            } catch (e) {
              console.error('Failed to load child events:', e);
            }
          }
        }
      });
    });

    el.querySelectorAll('[data-sub-action="open-child"]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const childId = (btn as HTMLElement).dataset.childId;
        if (!childId) return;
        await attachSession(childId);
      });
    });

    el.querySelectorAll('[data-panel-action]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const action = (btn as HTMLElement).dataset.panelAction;
        const tid = (btn as HTMLElement).dataset.tid;
        const sid = store.currentSessionId;
        if (!sid || !tid) return;
        if (action === 'kill') {
          try {
            await api.killPanelEntry(sid, tid);
          } catch (e) {
            console.error('Failed to kill panel entry:', e);
            return;
          }
          if (store.currentSessionId !== sid) return;
          await refreshPanel();
        } else if (action === 'fetch') {
          try {
            const detail = await api.getPanelEntry(sid, tid);
            if (store.currentSessionId !== sid) return;
            panelDetails.set(tid, detail);
            render();
          } catch (e) {
            console.error('Failed to fetch panel entry:', e);
          }
        }
      });
    });
  }

  async function refreshPanel(): Promise<void> {
    const sid = store.currentSessionId;
    if (!sid) return;
    try {
      const entries = await api.getPanel(sid);
      if (store.currentSessionId !== sid) return; // stale (Problem 2)
      store.panelEntries = entries;
      store.emit('panel');
      // If user has entries expanded, refresh their details too so the
      // tail-one-line on the collapsed row and the output block stay fresh.
      if (activeTab === 'panel' && expandedPanel.size > 0) {
        await Promise.all([...expandedPanel].map(async tid => {
          const entry = entries.find(e => e.tid === tid);
          if (entry?.type === 'sub_agent') {
            // Refresh the child's recent events while the sub-agent card is open.
            const childId = String((entry.meta ?? {}).child_session_id ?? '');
            if (childId) {
              try {
                const events = await api.getEventsTail(childId, 5);
                if (store.currentSessionId !== sid) return;
                subAgentChildEvents.set(tid, events);
              } catch (e) {
                console.error('Failed to refresh sub-agent child events:', e);
              }
            }
            return;
          }
          try {
            const detail = await api.getPanelEntry(sid, tid);
            if (store.currentSessionId !== sid) return;
            panelDetails.set(tid, detail);
          } catch (e) {
            console.error('Failed to refresh panel detail:', e);
          }
        }));
      }
      if (activeTab === 'panel') render();
    } catch (e) {
      console.error('Failed to refresh panel:', e);
    }
  }

  function updatePanelPolling() {
    const shouldPoll = activeTab === 'panel' && !!store.currentSessionId;
    if (shouldPoll && panelPollTimer == null) {
      panelPollTimer = window.setInterval(refreshPanel, 2000);
      // Fire an immediate refresh so the tab populates without waiting 2s.
      refreshPanel();
    } else if (!shouldPoll && panelPollTimer != null) {
      window.clearInterval(panelPollTimer);
      panelPollTimer = null;
    }
  }

  // ================= STORE WIRING =================

  store.on('tasks', () => {
    if (activeTab === 'tasks' && editingTask === null) render();
  });
  store.on('panel', () => {
    if (activeTab === 'panel') render();
  });
  store.on('config', () => {
    if (activeTab === 'config' && configMode === 'view') render();
  });
  store.on('currentSession', () => {
    editingTask = null;
    configMode = 'view';
    expandedPanel.clear();
    panelDetails.clear();
    store.panelEntries = [];
    render();
  });

  render();
  return el;
}


