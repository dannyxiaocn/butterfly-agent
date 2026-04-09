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
    <div id="hud-bar" class="hud-bar hidden">
      <span class="hud-item hud-cwd" title="Working directory">
        <span class="hud-icon">📁</span>
        <span class="hud-cwd-text">…</span>
      </span>
      <span class="hud-sep">·</span>
      <span class="hud-item hud-context" title="Context size">
        <span class="hud-icon">💬</span>
        <span class="hud-ctx-text">…</span>
      </span>
      <span class="hud-sep">·</span>
      <span class="hud-item hud-git" title="Git changes">
        <span class="hud-icon">⎇</span>
        <span class="hud-git-text">…</span>
      </span>
      <span class="hud-sep">·</span>
      <span class="hud-item hud-tokens" title="Last turn token usage">
        <span class="hud-icon">⚡</span>
        <span class="hud-tokens-text">…</span>
      </span>
    </div>
    <div id="chat-input-area" class="chat-input-area">
      <textarea id="chat-input" placeholder="Type a message… (Shift+Enter for newline, Enter to send)" rows="3"></textarea>
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
        } else {
          // Idle: if no agent message came, remove bubble
          if (isStreaming) removeStreamingBubble();
          store.modelState = { state: 'idle', source: null };
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
        // Thinking block from completed turn — append permanently above agent text
        appendEvent(event);
        break;

      case 'agent':
        // Final response: remove streaming bubble, append real message
        removeStreamingBubble();
        appendEvent(event);
        break;

      default:
        appendEvent(event);
    }
  }

  async function refreshHud(sessionId: string) {
    try {
      const data = await api.getHud(sessionId);
      const hudBar = el.querySelector('#hud-bar') as HTMLElement;
      hudBar.classList.remove('hidden');

      // CWD: show full path, auto-scroll if it overflows the container
      const cwdEl = hudBar.querySelector('.hud-cwd-text') as HTMLElement;
      cwdEl.textContent = data.cwd;
      cwdEl.title = data.cwd;
      cwdEl.classList.remove('scrolling');
      cwdEl.style.removeProperty('--scroll-px');
      // Measure after paint to check overflow
      requestAnimationFrame(() => {
        const parent = cwdEl.parentElement;
        if (parent && cwdEl.scrollWidth > parent.clientWidth + 2) {
          const px = cwdEl.scrollWidth - parent.clientWidth + 8;
          cwdEl.style.setProperty('--scroll-px', `-${px}px`);
          cwdEl.classList.add('scrolling');
        }
      });

      // Context: estimate tokens from bytes, show as % of model max
      const estimatedTokens = Math.round(data.context_bytes / 4);
      const maxTokens = getModelMaxTokens(data.model ?? null);
      const pct = Math.min(100, Math.round(estimatedTokens / maxTokens * 100));
      const ctxStr = `${pct}% (${(estimatedTokens / 1000).toFixed(0)}k/${(maxTokens / 1000).toFixed(0)}k)`;
      (hudBar.querySelector('.hud-ctx-text') as HTMLElement).textContent = `ctx: ${ctxStr}`;

      // Git stat
      const gitEl = hudBar.querySelector('.hud-git-text') as HTMLElement;
      const { added, deleted, files } = data.git;
      if (files === 0) {
        gitEl.innerHTML = '<span style="color:var(--dimmed)">clean</span>';
      } else {
        gitEl.innerHTML = `${files}f <span class="hud-git-added">+${added}</span> <span class="hud-git-deleted">-${deleted}</span>`;
      }

      // Token usage
      const tokEl = hudBar.querySelector('.hud-tokens-text') as HTMLElement;
      if (data.usage) {
        const u = data.usage;
        const tokParts: string[] = [];
        if (u.input) tokParts.push(`in:${(u.input / 1000).toFixed(1)}k`);
        if (u.output) tokParts.push(`out:${(u.output / 1000).toFixed(1)}k`);
        if (u.cache_read) tokParts.push(`cache:${(u.cache_read / 1000).toFixed(1)}k`);
        tokEl.textContent = tokParts.join(' ');
      } else {
        tokEl.textContent = 'no usage';
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

  // Message batching: queue messages and flush after 300ms debounce,
  // joining with \n so rapid sends (or Enter-key multi-line) become one request.
  let pendingMessages: string[] = [];
  let sendTimer: ReturnType<typeof setTimeout> | null = null;

  async function flushPendingMessages() {
    if (pendingMessages.length === 0) return;
    const combined = pendingMessages.join('\n');
    pendingMessages = [];
    const sessId = store.currentSessionId;
    if (!sessId) return;
    try {
      await api.sendMessage(sessId, combined);
    } catch (e) {
      appendEvent({ type: 'error', content: `Failed to send: ${e}` });
    }
  }

  async function sendMessage() {
    const content = inputEl.value.trim();
    if (!content || !store.currentSessionId) return;
    const sess = store.currentSession;
    if (sess?.id.endsWith('_meta') || sess?.params?.is_meta_session) return;
    inputEl.value = '';
    inputEl.style.height = 'auto';
    pendingMessages.push(content);
    if (sendTimer) clearTimeout(sendTimer);
    sendTimer = setTimeout(flushPendingMessages, 300);
  }

  sendBtn.addEventListener('click', sendMessage);

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

  // Update input disabled state for meta sessions
  store.on('currentSession', () => {
    const sess = store.currentSession;
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
      const isHb = event.triggered_by === 'heartbeat';
      div.className = 'msg msg-agent';
      const label = isHb ? '⏱ agent' : 'agent';
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
      div.className = 'msg msg-tool';
      div.innerHTML = renderToolEvent(event);
      break;
    }

    case 'heartbeat_trigger': {
      div.className = 'msg msg-heartbeat-trigger';
      div.innerHTML = `<span>⏱ heartbeat triggered</span><span class="msg-ts">${formatTs(event.ts)}</span>`;
      break;
    }

    case 'heartbeat_finished': {
      div.className = 'msg msg-heartbeat-finished';
      div.innerHTML = `<em>[session finished]</em>`;
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

    default:
      return null;
  }

  return div;
}

function renderToolEvent(event: DisplayEvent): string {
  const name = event.name ?? 'unknown';
  const input = event.input ?? {};

  if (name === 'bash' || name === 'shell') {
    const cmd = String(input['command'] ?? input['cmd'] ?? JSON.stringify(input));
    const lineCount = cmd.split('\n').length;
    const isLong = cmd.length > 500 || lineCount > 10;
    const cmdHtml = isLong
      ? `<details class="tool-collapse"><summary>command (${lineCount} lines, ${cmd.length} chars)</summary><pre class="tool-pre">${escapeHtml(cmd)}</pre></details>`
      : `<pre class="tool-pre">${escapeHtml(cmd)}</pre>`;
    return `
      <div class="msg-header">
        <span class="tool-pill">${escapeHtml(name)}</span>
        <span class="msg-ts">${formatTs(event.ts)}</span>
      </div>
      ${cmdHtml}
    `;
  }

  if (name === 'web_search') {
    const query = String(input['query'] ?? input['q'] ?? '');
    return `
      <div class="msg-header">
        <span class="tool-pill">web_search</span>
        <span class="msg-ts">${formatTs(event.ts)}</span>
      </div>
      <div class="tool-query">🔍 ${escapeHtml(query)}</div>
    `;
  }

  // generic: key=value pairs
  const pairs = Object.entries(input)
    .slice(0, 8)
    .map(([k, v]) => {
      const val = typeof v === 'string' ? v : JSON.stringify(v);
      return `<span class="kv-pair"><span class="kv-key">${escapeHtml(k)}</span>=<span class="kv-val">${escapeHtml(val.slice(0, 120))}</span></span>`;
    })
    .join(' ');

  return `
    <div class="msg-header">
      <span class="tool-pill">${escapeHtml(name)}</span>
      <span class="msg-ts">${formatTs(event.ts)}</span>
    </div>
    <div class="tool-args">${pairs}</div>
  `;
}
