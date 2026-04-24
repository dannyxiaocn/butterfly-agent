// Unified Card component (DESIGN.md §8.2). One shape, one function.
// Every event maps via eventToCardProps(); rendering is pure — the live
// path and the history replay path produce identical DOM.
import { EVT } from './types';
import type { BfEvent } from './types';
import { escapeHtml, renderMarkdown } from './markdown';
import type { Card } from './store';

export type CardKind =
  | 'user'
  | 'agent-text'
  | 'agent-thinking'
  | 'agent-tool'
  | 'task'
  | 'system'
  | 'error';

export interface CardProps {
  kind: CardKind;
  accent: string;
  title: string;
  body: string | HTMLElement;
  footer?: string;
  collapsed?: boolean; // initial <details> state; user can toggle
}

// ── Public renderer ─────────────────────────────────────────────────────────

export function renderCard(props: CardProps): HTMLElement {
  const wrap = document.createElement('div');
  wrap.className = `card card--${props.kind}`;
  wrap.style.setProperty('--card-accent', props.accent);

  const bodyIsHtml = typeof props.body !== 'string';

  if (props.collapsed !== undefined) {
    // Collapsible: use native <details>. No expanded-state Set.
    const details = document.createElement('details');
    details.className = 'card-details';
    if (!props.collapsed) details.setAttribute('open', '');
    const summary = document.createElement('summary');
    summary.className = 'card-summary';
    summary.innerHTML = `
      <span class="card-caret"></span>
      <span class="card-title">${escapeHtml(props.title)}</span>
      ${props.footer ? `<span class="card-footer">${escapeHtml(props.footer)}</span>` : ''}
    `;
    details.appendChild(summary);
    const body = document.createElement('div');
    body.className = 'card-body';
    if (bodyIsHtml) body.appendChild(props.body as HTMLElement);
    else body.innerHTML = props.body as string;
    details.appendChild(body);
    wrap.appendChild(details);
  } else {
    // Always expanded: simple header + body, no toggle.
    const header = document.createElement('div');
    header.className = 'card-header';
    header.innerHTML = `
      <span class="card-title">${escapeHtml(props.title)}</span>
      ${props.footer ? `<span class="card-footer">${escapeHtml(props.footer)}</span>` : ''}
    `;
    wrap.appendChild(header);
    const body = document.createElement('div');
    body.className = 'card-body';
    if (bodyIsHtml) body.appendChild(props.body as HTMLElement);
    else body.innerHTML = props.body as string;
    wrap.appendChild(body);
  }
  return wrap;
}

// ── Event -> CardProps ──────────────────────────────────────────────────────

/** Pure mapping from a Card (event + optional paired result) to props.
 *  One switch covers every event that appends a card; renderCard() paints it. */
export function cardPropsFor(card: Card): CardProps {
  const ev = card.event;
  const ts = formatTs(ev.ts);
  switch (ev.type) {
    case EVT.USER_INPUT:
      return userInputProps(ev, ts);
    case EVT.USER_INTERRUPT:
      return {
        kind: 'user',
        accent: 'var(--red)',
        title: 'Interrupt',
        body: renderMarkdown(String(ev.payload?.text ?? '')),
        footer: ts,
      };
    case EVT.AGENT_TEXT:
      return {
        kind: 'agent-text',
        accent: 'var(--muted)',
        title: String(ev.payload?.model ?? 'assistant'),
        body: renderMarkdown(String(ev.payload?.text ?? '')),
        footer: ts,
      };
    case EVT.AGENT_THINKING: {
      const text = String(ev.payload?.text ?? '');
      const interrupted = ev.payload?.interrupted === true;
      const dur = ev.payload?.duration_ms;
      const rtok = ev.payload?.reasoning_tokens;
      const title = interrupted ? 'Thinking interrupted' : 'Thought';
      const footerBits: string[] = [];
      if (typeof dur === 'number') footerBits.push(`${(dur / 1000).toFixed(1)}s`);
      if (typeof rtok === 'number' && rtok > 0) footerBits.push(`${rtok} tokens`);
      footerBits.push(ts);
      return {
        kind: 'agent-thinking',
        accent: '#bc8cff',
        title,
        body: renderMarkdown(text || '_(no content)_'),
        footer: footerBits.join(' · '),
        collapsed: true,
      };
    }
    case EVT.AGENT_TOOL_CALL:
      return toolCardProps(card, ts);
    case EVT.AGENT_TOOL_RESULT: {
      // Orphan result (no matching call). Render as a system card.
      const r = String(ev.payload?.result ?? '');
      return {
        kind: 'system',
        accent: 'var(--muted)',
        title: `tool_result (orphan — no matching call)`,
        body: `<pre class="card-pre">${escapeHtml(r)}</pre>`,
        footer: ts,
        collapsed: true,
      };
    }
    case EVT.SYSTEM_NOTICE: {
      const level = String(ev.payload?.level ?? 'info');
      const text = String(ev.payload?.text ?? '');
      return {
        kind: level === 'error' ? 'error' : 'system',
        accent: level === 'error' ? 'var(--red)' : 'var(--muted)',
        title: 'System',
        body: escapeHtml(text),
        footer: ts,
      };
    }
    case EVT.ERROR:
      return {
        kind: 'error',
        accent: 'var(--red)',
        title: 'Error',
        body: escapeHtml(String(ev.payload?.text ?? '')),
        footer: ts,
      };
    default:
      return {
        kind: 'system',
        accent: 'var(--muted)',
        title: ev.type,
        body: `<pre class="card-pre">${escapeHtml(JSON.stringify(ev.payload, null, 2))}</pre>`,
        footer: ts,
        collapsed: true,
      };
  }
}

function userInputProps(ev: BfEvent, ts: string): CardProps {
  const source = String(ev.payload?.source ?? 'web');
  const caller = ev.payload?.caller;
  const displayName = ev.payload?.display_name;
  const text = String(ev.payload?.text ?? '');
  let title = 'You';
  let accent = 'var(--green)';
  if (source === 'task') {
    title = `Task wakeup${caller ? ` · ${caller}` : ''}`;
    accent = 'var(--accent)';
  } else if (source === 'sub_agent') {
    title = `Sub-agent${displayName ? ` · ${displayName}` : caller ? ` · ${caller}` : ''}`;
    accent = 'var(--yellow)';
  } else if (source === 'cli') {
    title = 'You (cli)';
  }
  return {
    kind: 'user',
    accent,
    title,
    body: renderMarkdown(text),
    footer: ts,
  };
}

function toolCardProps(card: Card, ts: string): CardProps {
  const callEv = card.event;
  const resultEv = card.resultEvent;
  const toolName = String(callEv.payload?.tool_name ?? 'tool');
  const args = callEv.payload?.args ?? {};
  const argsJson = safeStringify(args);
  const isRunning = !resultEv;
  const isError = resultEv?.payload?.is_error === true;

  const header = isRunning ? `${toolName} · running…` : isError ? `${toolName} · error` : toolName;

  const footerBits: string[] = [];
  if (resultEv) {
    const dur = resultEv.payload?.duration_ms;
    if (typeof dur === 'number') footerBits.push(`${(dur / 1000).toFixed(2)}s`);
  }
  footerBits.push(ts);

  // Body: args block + result block (when present).
  const bodyParts: string[] = [];
  bodyParts.push(`<div class="tool-section">`);
  bodyParts.push(`<div class="tool-section-label">args</div>`);
  bodyParts.push(`<pre class="card-pre">${escapeHtml(argsJson)}</pre>`);
  bodyParts.push(`</div>`);
  if (resultEv) {
    const r = String(resultEv.payload?.result ?? '');
    bodyParts.push(`<div class="tool-section">`);
    bodyParts.push(`<div class="tool-section-label">${isError ? 'error' : 'result'}</div>`);
    bodyParts.push(`<pre class="card-pre">${escapeHtml(r)}</pre>`);
    bodyParts.push(`</div>`);
  } else {
    bodyParts.push(`<div class="tool-running">running…</div>`);
  }

  return {
    kind: 'agent-tool',
    accent: isError ? 'var(--red)' : isRunning ? 'var(--yellow)' : 'var(--accent)',
    title: header,
    body: bodyParts.join(''),
    footer: footerBits.join(' · '),
    collapsed: true,
  };
}

// ── helpers ────────────────────────────────────────────────────────────────

function formatTs(ts: number): string {
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  } catch {
    return '';
  }
}

function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}
