import { store } from '../store';
import { api } from '../api';
import { attachSession } from '../main';
import type { ModelsCatalog, Params, PanelEntry, PanelEntryDetail, PanelEntryStatus, ProviderCatalogEntry, TaskCard } from '../types';
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
  type ConfigMode = 'view' | 'form';
  let configMode: ConfigMode = 'view';
  // Cache of tools.md / skills.md / prompts/system.md content for the config
  // view, keyed by the session it was fetched for. The view renders
  // placeholder <pre> blocks and fills them from this cache (or fires a fresh
  // fetch if the session changed).
  const assetCache = new Map<string, { tools: string; skills: string; system: string }>();
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

    // v2.0.23: collapsible <details> chrome. Summary shows just identity
    // (name + duty + status + interval pill). The heavier fields —
    // full description, progress, comments, window + last-run meta,
    // and the Edit button — live in the body so the panel stays scannable
    // when there are many task cards.
    const descriptionBlock = card.description.trim()
      ? `<div class="task-card-section">
           <div class="task-card-section-label">description</div>
           <div class="task-card-section-body">${escHtml(card.description)}</div>
         </div>`
      : '';
    const progressBlock = card.progress && card.progress.trim()
      ? `<div class="task-card-section">
           <div class="task-card-section-label">progress</div>
           <div class="task-card-section-body">${escHtml(card.progress)}</div>
         </div>`
      : '';
    const commentsBlock = card.comments && card.comments.trim()
      ? `<div class="task-card-section">
           <div class="task-card-section-label">comments</div>
           <div class="task-card-section-body">${escHtml(card.comments)}</div>
         </div>`
      : '';
    const windowMeta = (card.start_at || card.end_at)
      ? `<span class="task-window">window: ${escHtml(card.start_at ?? '∞')} → ${escHtml(card.end_at ?? '∞')}</span>`
      : '';

    return `
      <details class="task-card" data-name="${escHtml(card.name)}">
        <summary class="task-card-summary">
          <span class="task-card-summary-main">
            <span class="task-name">${escHtml(card.name)}</span>
            ${dutyPill}
            <span class="task-status-badge ${statusClass}">${card.status}</span>
            <span class="task-interval-pill">${escHtml(intervalStr)}</span>
          </span>
        </summary>
        <div class="task-card-body">
          <div class="task-card-meta">
            ${windowMeta}
            <span class="task-last-run">last run: ${escHtml(lastRun)}</span>
          </div>
          ${descriptionBlock}
          ${progressBlock}
          ${commentsBlock}
          <div class="task-card-actions">
            <button class="btn-sm btn-edit" data-name="${escHtml(card.name)}">Edit</button>
          </div>
        </div>
      </details>
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

  /** Collapse boolean `thinking` + `thinking_effort` into the three-tier UI:
   *  No / Medium / High. Budget is assigned from the tier on save. */
  function thinkingTier(params: Record<string, unknown> | null | undefined): 'No' | 'Medium' | 'High' {
    if (!params) return 'No';
    if (!params.thinking) return 'No';
    const effort = String(params.thinking_effort ?? '').toLowerCase();
    if (effort === 'medium') return 'Medium';
    return 'High';
  }

  /** Map the three-tier selection back to the three underlying fields. */
  function tierToThinkingFields(tier: 'No' | 'Medium' | 'High'): {
    thinking: boolean; thinking_budget: number; thinking_effort: string;
  } {
    if (tier === 'No') return { thinking: false, thinking_budget: 0, thinking_effort: 'none' };
    if (tier === 'Medium') return { thinking: true, thinking_budget: 4000, thinking_effort: 'medium' };
    return { thinking: true, thinking_budget: 8000, thinking_effort: 'high' };
  }

  function renderConfigTab(): string {
    if (configMode !== 'view') return ''; // editor rendered imperatively below

    const params = store.currentParams;
    if (!params) return '<div class="config-empty">No config loaded.</div>';

    // Surface only the user-editable fields; internal bookkeeping
    // (thinking, thinking_budget) is folded into the thinking_effort tier.
    const rows: string[] = [];
    const renderRow = (k: string, v: unknown): string => {
      let displayVal: string;
      if (v === null || v === undefined || v === '') {
        displayVal = '<span class="cfg-null">—</span>';
      } else if (typeof v === 'object') {
        displayVal = `<code>${escHtml(JSON.stringify(v))}</code>`;
      } else if (typeof v === 'boolean') {
        displayVal = v ? '<span class="cfg-true">true</span>' : '<span class="cfg-false">false</span>';
      } else {
        displayVal = escHtml(String(v));
      }
      return `<tr><td class="cfg-key">${escHtml(k)}</td><td class="cfg-val">${displayVal}</td></tr>`;
    };
    const p = params as Record<string, unknown>;
    rows.push(renderRow('agent', p.agent));
    rows.push(renderRow('description', p.description));
    rows.push(renderRow('provider', p.provider));
    rows.push(renderRow('model', p.model));
    rows.push(renderRow('fallback_provider', p.fallback_provider));
    rows.push(renderRow('fallback_model', p.fallback_model));
    rows.push(renderRow('max_iterations', p.max_iterations));
    rows.push(renderRow('thinking_effort', thinkingTier(p)));
    if (p.duty !== undefined) rows.push(renderRow('duty', p.duty));

    return `
      <table class="config-table">
        <tbody>${rows.join('')}</tbody>
      </table>
      <div class="config-asset-group">
        <div class="cfg-label cfg-asset-heading">tools.md</div>
        <pre class="cfg-asset-block" id="cfg-asset-tools">Loading…</pre>
      </div>
      <div class="config-asset-group">
        <div class="cfg-label cfg-asset-heading">skills.md</div>
        <pre class="cfg-asset-block" id="cfg-asset-skills">Loading…</pre>
      </div>
      <div class="config-asset-group">
        <div class="cfg-label cfg-asset-heading">prompts/system.md</div>
        <pre class="cfg-asset-block" id="cfg-asset-system">Loading…</pre>
      </div>
      <div class="config-footer">
        <button class="btn-sm btn-primary" id="btn-edit-config-form">Edit</button>
      </div>
    `;
  }

  function fillConfigAssetBlocks(sessionId: string) {
    const cached = assetCache.get(sessionId);
    const fill = (assets: { tools: string; skills: string; system: string }) => {
      if (store.currentSessionId !== sessionId || activeTab !== 'config' || configMode !== 'view') return;
      const toolsEl = el.querySelector('#cfg-asset-tools');
      const skillsEl = el.querySelector('#cfg-asset-skills');
      const systemEl = el.querySelector('#cfg-asset-system');
      if (toolsEl) toolsEl.textContent = assets.tools || '(empty)';
      if (skillsEl) skillsEl.textContent = assets.skills || '(empty)';
      if (systemEl) systemEl.textContent = assets.system || '(empty)';
    };
    if (cached) {
      fill(cached);
      return;
    }
    Promise.all([
      api.getAssetMd(sessionId, 'tools').then(r => r.text).catch(() => ''),
      api.getAssetMd(sessionId, 'skills').then(r => r.text).catch(() => ''),
      api.getPromptMd(sessionId, 'system').then(r => r.text).catch(() => ''),
    ]).then(([tools, skills, system]) => {
      const assets = { tools, skills, system };
      assetCache.set(sessionId, assets);
      fill(assets);
    });
  }

  function bindConfigTab() {
    el.querySelector('#btn-edit-config-form')?.addEventListener('click', () => {
      configMode = 'form';
      showConfigFormEditor();
    });
    const sid = store.currentSessionId;
    if (sid) fillConfigAssetBlocks(sid);
  }

  /** Structured form. Single model per provider (auto-bound to provider
   *  default); thinking collapsed to No/Medium/High; tools.md / skills.md /
   *  prompts/system.md surfaced as plaintext cells. */
  async function showConfigFormEditor() {
    const content = el.querySelector('#panel-content');
    if (!content || !store.currentSessionId) return;
    const sessionId = store.currentSessionId;
    const params = { ...(store.currentParams ?? {}) } as Record<string, unknown>;

    content.innerHTML = `<div class="config-empty">Loading config…</div>`;
    const [catalog, toolsMd, skillsMd, systemMd] = await Promise.all([
      ensureModelsCatalog(),
      api.getAssetMd(sessionId, 'tools').then(r => r.text).catch(() => ''),
      api.getAssetMd(sessionId, 'skills').then(r => r.text).catch(() => ''),
      api.getPromptMd(sessionId, 'system').then(r => r.text).catch(() => ''),
    ]);
    if (store.currentSessionId !== sessionId || configMode !== 'form') return;

    const providerOptions: ProviderCatalogEntry[] = catalog?.providers ?? [];
    const providerByKey = new Map(providerOptions.map(p => [p.provider, p]));

    const form = document.createElement('div');
    form.className = 'config-form';

    const err = document.createElement('div');
    err.className = 'form-error hidden';

    // ---- Text fields ----
    const agentInput = textRow(form, 'agent', String(params.agent ?? ''));
    const descInput = textRow(form, 'description', String(params.description ?? ''));

    // ---- Provider + read-only model (driven by provider) ----
    const providerSelect = selectRow(
      form, 'provider',
      providerOptions.map(p => p.provider),
      String(params.provider ?? ''),
      '(default)',
    );
    const modelWrap = document.createElement('div');
    modelWrap.className = 'cfg-row';
    form.appendChild(modelWrap);

    // v2.0.19 (parallel): dropdown-with-Custom row restored — PR #36's
    // read-only readout assumed "one model per provider forever", but the
    // models.yaml catalog keeps the list shape so future multi-model
    // support is just a yaml edit. ``providerOptions[*].models`` is always
    // populated from /api/models; the list currently has exactly one entry
    // per provider but the UI is list-shaped regardless.
    function renderModelRow() {
      modelWrap.innerHTML = '';
      const providerKey = providerSelect.value;
      const entry = providerByKey.get(providerKey) ?? null;
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
        opt.value = m.name;
        opt.textContent = m.name;
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

      if (current && models.some(m => m.name === current)) {
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
          params.model = customInput.value.trim() || null;
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

      params.model = select.value === '__custom__'
        ? (customInput.value.trim() || null)
        : (select.value || null);
    }
    renderModelRow();
    providerSelect.addEventListener('change', () => {
      params.provider = providerSelect.value || null;
      // Provider changed — old model string may not belong under the new
      // provider, so clear and let renderModelRow re-seed from the catalog.
      params.model = null;
      renderModelRow();
    });

    // ---- Fallback provider + read-only fallback model ----
    const fbProviderSelect = selectRow(
      form, 'fallback_provider',
      providerOptions.map(p => p.provider),
      String(params.fallback_provider ?? ''),
      '(none)',
    );
    const fbModelWrap = document.createElement('div');
    fbModelWrap.className = 'cfg-row';
    form.appendChild(fbModelWrap);

    // Parallel dropdown for fallback_model — same rationale as the primary
    // model row. Kept separate from renderModelRow so the two rows can
    // evolve independently (e.g. if one picks up a "none" sentinel).
    function renderFallbackModelRow() {
      fbModelWrap.innerHTML = '';
      const providerKey = fbProviderSelect.value;
      const entry = providerByKey.get(providerKey) ?? null;
      const models = entry?.models ?? [];
      const current = String(params.fallback_model ?? '');
      const label = document.createElement('label');
      label.className = 'cfg-label';
      label.textContent = 'fallback_model';
      fbModelWrap.appendChild(label);

      const fbSelect = document.createElement('select');
      fbSelect.className = 'cfg-input';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = entry ? `(default: ${entry.default_model})` : '(none)';
      fbSelect.appendChild(blank);
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.name;
        fbSelect.appendChild(opt);
      }
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = 'Custom…';
      fbSelect.appendChild(customOpt);

      const fbCustomInput = document.createElement('input');
      fbCustomInput.type = 'text';
      fbCustomInput.className = 'cfg-input cfg-input-custom';
      fbCustomInput.placeholder = 'custom model id';

      if (current && models.some(m => m.name === current)) {
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
    const maxIterInput = numberRow(form, 'max_iterations', Number(params.max_iterations ?? 1000));

    // ---- thinking_effort (collapsed: No / Medium / High) ----
    const thinkingSelect = selectRow(
      form, 'thinking_effort',
      ['No', 'Medium', 'High'],
      thinkingTier(params),
    );

    // ---- Plaintext asset / prompt cells ----
    const toolsTextarea = textareaRow(form, 'tools.md', toolsMd, 10);
    const skillsTextarea = textareaRow(form, 'skills.md', skillsMd, 4);
    const systemTextarea = textareaRow(form, 'prompts/system.md', systemMd, 14);

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
      const tier = thinkingSelect.value as 'No' | 'Medium' | 'High';
      const thinkingFields = tierToThinkingFields(tier);
      // model / fallback_model live on ``params`` — the dropdown-with-Custom
      // rows write through to params.model / params.fallback_model on every
      // change, so saveBtn just reads them out here.
      const next: Record<string, unknown> = {
        agent: agentInput.value,
        description: descInput.value,
        provider: providerSelect.value || null,
        model: (params.model as string | null) ?? null,
        fallback_provider: fbProviderSelect.value || null,
        fallback_model: (params.fallback_model as string | null) ?? null,
        max_iterations: Number(maxIterInput.value) || 1000,
        ...thinkingFields,
        prompts: params.prompts,
        tools: params.tools,
        skills: params.skills,
        duty: params.duty,
      };
      try {
        const [cfgRes] = await Promise.all([
          api.setConfig(sessionId, next as Params),
          api.setAssetMd(sessionId, 'tools', toolsTextarea.value),
          api.setAssetMd(sessionId, 'skills', skillsTextarea.value),
          api.setPromptMd(sessionId, 'system', systemTextarea.value),
        ]);
        if (store.currentSessionId !== sessionId) return;
        store.currentParams = cfgRes.params;
        assetCache.set(sessionId, {
          tools: toolsTextarea.value,
          skills: skillsTextarea.value,
          system: systemTextarea.value,
        });
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

  function textareaRow(parent: HTMLElement, key: string, value: string, rows: number): HTMLTextAreaElement {
    const row = document.createElement('div');
    row.className = 'cfg-row cfg-row-block';
    const label = document.createElement('label');
    label.className = 'cfg-label';
    label.textContent = key;
    const textarea = document.createElement('textarea');
    textarea.className = 'cfg-input cfg-textarea';
    textarea.rows = rows;
    textarea.value = value ?? '';
    row.appendChild(label);
    row.appendChild(textarea);
    parent.appendChild(row);
    return textarea;
  }

  // ================= PANEL TAB =================

  /** Build a synthetic sub_agent panel entry from a child session.
   *
   * Non-background sub-agent spawns don't write a real panel entry — the
   * SubAgentTool executor just blocks on the child and returns its reply.
   * The Panel surfaces them anyway by synthesising an entry from the
   * sessions list. Synthetic tids are prefixed with ``sa_`` so they don't
   * collide with real background tids (``bg_``). Bound to the child's
   * session id so the entry is stable across refreshes.
   */
  function syntheticSubAgentEntry(child: { id: string; mode?: string | null; display_name?: string | null; status: string; created_at: string | null; last_run_at: string | null; model_state: string }): PanelEntry {
    const isRunning = child.status !== 'stopped' && child.model_state === 'running';
    const status = (child.status === 'stopped' ? 'completed' : (isRunning ? 'running' : 'completed')) as PanelEntryStatus;
    const createdTs = child.created_at ? Date.parse(child.created_at) / 1000 : Date.now() / 1000;
    const lastTs = child.last_run_at ? Date.parse(child.last_run_at) / 1000 : createdTs;
    return {
      tid: `sa_${child.id}`,
      type: 'sub_agent',
      tool_name: 'sub_agent',
      input: {},
      status,
      created_at: createdTs,
      started_at: createdTs,
      finished_at: status === 'completed' ? lastTs : null,
      polling_interval: null,
      last_delivered_bytes: 0,
      last_activity_at: lastTs,
      pid: null,
      exit_code: null,
      output_file: null,
      output_bytes: 0,
      meta: {
        child_session_id: child.id,
        mode: child.mode ?? '',
        display_name: child.display_name ?? '',
        synthetic: true,
      },
    } as PanelEntry;
  }

  /** Merge real panel entries with synthetic entries for inline sub-agents.
   *
   * A sub-agent child is represented by a real panel entry only when its
   * parent ran `sub_agent` as a background tool. Inline calls block the
   * parent's turn and never write panel json, so the Panel would otherwise
   * miss them entirely. We deduplicate on child_session_id — if the same
   * child shows up in both sources, prefer the real entry (richer meta).
   */
  function mergedPanelEntries(): PanelEntry[] {
    const sid = store.currentSessionId;
    if (!sid) return store.panelEntries;

    const realChildren = new Set<string>();
    for (const e of store.panelEntries) {
      if (e.type === 'sub_agent') {
        const cid = String((e.meta ?? {}).child_session_id ?? '');
        if (cid) realChildren.add(cid);
      }
    }
    const synthetic: PanelEntry[] = [];
    for (const s of store.sessions) {
      if (s.parent_session_id !== sid) continue;
      if (realChildren.has(s.id)) continue;
      synthetic.push(syntheticSubAgentEntry(s));
    }
    // created_at ascending so new rows land at the bottom — matches how
    // list_entries() sorts real panel entries.
    return [...store.panelEntries, ...synthetic].sort(
      (a, b) => a.created_at - b.created_at,
    );
  }

  function renderPanelTab(): string {
    const entries = mergedPanelEntries();
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
    // Tid pill + action row live inside the expanded body only — see the
    // sub-agent branch for the same rationale (tid is a debug aid, not for
    // the always-visible summary).
    const tidLine = expanded
      ? `<div class="sub-agent-detail-row"><span class="cfg-label">task_id:</span> <span class="hb-pill">${escHtml(entry.tid)}</span></div>`
      : '';
    const actionsHtml = expanded
      ? `
        ${tidLine}
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
      // Synthetic rows cover inline sub-agents that never wrote a panel
      // entry, so the /panel/{tid} kill endpoint has nothing to target.
      // Expose "Stop child" instead — hits the sessions stop endpoint on
      // the child session directly, which the runtime already handles.
      const synthetic = Boolean(meta.synthetic);
      const killBtn = synthetic
        ? (childId && entry.status === 'running'
          ? `<button class="btn-sm" data-sub-action="stop-child" data-child-id="${escHtml(childId)}">Stop child</button>`
          : '')
        : `<button class="btn-sm" data-panel-action="kill" data-tid="${escHtml(entry.tid)}">Kill</button>`;
      // Surface tid inside the expanded body instead of the always-visible
      // summary. It's a debugging aid, not user-facing information, so it
      // doesn't belong in the scannable header.
      expandedHtml = `
        <div class="sub-agent-detail">
          <div class="sub-agent-detail-row"><span class="cfg-label">child:</span> <span class="hb-pill">${escHtml(childId)}</span></div>
          <div class="sub-agent-detail-row"><span class="cfg-label">task_id:</span> <span class="hb-pill">${escHtml(entry.tid)}</span></div>
          <div class="sub-agent-detail-row"><span class="cfg-label">recent activity (last 5):</span></div>
          ${recentBlock}
          ${resultBlock}
          <div class="task-card-actions">
            ${openLink}
            ${killBtn}
          </div>
        </div>
      `;
    }

    const modeChipClass = mode === 'explorer' || mode === 'executor' ? ` mode-${mode}` : '';
    return `
      <div class="task-card panel-row sub-agent-row${expanded ? ' expanded' : ''}" data-tid="${escHtml(entry.tid)}">
        <div class="task-card-header panel-row-header" data-tid="${escHtml(entry.tid)}" title="${escHtml(childId || entry.tid)}">
          <span class="task-status-badge ${statusClass}">${escHtml(entry.status)}</span>
          <span class="task-name">${escHtml(headerLabel)}</span>
          <span class="session-mode-chip${modeChipClass}">${escHtml(mode)}</span>
        </div>
        <div class="task-preview panel-tail" title="${escHtml(thumb)}">${thumb}</div>
        ${expandedHtml}
      </div>
    `;
  }

  /** Render one child activity row — tool call, tool output, or agent
   *  text output. Anything else (thinking, model_status, partial_text,
   *  loop_start, etc.) is filtered out by ``isDisplayableChildEvent`` so
   *  the sub-agent card only shows real user-visible activity.
   */
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
    if (t === 'agent_output_done' || t === 'agent_output_start') {
      return `<div class="sub-agent-evt"><span class="sub-agent-evt-icon">✎</span> output</div>`;
    }
    return '';
  }

  /** Keep only user-visible activity markers; drop thinking / status /
   *  partial deltas / housekeeping events. This runs before the slice so
   *  the visible 5 entries are all meaningful — picking the raw tail
   *  meant any busy turn would fill the card with ``model_status`` /
   *  ``partial_text`` noise.
   */
  function isDisplayableChildEvent(evt: Record<string, unknown>): boolean {
    const t = String(evt.type ?? '');
    if (t === 'tool_call' || t === 'tool' || t === 'tool_done') return true;
    if (t === 'agent_output_done' || t === 'agent_output_start') return true;
    return false;
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
        // Sub-agent rows: load the child's recent displayable activity on
        // first open. Fetch 60 raw events, filter to tool/output markers,
        // keep the last 5 — raw tail often fills with model_status /
        // partial_text spam so 5 raw entries rarely includes real activity.
        if (opening) {
          const entry = mergedPanelEntries().find(e => e.tid === tid);
          const childId = entry && entry.type === 'sub_agent'
            ? String((entry.meta ?? {}).child_session_id ?? '')
            : '';
          if (childId) {
            try {
              const raw = await api.getEventsTail(childId, 60);
              const filtered = raw.filter(isDisplayableChildEvent).slice(-5);
              subAgentChildEvents.set(tid, filtered);
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

    el.querySelectorAll('[data-sub-action="stop-child"]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const childId = (btn as HTMLElement).dataset.childId;
        if (!childId) return;
        try {
          await api.stopSession(childId);
        } catch (e) {
          console.error('Failed to stop child session:', e);
          return;
        }
        const sessions = await api.listSessions();
        store.sessions = sessions;
        store.emit('sessions');
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
      // Use the merged list so synthetic (inline sub-agent) entries also
      // get their child events refreshed.
      const merged = mergedPanelEntries();
      if (activeTab === 'panel' && expandedPanel.size > 0) {
        await Promise.all([...expandedPanel].map(async tid => {
          const entry = merged.find(e => e.tid === tid);
          if (entry?.type === 'sub_agent') {
            // Refresh the child's recent events while the sub-agent card is open.
            const childId = String((entry.meta ?? {}).child_session_id ?? '');
            if (childId) {
              try {
                const raw = await api.getEventsTail(childId, 60);
                if (store.currentSessionId !== sid) return;
                const filtered = raw.filter(isDisplayableChildEvent).slice(-5);
                subAgentChildEvents.set(tid, filtered);
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
  store.on('sessions', () => {
    // Synthetic sub-agent entries are derived from store.sessions —
    // re-render so newly-spawned inline sub-agents surface without waiting
    // for the next 2s panel poll, and finished ones flip to "completed".
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


