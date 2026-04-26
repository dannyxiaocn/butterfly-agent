// Chat pane: HUD + cards list + input box. The cards list is a pure
// projection of store.cards; the reducer is the only thing that mutates
// that list (DESIGN.md §8.1).
import { api } from '../api';
import { store } from '../store';
import { cardPropsFor, renderCard } from '../card';
import type { HudSnapshot } from '../types';

export function createChat(): HTMLElement {
  const el = document.createElement('main');
  el.id = 'chat';
  el.innerHTML = `
    <div id="hud-bar" class="hud-bar hidden"></div>
    <div id="messages" class="messages"></div>
    <div id="chat-input-area" class="chat-input-area">
      <textarea id="chat-input" placeholder="Type a message…  (Enter sends · Shift+Enter newline)" rows="3"></textarea>
      <div class="chat-input-actions">
        <button id="btn-interrupt" class="btn-sm btn-warn" title="Interrupt in-flight run">⚡ Interrupt</button>
        <button id="btn-send" class="btn-sm btn-primary">Send</button>
      </div>
    </div>
  `;

  const messagesEl = el.querySelector('#messages') as HTMLDivElement;
  const hudEl = el.querySelector('#hud-bar') as HTMLDivElement;
  const inputEl = el.querySelector('#chat-input') as HTMLTextAreaElement;

  // ── Messages rendering ────────────────────────────────────────────────
  // We maintain a parallel array of mounted <div> elements indexed by
  // position in store.cards. A full re-render is cheap for moderate
  // session sizes; if it becomes a bottleneck, keep a WeakMap<Card,
  // HTMLElement> and diff. Not in scope for Phase 8.
  function renderMessages() {
    // Scroll-bottom detection: if the user was near bottom, keep them
    // pinned after the re-render.
    const wasAtBottom =
      messagesEl.scrollTop + messagesEl.clientHeight >= messagesEl.scrollHeight - 40;

    // Rebuild with a DocumentFragment — single reflow, no innerHTML.
    // Each card render is isolated in try/catch: a single malformed event
    // (NaN ts, missing required field, reducer regression) must not abort
    // the loop and silently drop every later card. We surface the failure
    // as a small inline placeholder so the user sees that something went
    // wrong rather than a blank pane.
    const frag = document.createDocumentFragment();
    for (const card of store.cards) {
      try {
        frag.appendChild(renderCard(cardPropsFor(card)));
      } catch (err) {
        console.error('renderCard failed for card', card, err);
        frag.appendChild(renderErrorPlaceholder(card, err));
      }
    }
    messagesEl.replaceChildren(frag);

    if (wasAtBottom) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  function renderErrorPlaceholder(card: unknown, err: unknown): HTMLElement {
    const el = document.createElement('div');
    el.className = 'card card--error';
    const kind = (card && typeof card === 'object' && 'kind' in card)
      ? String((card as { kind: unknown }).kind)
      : 'unknown';
    const message = err instanceof Error ? err.message : String(err);
    // textContent (not innerHTML) — error message is untrusted by virtue
    // of having come out of a render path that just failed.
    const title = document.createElement('div');
    title.className = 'card-header';
    title.textContent = `⚠ render failed (kind=${kind})`;
    const body = document.createElement('div');
    body.className = 'card-body';
    body.textContent = message;
    el.appendChild(title);
    el.appendChild(body);
    return el;
  }

  // Card-adjacent re-render: the reducer emits 'cards' on both append
  // and tool-result upgrade, and emits 'card:<tool_use_id>' as a
  // per-card signal (currently unused by this view — a full re-render
  // is cheap enough at expected session sizes, and keeps zero state in
  // the view layer). Future optimisation: diff against a WeakMap of
  // mounted nodes.
  store.on('cards', renderMessages);

  // ── HUD ───────────────────────────────────────────────────────────────

  function renderHud() {
    const hud = store.hud;
    if (!hud || !store.currentSessionId) {
      hudEl.classList.add('hidden');
      hudEl.innerHTML = '';
      return;
    }
    hudEl.classList.remove('hidden');
    const model = hud.model ?? '—';
    const effort = hud.thinking_effort ? ` · ${hud.thinking_effort}` : '';
    const ctx = hud.context_tokens ?? 0;
    const maxCtx = hud.max_context_tokens ?? 0;
    const pct = maxCtx > 0 ? Math.min(100, Math.round((ctx / maxCtx) * 100)) : 0;
    const tps = hud.toks_per_s != null ? `${hud.toks_per_s.toFixed(1)} tok/s` : '';
    const bash = hud.bash_running ?? 0;
    const sub = hud.sub_agents_running ?? 0;
    const usage = hud.usage ?? null;
    const usageBits = usage
      ? [
          typeof usage.input === 'number' ? `↑${usage.input}` : '',
          typeof usage.output === 'number' ? `↓${usage.output}` : '',
          typeof usage.cache_read === 'number' ? `⛀${usage.cache_read}` : '',
        ].filter(Boolean).join(' ')
      : '';
    hudEl.innerHTML = `
      <div class="hud-row">
        <span class="hud-model">${esc(model)}${esc(effort)}</span>
        <span class="hud-ctx">ctx ${pct}% (${ctx}/${maxCtx})</span>
        ${tps ? `<span class="hud-tps">${esc(tps)}</span>` : ''}
        ${usageBits ? `<span class="hud-usage">${esc(usageBits)}</span>` : ''}
      </div>
      <div class="hud-row hud-row-runners">
        <span>▸ ${bash} bash · ${sub} sub-agents</span>
        ${renderHudTodo(hud)}
      </div>
    `;
  }

  function renderHudTodo(hud: HudSnapshot): string {
    const todo = hud.todo;
    if (!todo || todo.total === 0) return '';
    return `<span class="hud-todo">${esc(todo.progress_line)}</span>`;
  }

  store.on('hud', renderHud);
  store.on('currentSession', renderHud);

  // ── Input ─────────────────────────────────────────────────────────────

  async function sendInput() {
    const id = store.currentSessionId;
    if (!id) return;
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = '';
    try {
      await api.sendMessage(id, text);
    } catch (e) {
      console.error('sendMessage failed:', e);
      inputEl.value = text;
    }
  }

  el.querySelector('#btn-send')!.addEventListener('click', sendInput);
  el.querySelector('#btn-interrupt')!.addEventListener('click', async () => {
    const id = store.currentSessionId;
    if (!id) return;
    try {
      await api.interruptSession(id);
    } catch (e) {
      console.error('interrupt failed:', e);
    }
  });

  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendInput();
    }
  });

  // Initial paint
  renderMessages();
  renderHud();

  return el;
}

// HUD refresh is called from main.ts on session attach and every 10s
// (same cadence as pre-refactor). We expose a small helper so main.ts
// doesn't have to reach into the chat component's private scope.
export async function refreshHud(sessionId: string): Promise<void> {
  try {
    const hud = await api.getHud(sessionId);
    if (store.currentSessionId !== sessionId) return;
    store.hud = hud;
    store.emit('hud');
  } catch {
    // non-fatal
  }
}

function esc(s: string): string {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
