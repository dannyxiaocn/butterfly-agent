import { store } from '../store';
import { api } from '../api';
import type { DisplayEvent } from '../types';
import { renderMarkdown, escapeHtml, formatTs } from '../markdown';

const _MODEL_MAX_TOKENS: [string, number][] = [
  ['claude', 200000],
  ['gpt-5.4', 200000],
  ['gpt-5', 200000],
  ['gpt-4o', 128000],
  ['gpt-4', 128000],
  ['kimi', 128000],
];

function getModelMaxTokens(model: string | null): number {
  if (!model) return 200000;
  const m = model.toLowerCase();
  for (const [key, val] of _MODEL_MAX_TOKENS) {
    if (m.includes(key)) return val;
  }
  return 200000;
}

export function createChat(): HTMLElement {
  const el = document.createElement('main');
  el.id = 'chat';
  el.innerHTML = `
    <div id="messages" class="messages"></div>
    <div id="hud-bar" class="hud-bar hidden" title="model · context · running tool · tokens">
      <span class="hud-dot hud-dot-idle" id="hud-dot"></span>
      <span class="hud-item hud-model"><span class="hud-model-text">…</span></span>
      <span class="hud-sep">·</span>
      <span class="hud-item hud-context"><span class="hud-ctx-text">…</span></span>
      <span class="hud-sep hud-tool-sep hidden">·</span>
      <span class="hud-item hud-tool hidden"><span class="hud-tool-text"></span></span>
      <span class="hud-sep hud-tokens-sep">·</span>
      <span class="hud-item hud-tokens"><span class="hud-tokens-text">…</span></span>
    </div>
    <div id="chat-input-area" class="chat-input-area">
      <textarea id="chat-input" placeholder="Type a message… (Enter = send, Shift+Enter = newline, Alt/⌥+Enter = wait-mode)" rows="3"></textarea>
      <div class="chat-input-actions">
        <button id="btn-interrupt" class="btn-sm btn-warn" title="Bare interrupt — cancel current run, send nothing">⚡ Interrupt</button>
        <label class="send-mode-toggle" title="Wait: queue behind in-flight run instead of interrupting it">
          <input type="checkbox" id="chk-wait-mode" />
          <span>wait</span>
        </label>
        <div class="chat-input-actions-right">
          <button id="btn-send" class="btn-primary">Send</button>
        </div>
      </div>
    </div>
  `;

  const messages = el.querySelector('#messages') as HTMLDivElement;
  const inputEl = el.querySelector('#chat-input') as HTMLTextAreaElement;
  const sendBtn = el.querySelector('#btn-send') as HTMLButtonElement;
  const interruptBtn = el.querySelector('#btn-interrupt') as HTMLButtonElement;
  const waitModeChk = el.querySelector('#chk-wait-mode') as HTMLInputElement;

  // Track in-flight tool by name so the HUD can show "▶ bash" during a call.
  // Also held per msg-tool DOM node for the running/finished state transition.
  const runningTools = new Map<string, { el: HTMLElement | null; startTs: number }>();
  let latestToolKey: string | null = null;

  // Thinking cells: map block_id → running DOM element. On thinking_start we
  // insert a placeholder "💭 Thinking…" cell; on thinking_done we flip it to
  // the collapsed "💭 Thought for Xs" state with the full body inside a
  // <details> element. Mirrors the msg-tool running/done lifecycle.
  const runningThinking = new Map<string, HTMLElement>();

  // Streaming bubble lives INSIDE the messages div so it scrolls with the conversation
  let streamingEl: HTMLDivElement | null = null;
  let isStreaming = false;

  function getOrCreateStreamingBubble(): HTMLDivElement {
    if (!streamingEl) {
      streamingEl = document.createElement('div');
      streamingEl.className = 'msg msg-agent msg-streaming';
      streamingEl.innerHTML = `
        <div class="msg-header">
          <span class="msg-label">agent</span>
          <span class="streaming-badge">
            <span class="streaming-dot"></span><span class="streaming-dot"></span><span class="streaming-dot"></span>
            <span class="streaming-label">generating…</span>
          </span>
        </div>
        <div class="msg-body msg-streaming-body markdown-body"></div>
      `;
      messages.appendChild(streamingEl);
    }
    return streamingEl;
  }

  function removeStreamingBubble() {
    if (streamingEl) {
      streamingEl.remove();
      streamingEl = null;
    }
    isStreaming = false;
  }

  function clearMessages() {
    removeStreamingBubble();
    messages.innerHTML = '';
    isStreaming = false;
    runningTools.clear();
    latestToolKey = null;
    runningThinking.clear();
    updateHudTool(null);
    updateHudDot('idle');
  }

  function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function appendEvent(event: DisplayEvent) {
    const msgEl = renderEvent(event);
    if (msgEl) {
      // Keep streaming bubble at the bottom: insert new events before it when streaming
      if (streamingEl && messages.contains(streamingEl)) {
        messages.insertBefore(msgEl, streamingEl);
      } else {
        messages.appendChild(msgEl);
      }
      scrollToBottom();
    }
  }

  function handleEvent(event: DisplayEvent) {
    switch (event.type) {
      case 'model_status':
        if (event.state === 'running') {
          // Show streaming bubble with dots (no text yet)
          isStreaming = true;
          const bubble = getOrCreateStreamingBubble();
          const body = bubble.querySelector('.msg-streaming-body') as HTMLElement;
          body.innerHTML = '';
          scrollToBottom();
          store.modelState = { state: 'running', source: event.source ?? null };
          updateHudDot('running');
        } else {
          // Idle: if no agent message came, remove bubble
          if (isStreaming) removeStreamingBubble();
          store.modelState = { state: 'idle', source: null };
          updateHudDot('idle');
          // v2.0.12: backend dispatcher owns the queue/merge logic now,
          // so the frontend has no pending buffer to flush here. The 5 s
          // merge window was removed (see PR review note: keeping merging
          // server-side avoids a stale per-tab buffer when multiple
          // browsers send concurrently).
        }
        store.emit('modelState');
        break;

      case 'partial_text':
        // Live-update the streaming bubble body with the thinking text
        if (!isStreaming) isStreaming = true;
        {
          const bubble = getOrCreateStreamingBubble();
          const body = bubble.querySelector('.msg-streaming-body') as HTMLElement;
          if (event.content) {
            body.innerHTML = renderMarkdown(event.content);
          }
          scrollToBottom();
        }
        break;

      case 'thinking':
        // Thinking block from a COMPLETED turn (history replay or legacy
        // session where thinking_start/thinking_done weren't emitted).
        // Append as a collapsed cell above the agent text.
        appendEvent(event);
        break;

      case 'thinking_start': {
        const blockId = event.block_id ?? `th:${Date.now()}`;
        // If an older cell for this id somehow exists, replace it.
        const existing = runningThinking.get(blockId);
        if (existing) existing.remove();
        const cell = document.createElement('div');
        cell.className = 'msg msg-thinking msg-thinking-running';
        cell.dataset.blockId = blockId;
        cell.dataset.startedAt = String(Date.now());
        cell.innerHTML = `
          <div class="tool-row-header">
            <div class="tool-status-summary">
              <span class="tool-status-icon">💭</span>
              <span class="tool-status-name">Thinking…</span>
              <span class="tool-status-meta"><span class="streaming-dot"></span><span class="streaming-dot"></span><span class="streaming-dot"></span></span>
            </div>
            <span class="msg-ts">${formatTs(event.ts)}</span>
          </div>
        `;
        // Place it above the streaming bubble (same rule as tool cells).
        if (streamingEl && messages.contains(streamingEl)) {
          messages.insertBefore(cell, streamingEl);
        } else {
          messages.appendChild(cell);
        }
        runningThinking.set(blockId, cell);
        scrollToBottom();
        break;
      }

      case 'thinking_done': {
        const blockId = event.block_id ?? '';
        let cell = blockId ? runningThinking.get(blockId) ?? null : null;
        if (!cell && blockId) {
          // Reconnect path: fell out of the live map; try DOM lookup.
          cell = messages.querySelector<HTMLElement>(`.msg-thinking[data-block-id="${CSS.escape(blockId)}"]`);
        }
        const durMs = typeof event.duration_ms === 'number'
          ? event.duration_ms
          : (cell?.dataset.startedAt ? Date.now() - Number(cell.dataset.startedAt) : 0);
        const durSec = (durMs / 1000).toFixed(1) + 's';
        const body = event.text ?? '';
        const bodyHtml = body
          ? `<div class="thinking-body markdown-body">${renderMarkdown(body)}</div>`
          : `<div class="thinking-body thinking-empty"><em>No thinking body exposed by the provider.</em></div>`;
        const html = `
          <details class="thinking-details">
            <summary class="thinking-summary">
              <span class="thinking-icon">💭</span>
              <span class="thinking-label">Thought for ${escapeHtml(durSec)}</span>
              <span class="thinking-toggle-hint"></span>
            </summary>
            ${bodyHtml}
          </details>
        `;
        if (cell) {
          cell.classList.remove('msg-thinking-running');
          cell.classList.add('msg-thinking-done');
          cell.innerHTML = html;
        } else {
          // No running cell (e.g. out-of-order delivery) — append a done cell.
          const doneCell = document.createElement('div');
          doneCell.className = 'msg msg-thinking msg-thinking-done';
          if (blockId) doneCell.dataset.blockId = blockId;
          doneCell.innerHTML = html;
          if (streamingEl && messages.contains(streamingEl)) {
            messages.insertBefore(doneCell, streamingEl);
          } else {
            messages.appendChild(doneCell);
          }
        }
        if (blockId) runningThinking.delete(blockId);
        scrollToBottom();
        break;
      }

      case 'agent':
        // Final response: remove streaming bubble, append real message
        removeStreamingBubble();
        appendEvent(event);
        // The 'agent' event carries the turn's token usage — update the HUD
        // pill inline so tokens don't freeze between explicit refreshHud()
        // calls (flagged in PR #24 review item 8).
        if (event.usage) updateHudTokens(event.usage);
        break;

      case 'tool': {
        // Record running tool so tool_done can pair against it. Events don't
        // carry a tool_use_id yet, so we key by name — last wins.
        const name = event.name ?? 'tool';
        latestToolKey = name;
        runningTools.set(name, { el: null, startTs: Date.now() });
        updateHudTool(name);
        // Render via the normal path — renderEvent defaults to "done" styling
        // so history replays look correct. Flip the freshly appended row back
        // to the running state here (live path only).
        appendEvent(event);
        const lastTool = messages.querySelector('.msg-tool:last-of-type') as HTMLElement | null;
        if (lastTool) {
          lastTool.classList.remove('done');
          const summary = lastTool.querySelector('.tool-status-summary') as HTMLElement | null;
          if (summary) {
            summary.innerHTML = `<span class="tool-status-icon">▶</span><span class="tool-status-name">${escapeHtml(name)}</span><span class="tool-status-meta">running…</span>`;
          }
        }
        break;
      }

      case 'tool_done': {
        const name = event.name ?? (latestToolKey ?? 'tool');
        const entry = runningTools.get(name);
        const started = entry?.startTs ?? Date.now();
        const durationMs = Date.now() - started;
        // Attach finished state onto the most recent msg-tool for this name.
        const toolEls = Array.from(messages.querySelectorAll('.msg-tool')) as HTMLElement[];
        const target = toolEls
          .reverse()
          .find(n => n.dataset.toolName === name && !n.classList.contains('done'));
        if (target) {
          target.classList.add('done');
          const durSec = (durationMs / 1000).toFixed(1) + 's';
          const summary = target.querySelector('.tool-status-summary') as HTMLElement | null;
          if (summary) {
            summary.innerHTML = `<span class="tool-status-icon">✓</span><span class="tool-status-name">${escapeHtml(name)}</span><span class="tool-status-meta">${escapeHtml(durSec)}${typeof event.result_len === 'number' ? ` · ${event.result_len} chars` : ''}</span>`;
          }
        }
        runningTools.delete(name);
        if (latestToolKey === name) latestToolKey = null;
        updateHudTool(latestToolKey);
        // Intentionally NOT appending a separate msg-status line — the running
        // pill transitions to done in place. Memory note: keeps the log quiet.
        break;
      }

      case 'loop_start':
      case 'loop_end':
        // Quiet: loop lifecycle no longer clutters the transcript (user feedback).
        // HUD's running dot already conveys the information.
        break;

      default:
        appendEvent(event);
    }
  }

  function updateHudDot(state: 'running' | 'idle') {
    const dot = el.querySelector('#hud-dot') as HTMLElement | null;
    if (!dot) return;
    dot.classList.toggle('hud-dot-running', state === 'running');
    dot.classList.toggle('hud-dot-idle', state === 'idle');
  }

  function updateHudTool(toolName: string | null) {
    const sep = el.querySelector('.hud-tool-sep') as HTMLElement | null;
    const item = el.querySelector('.hud-tool') as HTMLElement | null;
    const text = el.querySelector('.hud-tool-text') as HTMLElement | null;
    if (!sep || !item || !text) return;
    if (toolName) {
      sep.classList.remove('hidden');
      item.classList.remove('hidden');
      text.textContent = `▶ ${toolName}`;
    } else {
      sep.classList.add('hidden');
      item.classList.add('hidden');
      text.textContent = '';
    }
  }

  function formatHudUsageTooltip(u: NonNullable<DisplayEvent['usage']>): string {
    // Usage is summed across all LLM calls in the turn — cache_read in
    // particular accumulates the same cached prefix N times, so a 30k input
    // turn with 12 iterations can show 200k+ cache_read. Label it explicitly
    // so the number isn't misread as a single-call figure.
    const full: string[] = [];
    if (u.input) full.push(`in:${u.input}`);
    if (u.output) full.push(`out:${u.output}`);
    if (u.cache_read) full.push(`cache_read:${u.cache_read}`);
    if (u.cache_write) full.push(`cache_write:${u.cache_write}`);
    if (u.reasoning) full.push(`reasoning:${u.reasoning}`);
    return full.length ? `turn total (sum of all LLM calls)\n${full.join(' · ')}` : 'no usage';
  }

  function updateHudTokens(usage: NonNullable<DisplayEvent['usage']>) {
    const hudBar = el.querySelector('#hud-bar') as HTMLElement | null;
    if (!hudBar) return;
    const tokEl = hudBar.querySelector('.hud-tokens-text') as HTMLElement | null;
    if (!tokEl) return;
    const inK = usage.input ? (usage.input / 1000).toFixed(1) + 'k' : '—';
    const outK = usage.output ? (usage.output / 1000).toFixed(1) + 'k' : '—';
    tokEl.textContent = `${inK}↓ ${outK}↑`;
    tokEl.title = formatHudUsageTooltip(usage);
  }

  async function refreshHud(sessionId: string) {
    try {
      const data = await api.getHud(sessionId);
      const hudBar = el.querySelector('#hud-bar') as HTMLElement;
      hudBar.classList.remove('hidden');

      // Model name — compact, single-line. This is the "essential" field.
      const modelEl = hudBar.querySelector('.hud-model-text') as HTMLElement;
      const modelName = data.model ?? '(default)';
      modelEl.textContent = modelName;
      modelEl.title = `model: ${modelName} · cwd: ${data.cwd}`;

      // Context: estimate tokens from bytes, show as % of model max
      const estimatedTokens = Math.round(data.context_bytes / 4);
      const maxTokens = getModelMaxTokens(data.model ?? null);
      const pct = Math.min(100, Math.round(estimatedTokens / maxTokens * 100));
      const ctxEl = hudBar.querySelector('.hud-ctx-text') as HTMLElement;
      ctxEl.textContent = `ctx ${pct}%`;
      ctxEl.title = `${estimatedTokens.toLocaleString()} / ${maxTokens.toLocaleString()} tokens`;

      // Token usage — collapse to one number unless caching meaningfully present.
      const tokEl = hudBar.querySelector('.hud-tokens-text') as HTMLElement;
      if (data.usage) {
        const u = data.usage;
        const inK = u.input ? (u.input / 1000).toFixed(1) + 'k' : '—';
        const outK = u.output ? (u.output / 1000).toFixed(1) + 'k' : '—';
        tokEl.textContent = `${inK}↓ ${outK}↑`;
        tokEl.title = formatHudUsageTooltip(u);
      } else {
        tokEl.textContent = '—';
        tokEl.title = 'no usage yet';
      }
    } catch {
      // ignore — HUD is best-effort
    }
  }

  // Expose methods to main.ts
  type ChatMethods = {
    clearMessages(): void;
    appendEvent(e: DisplayEvent): void;
    handleEvent(e: DisplayEvent): void;
    refreshHud(id: string): Promise<void>;
  };
  (el as HTMLElement & ChatMethods).clearMessages = clearMessages;
  (el as HTMLElement & ChatMethods).appendEvent = appendEvent;
  (el as HTMLElement & ChatMethods).handleEvent = handleEvent;
  (el as HTMLElement & ChatMethods).refreshHud = refreshHud;

  // ==================== Send (delegates merge/cancel to backend) ============
  //
  // v2.0.12: the 5 s frontend merge window + pending bar are gone. The
  // backend dispatcher (Session._consumer_loop) owns merging and
  // interruption — see docs/butterfly/session_engine/design.md §"Input
  // dispatcher". Each Enter sends one POST /messages with a mode field;
  // the daemon decides whether to interrupt+merge, queue, or wait-merge.
  //
  // UI conventions:
  //   • Default ``Send`` (Enter / button) → mode=interrupt. Sends with
  //     interrupt semantics: cancels the in-flight run when uncommitted
  //     and merges the cancelled input with the new one server-side.
  //   • Wait toggle / Alt+Enter → mode=wait. Queues behind the in-flight
  //     run; consecutive wait sends collapse into a single user turn.
  //   • The bare ⚡ Interrupt button cancels with nothing in its place
  //     (POST /interrupt) — distinct from a chat-with-mode=interrupt.
  //
  // No per-tab pending buffer / timer is kept anymore: a stale buffer
  // bound to a tab's `pendingSessionId` was the source of the multi-tab
  // footgun flagged in PR #24 review — moving merge to the daemon
  // sidesteps that whole class of bug.

  async function sendMessage(modeOverride?: 'interrupt' | 'wait') {
    const content = inputEl.value.trim();
    if (!content || !store.currentSessionId) return;
    const sess = store.currentSession;
    if (sess?.id.endsWith('_meta') || sess?.params?.is_meta_session) return;
    const sessId = store.currentSessionId;
    const mode: 'interrupt' | 'wait' = modeOverride ?? (waitModeChk.checked ? 'wait' : 'interrupt');
    inputEl.value = '';
    inputEl.style.height = 'auto';
    try {
      await api.sendMessage(sessId, content, mode);
    } catch (e) {
      console.error('Failed to send user message:', e);
      appendEvent({ type: 'error', content: `Failed to send: ${e}` });
    }
  }

  sendBtn.addEventListener('click', () => { void sendMessage(); });

  // Chinese IME: track composition state so Enter that confirms a candidate
  // does not also trigger message send.
  let isComposing = false;
  inputEl.addEventListener('compositionstart', () => { isComposing = true; });
  inputEl.addEventListener('compositionend', () => { isComposing = false; });

  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !isComposing) {
      e.preventDefault();
      // Alt/⌥+Enter: explicit one-shot wait-mode send (independent of toggle)
      void sendMessage(e.altKey ? 'wait' : undefined);
    }
  });
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
  });

  interruptBtn.addEventListener('click', async () => {
    if (!store.currentSessionId) return;
    await api.interruptSession(store.currentSessionId).catch(console.error);
  });

  store.on('currentSession', () => {
    const sess = store.currentSession;
    const isMeta = sess?.id.endsWith('_meta') || sess?.params?.is_meta_session;
    inputEl.disabled = !!isMeta;
    sendBtn.disabled = !!isMeta;
    inputEl.placeholder = isMeta
      ? 'Direct chat with meta sessions is disabled.'
      : 'Type a message… (Enter = send, Shift+Enter = newline, Alt/⌥+Enter = wait-mode)';
  });

  return el;
}

function renderEvent(event: DisplayEvent): HTMLElement | null {
  const div = document.createElement('div');

  switch (event.type) {
    case 'thinking': {
      if (!event.content) return null;
      div.className = 'msg msg-thinking';
      div.innerHTML = `
        <details class="thinking-details">
          <summary class="thinking-summary">
            <span class="thinking-icon">💭</span>
            <span class="thinking-label">Thinking</span>
            <span class="thinking-toggle-hint"></span>
          </summary>
          <div class="thinking-body markdown-body">${renderMarkdown(event.content)}</div>
        </details>
      `;
      break;
    }

    case 'agent': {
      if (!event.content) return null;
      const isTask = event.triggered_by?.startsWith('task:');
      div.className = 'msg msg-agent';
      const label = isTask ? '⏱ agent' : 'agent';
      let usageHtml = '';
      if (event.usage) {
        const u = event.usage;
        const parts: string[] = [];
        if (u.input != null) parts.push(`in:${u.input}`);
        if (u.output != null) parts.push(`out:${u.output}`);
        if (u.cache_read != null) parts.push(`cached:${u.cache_read}`);
        if (u.cache_write != null) parts.push(`wrote:${u.cache_write}`);
        if (parts.length) usageHtml = `<span class="usage-stats">${escapeHtml(parts.join(' · '))}</span>`;
      }
      div.innerHTML = `
        <div class="msg-header">
          <span class="msg-label">${escapeHtml(label)}</span>
          ${usageHtml}
          <span class="msg-ts">${formatTs(event.ts)}</span>
        </div>
        <div class="msg-body markdown-body">${renderMarkdown(event.content)}</div>
      `;
      break;
    }

    case 'user': {
      if (!event.content) return null;
      div.className = 'msg msg-user';
      div.innerHTML = `
        <div class="msg-header">
          <span class="msg-label">you</span>
          <span class="msg-ts">${formatTs(event.ts)}</span>
        </div>
        <div class="msg-body markdown-body">${renderMarkdown(event.content)}</div>
      `;
      break;
    }

    case 'tool': {
      // Default to "done" styling because renderEvent is also used by history
      // replay (completed turns). handleEvent's live `tool` case explicitly
      // strips the .done class after appending to show the running state.
      div.className = 'msg msg-tool msg-tool-compact done';
      div.dataset.toolName = event.name ?? 'tool';
      div.innerHTML = renderToolEvent(event);
      break;
    }

    // tool_done / loop_start / loop_end are handled in handleEvent (transition
    // the live msg-tool to "done" and update the HUD); they don't produce
    // separate log lines anymore. This keeps the transcript quiet and
    // matches the uniform `✓ name (duration)` pattern the user asked for.

    case 'task_wakeup': {
      div.className = 'msg msg-task-wakeup';
      div.innerHTML = `<span>⏱ task wakeup${event.card ? `: ${escapeHtml(event.card)}` : ''}</span><span class="msg-ts">${formatTs(event.ts)}</span>`;
      break;
    }

    case 'task_finished': {
      div.className = 'msg msg-task-finished';
      div.innerHTML = `<em>[task finished${event.card ? `: ${escapeHtml(event.card)}` : ''}]</em>`;
      break;
    }

    case 'status': {
      div.className = 'msg msg-status';
      div.innerHTML = `<em>${escapeHtml(event.value ?? '')}</em>`;
      break;
    }

    case 'error': {
      div.className = 'msg msg-error';
      div.innerHTML = `<span class="msg-label">error</span><div class="msg-body">${escapeHtml(event.content ?? '')}</div>`;
      break;
    }

    case 'system_notice': {
      div.className = 'msg msg-system-notice';
      div.innerHTML = `<span class="msg-label">notice</span><div class="msg-body">${escapeHtml(event.message ?? '')}</div>`;
      break;
    }

    default:
      return null;
  }

  return div;
}

function renderToolEvent(event: DisplayEvent): string {
  // Uniform compact layout:
  //   [summary row] ▶ toolname  <one-line arg preview>      (ts)
  //   [optional] click-to-expand <details> with full args / command
  //
  // When tool_done lands, chat.ts flips the summary row to `✓ name (Xs)`.
  const name = event.name ?? 'unknown';
  const input = event.input ?? {};
  const preview = toolArgPreview(name, input);
  const expanded = toolArgExpanded(name, input);

  // Default icon is the "done" checkmark; handleEvent's live `tool` case
  // overrides this to show `▶ running…` until tool_done lands.
  const summary = `
    <div class="tool-status-summary">
      <span class="tool-status-icon">✓</span>
      <span class="tool-status-name">${escapeHtml(name)}</span>
      <span class="tool-status-meta"></span>
    </div>
  `;

  const previewLine = preview
    ? `<div class="tool-arg-preview" title="${escapeHtml(preview)}">${escapeHtml(preview)}</div>`
    : '';
  const detailsBlock = expanded
    ? `<details class="tool-collapse"><summary>details</summary>${expanded}</details>`
    : '';

  return `
    <div class="tool-row-header">
      ${summary}
      <span class="msg-ts">${formatTs(event.ts)}</span>
    </div>
    ${previewLine}
    ${detailsBlock}
  `;
}

function toolArgPreview(name: string, input: Record<string, unknown>): string {
  if (name === 'bash' || name === 'shell') {
    const cmd = String(input['command'] ?? input['cmd'] ?? '');
    const firstLine = cmd.split('\n')[0] ?? '';
    return firstLine.length > 120 ? firstLine.slice(0, 120) + '…' : firstLine;
  }
  if (name === 'web_search') {
    return `🔍 ${String(input['query'] ?? input['q'] ?? '')}`;
  }
  if (name === 'read' || name === 'edit' || name === 'write') {
    const p = String(input['file_path'] ?? input['path'] ?? '');
    return p;
  }
  // Generic: first kv pair
  const entries = Object.entries(input);
  if (!entries.length) return '';
  const [k, v] = entries[0];
  const val = typeof v === 'string' ? v : JSON.stringify(v);
  return `${k}=${val.slice(0, 100)}${val.length > 100 ? '…' : ''}`;
}

function toolArgExpanded(name: string, input: Record<string, unknown>): string {
  if (name === 'bash' || name === 'shell') {
    const cmd = String(input['command'] ?? input['cmd'] ?? JSON.stringify(input));
    return `<pre class="tool-pre">${escapeHtml(cmd)}</pre>`;
  }
  const entries = Object.entries(input);
  if (!entries.length) return '';
  const rows = entries
    .map(([k, v]) => {
      const val = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
      return `<div class="kv-row"><span class="kv-key">${escapeHtml(k)}</span><pre class="kv-val">${escapeHtml(val)}</pre></div>`;
    })
    .join('');
  return `<div class="tool-args-expanded">${rows}</div>`;
}
