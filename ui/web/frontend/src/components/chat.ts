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
    <div id="pending-bar" class="pending-bar hidden">
      <span class="pending-label">queued</span>
      <span class="pending-preview"></span>
      <span class="pending-timer"></span>
      <button id="btn-send-now" class="btn-sm btn-primary" title="Send queued messages immediately">Send now</button>
      <button id="btn-interrupt-send" class="btn-sm btn-warn hidden" title="Cancel current agent run and send">Interrupt &amp; send</button>
    </div>
    <div id="chat-input-area" class="chat-input-area">
      <textarea id="chat-input" placeholder="Type a message… (Shift+Enter for newline, Enter to send — messages are merged over a 5 s window)" rows="3"></textarea>
      <div class="chat-input-actions">
        <button id="btn-interrupt" class="btn-sm btn-warn" title="Interrupt current turn">⚡ Interrupt</button>
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
  const pendingBar = el.querySelector('#pending-bar') as HTMLDivElement;
  const pendingPreview = el.querySelector('.pending-preview') as HTMLSpanElement;
  const pendingTimerEl = el.querySelector('.pending-timer') as HTMLSpanElement;
  const sendNowBtn = el.querySelector('#btn-send-now') as HTMLButtonElement;
  const interruptSendBtn = el.querySelector('#btn-interrupt-send') as HTMLButtonElement;

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
          // Agent idled: flush any pending merged message accumulated while streaming.
          if (pendingMessages.length > 0 && !sendTimer) {
            void flushPendingMessages();
          }
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

  // ==================== 5-s user-input merge window ====================
  //
  // State machine (web UI only; CLI is untouched):
  //
  //   IDLE  ──[user types+Enter]──▶  PENDING(timer=5s)
  //
  //   PENDING  ──[timer fires, agent idle]──▶  flush → IDLE
  //   PENDING  ──[timer fires, agent running]──▶  BUFFERED_WHILE_STREAMING
  //   PENDING  ──[another message typed]──▶  append to buffer, reset 5s timer
  //   PENDING  ──["Send now"]──▶  flush → IDLE
  //   PENDING  ──["Interrupt & send"]──▶  /interrupt + flush → IDLE
  //
  //   BUFFERED_WHILE_STREAMING  ──[model_status=idle]──▶  flush → IDLE
  //   BUFFERED_WHILE_STREAMING  ──[another message]──▶  append to buffer
  //                                                      (no timer — waits on agent)
  //   BUFFERED_WHILE_STREAMING  ──["Send now"]──▶  flush → IDLE
  //   BUFFERED_WHILE_STREAMING  ──["Interrupt & send"]──▶  /interrupt + flush
  //
  // Task-layer messages (scheduled duty fires) bypass this entirely — they
  // go directly to the backend via the task runtime, not through this path.
  // CLI chat also bypasses; this merge is strictly front-end driven.
  //
  // Auto-interrupt heuristic: if the buffered content starts with a
  // correction phrase ("stop", "wait", "no", "cancel"), promote the pending
  // "Send now" into an "Interrupt & send". Otherwise default to merge-and-send.
  const MERGE_WINDOW_MS = 5000;
  // Bilingual correction phrases — project is EN/ZH per memory. Word boundary
  // only applies to ASCII; CJK terms fall back to plain prefix match.
  const INTERRUPT_PHRASES = /^\s*(stop|wait|no|cancel|nope|hold on|等等|等一下|停|取消|别|算了)/i;

  let pendingMessages: string[] = [];
  let pendingSessionId: string | null = null;
  let sendTimer: ReturnType<typeof setTimeout> | null = null;
  let timerDeadline = 0;
  let timerTickHandle: ReturnType<typeof setInterval> | null = null;

  function isAgentRunning(): boolean {
    return store.modelState.state === 'running';
  }

  function updatePendingBar() {
    if (pendingMessages.length === 0) {
      pendingBar.classList.add('hidden');
      if (timerTickHandle) { clearInterval(timerTickHandle); timerTickHandle = null; }
      return;
    }
    pendingBar.classList.remove('hidden');
    const combined = pendingMessages.join('\n');
    const preview = combined.length > 80 ? combined.slice(0, 80) + '…' : combined;
    pendingPreview.textContent = preview;

    if (isAgentRunning()) {
      pendingTimerEl.textContent = 'waiting for agent…';
      interruptSendBtn.classList.remove('hidden');
      if (timerTickHandle) { clearInterval(timerTickHandle); timerTickHandle = null; }
    } else if (timerDeadline > 0) {
      const remaining = Math.max(0, Math.ceil((timerDeadline - Date.now()) / 1000));
      pendingTimerEl.textContent = `sending in ${remaining}s`;
      interruptSendBtn.classList.add('hidden');
    }

    // Auto-interrupt heuristic
    if (INTERRUPT_PHRASES.test(combined) && isAgentRunning()) {
      interruptSendBtn.classList.add('pulse');
    } else {
      interruptSendBtn.classList.remove('pulse');
    }
  }

  function startTimerTick() {
    if (timerTickHandle) clearInterval(timerTickHandle);
    timerTickHandle = setInterval(updatePendingBar, 500);
  }

  async function flushPendingMessages() {
    if (pendingMessages.length === 0) return;
    const combined = pendingMessages.join('\n');
    const sessId = pendingSessionId;
    pendingMessages = [];
    pendingSessionId = null;
    timerDeadline = 0;
    if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
    updatePendingBar();
    if (!sessId) return;
    try {
      await api.sendMessage(sessId, combined);
    } catch (e) {
      console.error('Failed to send merged user message:', e);
      appendEvent({ type: 'error', content: `Failed to send: ${e}` });
    }
  }

  async function interruptAndFlush() {
    const sessId = pendingSessionId ?? store.currentSessionId;
    if (!sessId) return;
    try {
      await api.interruptSession(sessId);
    } catch (e) {
      console.error('Interrupt failed:', e);
    }
    await flushPendingMessages();
  }

  async function sendMessage() {
    const content = inputEl.value.trim();
    if (!content || !store.currentSessionId) return;
    const sess = store.currentSession;
    if (sess?.id.endsWith('_meta') || sess?.params?.is_meta_session) return;
    const sessId = store.currentSessionId; // capture NOW, not at flush time
    inputEl.value = '';
    inputEl.style.height = 'auto';
    // If session changed mid-batch, flush previous batch to its original session first
    if (pendingSessionId && pendingSessionId !== sessId) {
      if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
      void flushPendingMessages();
    }
    pendingMessages.push(content);
    pendingSessionId = sessId;

    if (isAgentRunning()) {
      // Agent is mid-reply — hold the buffer, don't arm the 5s timer.
      // It will be flushed on model_status=idle.
      if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
      timerDeadline = 0;
    } else {
      // Agent idle — arm or reset the 5s merge timer.
      if (sendTimer) clearTimeout(sendTimer);
      timerDeadline = Date.now() + MERGE_WINDOW_MS;
      sendTimer = setTimeout(() => {
        sendTimer = null;
        timerDeadline = 0;
        // Re-check: by the time the timer fires, the agent might have started.
        if (isAgentRunning()) {
          updatePendingBar();
          return;
        }
        void flushPendingMessages();
      }, MERGE_WINDOW_MS);
      startTimerTick();
    }
    updatePendingBar();
  }

  sendBtn.addEventListener('click', sendMessage);
  sendNowBtn.addEventListener('click', () => {
    if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
    void flushPendingMessages();
  });
  interruptSendBtn.addEventListener('click', () => {
    if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
    void interruptAndFlush();
  });

  // Chinese IME: track composition state so Enter that confirms a candidate
  // does not also trigger message send.
  let isComposing = false;
  inputEl.addEventListener('compositionstart', () => { isComposing = true; });
  inputEl.addEventListener('compositionend', () => { isComposing = false; });

  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !isComposing) {
      e.preventDefault();
      sendMessage();
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

  // Update input disabled state for meta sessions. Also flush any pending
  // buffer that belongs to a DIFFERENT session — otherwise switching mid-5s
  // window leaves a stale pending bar visible on the new session that
  // targets the previous session ("Send now" / "Interrupt & send" would act
  // on the wrong session — silent footgun flagged in PR #24 review).
  store.on('currentSession', () => {
    const sess = store.currentSession;
    const newSessionId = sess?.id ?? null;
    if (pendingSessionId && pendingSessionId !== newSessionId) {
      if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
      // Fire-and-forget: the send targets the original session id captured
      // in pendingSessionId, not the newly-selected one.
      void flushPendingMessages();
    }
    const isMeta = sess?.id.endsWith('_meta') || sess?.params?.is_meta_session;
    inputEl.disabled = !!isMeta;
    sendBtn.disabled = !!isMeta;
    inputEl.placeholder = isMeta ? 'Direct chat with meta sessions is disabled.' : 'Type a message… (Shift+Enter for newline, Enter to send)';
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
