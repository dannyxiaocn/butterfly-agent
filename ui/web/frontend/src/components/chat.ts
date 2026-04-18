import { store } from '../store';
import { api } from '../api';
import type { DisplayEvent } from '../types';
import { renderMarkdown, escapeHtml, formatTs } from '../markdown';

// v2.0.19: model_max tokens come from GET /api/hud's ``max_context_tokens``
// field, which the backend sources from butterfly/llm_engine/models.yaml.
// The hardcoded table this replaced was drift-prone and invisibly wrong for
// gpt-5.4 / kimi-for-coding — both get 250k now.

export function createChat(): HTMLElement {
  const el = document.createElement('main');
  el.id = 'chat';
  el.innerHTML = `
    <div id="messages" class="messages"></div>
    <div id="agent-status" class="agent-status hidden">Agent is working…</div>
    <div id="hud-bar" class="hud-bar hidden" title="model · context · running tool · tokens">
      <span class="hud-dot hud-dot-idle" id="hud-dot"></span>
      <span class="hud-item hud-model"><span class="hud-model-text">…</span></span>
      <span class="hud-sep">·</span>
      <span class="hud-item hud-context"><span class="hud-ctx-text">…</span></span>
      <span class="hud-sep hud-tool-sep hidden">·</span>
      <span class="hud-item hud-tool hidden"><span class="hud-tool-text"></span></span>
      <span class="hud-sep hud-subagent-sep hidden">·</span>
      <span class="hud-item hud-subagent hidden" title="Sub-agents currently running"><span class="hud-subagent-text"></span></span>
      <span class="hud-sep hud-speed-sep hidden">·</span>
      <span class="hud-item hud-speed hidden" title="Output tokens/sec from the latest LLM call (not cumulative)"><span class="hud-speed-text"></span></span>
      <!-- Tokens pill kept in the DOM but hidden by default — PR #36 -->
      <!-- dropped the noisy in/out counter from the HUD. -->
      <span class="hud-sep hud-tokens-sep hidden">·</span>
      <span class="hud-item hud-tokens hidden"><span class="hud-tokens-text">…</span></span>
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
  // Background tools (sub_agent + bash bg) have a two-phase lifecycle: the
  // immediate tool_done is just a placeholder (cell stays yellow), and the
  // matching tool_finalize event arrives later when the actual work ends.
  // We map tid → cell so the finalize handler can locate the right row even
  // when many bg tools are running concurrently.
  const backgroundCells = new Map<string, { el: HTMLElement; name: string; startTs: number }>();

  // Thinking cells: map block_id → running DOM element. On thinking_start we
  // insert a placeholder "Thinking…" pill; on thinking_done we flip it to
  // the collapsed "Thought for Xs" state with the full body inside a
  // <details> element. Mirrors the msg-tool running/done lifecycle.
  const runningThinking = new Map<string, HTMLElement>();

  // Streaming bubble lives INSIDE the messages div so it scrolls with the conversation.
  //   * While the provider is running but no text has streamed yet, the bubble shows
  //     a dim "Agent is working…" placeholder — no cell chrome, no dots badge.
  //   * On first ``partial_text`` delta the placeholder is replaced; subsequent
  //     deltas APPEND to the accumulator (backend emits each chunk as a ~150-char
  //     delta, not a cumulative buffer, so the body grows incrementally).
  //   * On ``tool_call`` mid-turn the bubble is finalized in-place into a plain
  //     msg-agent cell — this is how interleaved mode (think → tool → text →
  //     tool → text) renders each text output as its own permanent cell rather
  //     than collapsing them all into the last one.
  //   * On the turn's ``agent`` event, the bubble is promoted in-place with the
  //     canonical text + header + usage stats (no remove-then-append flash).
  let streamingEl: HTMLDivElement | null = null;
  let streamingText = '';
  let isStreaming = false;
  // True once an ``agent`` event has finalized the bubble for the current
  // run. Late ``partial_text`` chunks that the SSE merge orders AFTER the
  // turn event (iter_events reads context.jsonl before events.jsonl, so
  // within a single poll cycle any chunks written between the last flush
  // and the turn's commit can land here) would otherwise spawn a fresh
  // streaming bubble that ``model_status:idle`` then finalizes as a
  // spurious "AGENT" cell showing just the tail fragment of the message
  // (observed 2026-04-17 on session 2026-04-17_21-36-14-f6c3). Reset on
  // every ``model_status:running``.
  let streamingFinalized = false;

  function setAgentStatus(running: boolean) {
    const bar = el.querySelector('#agent-status') as HTMLElement | null;
    if (!bar) return;
    bar.classList.toggle('hidden', !running);
  }

  function getOrCreateStreamingBubble(): HTMLDivElement {
    if (!streamingEl) {
      streamingEl = document.createElement('div');
      streamingEl.className = 'msg msg-agent msg-streaming';
      streamingEl.innerHTML = `
        <div class="msg-body msg-streaming-body markdown-body"></div>
      `;
      messages.appendChild(streamingEl);
      // Output has begun — the standalone status line is redundant.
      setAgentStatus(false);
    }
    return streamingEl;
  }

  function removeStreamingBubble() {
    if (streamingEl) {
      streamingEl.remove();
      streamingEl = null;
    }
    streamingText = '';
    isStreaming = false;
  }

  function finalizeStreamingBubble(canonicalText?: string, usage?: any, finalTs?: string): void {
    // Promote the current streaming bubble into a permanent msg-agent cell,
    // or drop it if nothing was streamed. Called:
    //   * on the turn's agent event (canonicalText = event.content) — final
    //     in-place promotion with header + usage.
    //   * on tool_call mid-turn (canonicalText omitted) — freeze the
    //     accumulator as an intermediate output cell, preserving the
    //     iteration-ordered display of interleaved text+tool patterns.
    //   * on model_status:idle (canonicalText omitted) — cancel path, keep
    //     whatever text was streamed visible so the user sees where it stopped.
    if (!streamingEl) return;
    const text = canonicalText ?? streamingText;
    if (!text) {
      streamingEl.remove();
      streamingEl = null;
      streamingText = '';
      return;
    }
    streamingEl.classList.remove('msg-streaming');
    let usageHtml = '';
    if (usage) {
      const u = usage as Record<string, number>;
      const parts: string[] = [];
      if (u.input != null) parts.push(`in:${u.input}`);
      if (u.output != null) parts.push(`out:${u.output}`);
      if (u.cache_read != null) parts.push(`cached:${u.cache_read}`);
      if (u.cache_write != null) parts.push(`wrote:${u.cache_write}`);
      if (parts.length) usageHtml = `<span class="usage-stats">${escapeHtml(parts.join(' · '))}</span>`;
    }
    streamingEl.innerHTML = `
      <div class="msg-header">
        <span class="msg-label">agent</span>
        ${usageHtml}
        <span class="msg-ts">${formatTs(finalTs)}</span>
      </div>
      <div class="msg-body markdown-body">${renderMarkdown(text)}</div>
    `;
    streamingEl = null;
    streamingText = '';
  }

  function markRunningThinkingInterrupted() {
    // Flip any still-running thinking cells (no matching thinking_done)
    // to a terminal "interrupted" state. Called from the model_status:idle
    // branch — by the time the daemon flips to idle, the provider will not
    // emit any more thinking_done for this run (cancel path takes priority
    // over the thinking block's natural end), so the cell would spin on
    // "Thinking…" forever. The CSS rule .msg-thinking-interrupted dims it.
    for (const [blockId, cell] of runningThinking) {
      const summary = cell.querySelector('.tool-status-summary') as HTMLElement | null;
      if (summary) {
        summary.innerHTML = '<span class="tool-status-icon">⚠</span><span class="tool-status-name">Thinking interrupted</span><span class="tool-status-meta">cancelled</span>';
      }
      cell.classList.remove('msg-thinking-running');
      cell.classList.add('msg-thinking-done', 'msg-thinking-interrupted');
      cell.dataset.interrupted = '1';
      runningThinking.delete(blockId);
    }
  }

  function clearMessages() {
    setAgentStatus(false);
    removeStreamingBubble();
    messages.innerHTML = '';
    isStreaming = false;
    streamingText = '';
    streamingFinalized = false;
    runningTools.clear();
    latestToolKey = null;
    runningThinking.clear();
    backgroundCells.clear();
    updateHudTool(null);
    updateHudSubAgents(0);
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
          // Defensive: any orphan bubble from a previous run that somehow
          // didn't get a terminal agent/idle event (rare — cancelled
          // without partial-turn commit) must be finalized before we
          // start a fresh one; otherwise a late partial_text returns the
          // stale instance and streamingText keeps growing across runs.
          if (streamingEl && !streamingFinalized) {
            finalizeStreamingBubble();
          }
          isStreaming = true;
          streamingText = '';
          streamingFinalized = false;
          // Running state now uses a single status line — no empty chat
          // cell. The streaming bubble is created lazily when the first
          // partial_text delta arrives.
          setAgentStatus(true);
          store.modelState = { state: 'running', source: event.source ?? null };
          updateHudDot('running');
        } else {
          // Idle: flip any still-spinning thinking cell to interrupted.
          markRunningThinkingInterrupted();
          setAgentStatus(false);
          // Bubble handling at idle: finalize any in-flight streaming
          // bubble with whatever text accumulated so the user sees where
          // output stopped (or drop it if empty).
          if (!streamingFinalized) {
            finalizeStreamingBubble();
          }
          isStreaming = false;
          store.modelState = { state: 'idle', source: null };
          updateHudDot('idle');
        }
        store.emit('modelState');
        break;

      case 'partial_text':
        // Backend emits each chunk as a ~150-char DELTA (not a cumulative
        // buffer), so we append to a local accumulator and re-render the
        // body each time. The cell grows smoothly rather than flashing
        // truncated fragments.
        //
        // Gate: after ``agent`` has finalized the current run, any late
        // chunks that the SSE merge re-ordered after the turn event must
        // be dropped — otherwise they'd create a fresh streaming bubble
        // whose accumulator is just the tail fragment, which the next
        // idle event would crystallise into a duplicate "AGENT" cell.
        if (streamingFinalized) break;
        if (!isStreaming) isStreaming = true;
        {
          const bubble = getOrCreateStreamingBubble();
          const body = bubble.querySelector('.msg-streaming-body') as HTMLElement;
          if (event.content) {
            streamingText += event.content;
            body.innerHTML = renderMarkdown(streamingText);
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
              <span class="tool-status-name">Thinking…</span>
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
        // Initial label has no reasoning_tokens yet — provider reports them
        // at LLM call end, which arrives as a later ``thinking_tokens_update``
        // event. Anthropic never sends the follow-up, so its cells stay on
        // the duration-only label (spec: null → "Thought Xs").
        const label = `Thought ${escapeHtml(durSec)}`;
        const html = `
          <details class="thinking-details">
            <summary class="thinking-summary">
              <span class="thinking-label" data-duration-label="${escapeHtml(durSec)}">${label}</span>
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

      case 'thinking_tokens_update': {
        // Provider-reported reasoning_tokens for one LLM call arrived; stamp
        // the matching thinking block's label. Look up by data-block-id
        // rather than the live runningThinking map (the cell has already
        // transitioned to done state by the time this event fires).
        const blockId = event.block_id ?? '';
        const tokens = event.reasoning_tokens ?? 0;
        if (!blockId || tokens <= 0) break;
        const cellEl = messages.querySelector<HTMLElement>(
          `.msg-thinking[data-block-id="${CSS.escape(blockId)}"]`
        );
        if (!cellEl) break;
        const labelEl = cellEl.querySelector<HTMLElement>('.thinking-label');
        if (!labelEl) break;
        const dur = labelEl.dataset.durationLabel ?? '';
        labelEl.textContent = dur
          ? `Thought ${dur} for ${tokens} tokens`
          : `Thought for ${tokens} tokens`;
        break;
      }

      case 'agent':
        // Promote the current streaming bubble in-place rather than remove-
        // then-append (avoids the old "everything flashes into a big cell at
        // the end" UX). For history replay (no streaming bubble exists) fall
        // back to a plain append so the cell still renders. The
        // ``streamingFinalized`` flag is set so ``partial_text`` chunks that
        // the SSE merge delivered AFTER this event get dropped instead of
        // spawning a duplicate streaming bubble.
        if (streamingEl) {
          finalizeStreamingBubble(event.content, event.usage, event.ts);
        } else {
          appendEvent(event);
        }
        streamingFinalized = true;
        // The 'agent' event carries the turn's token usage — update the HUD
        // pill inline so tokens don't freeze between explicit refreshHud()
        // calls (flagged in PR #24 review item 8).
        if (event.usage) updateHudTokens(event.usage);
        break;

      case 'tool': {
        // Interleaved-mode boundary: a tool_call mid-turn means the current
        // iteration's text output (if any) is complete. Finalize the
        // streaming bubble into a permanent cell so this intermediate text
        // becomes part of the transcript instead of being clobbered by the
        // next iteration's stream. If the bubble is still showing just the
        // placeholder, finalizeStreamingBubble() drops it.
        finalizeStreamingBubble();
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
          if (summary) setToolStatus(summary, '▶', 'running…');
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
        // Background-spawn placeholder: the cell must stay yellow until the
        // matching tool_finalize event arrives. Tag it with the tid + parked
        // start timestamp so finalize can locate and resolve it.
        if (event.is_background && event.tid && target) {
          target.dataset.bgTid = event.tid;
          backgroundCells.set(event.tid, { el: target, name, startTs: started });
          const summary = target.querySelector('.tool-status-summary') as HTMLElement | null;
          if (summary) setToolStatus(summary, '▶', 'running…');
          // Don't drop runningTools entry — the HUD ▶ name pill should stay.
          break;
        }
        if (target) {
          target.classList.add('done');
          const durSec = (durationMs / 1000).toFixed(1) + 's';
          const summary = target.querySelector('.tool-status-summary') as HTMLElement | null;
          if (summary) {
            const meta = typeof event.result_len === 'number'
              ? `${durSec} · ${event.result_len} chars`
              : durSec;
            setToolStatus(summary, '✓', meta);
          }
          const resultEl = target.querySelector('.tool-body-result .tool-body-content') as HTMLElement | null;
          if (resultEl && typeof event.result === 'string') {
            resultEl.innerHTML = renderToolResultBlock(event.result, event.result_truncated);
          }
        }
        runningTools.delete(name);
        if (latestToolKey === name) latestToolKey = null;
        updateHudTool(latestToolKey);
        // Intentionally NOT appending a separate msg-status line — the running
        // pill transitions to done in place. Memory note: keeps the log quiet.
        break;
      }

      case 'tool_progress': {
        // Refresh the in-place "▶ name · running…" pill with the latest
        // one-line summary from the background runner.
        if (!event.tid) break;
        const tracked = backgroundCells.get(event.tid);
        if (!tracked) break;
        const summaryEl = tracked.el.querySelector('.tool-status-summary') as HTMLElement | null;
        if (summaryEl) setToolStatus(summaryEl, '▶', event.summary || 'running…');
        break;
      }

      case 'tool_finalize': {
        if (!event.tid) break;
        const tracked = backgroundCells.get(event.tid);
        if (!tracked) break;
        const durationMs = event.duration_ms ?? (Date.now() - tracked.startTs);
        const durSec = (durationMs / 1000).toFixed(1) + 's';
        const kind = event.kind || 'completed';
        // Map kind to a finishing icon — completed is the success path,
        // killed / killed_by_restart / stalled all surface as warnings.
        const icon = kind === 'completed' ? '✓' : '⚠';
        tracked.el.classList.add('done');
        if (kind !== 'completed') tracked.el.classList.add('warned');
        const summaryEl = tracked.el.querySelector('.tool-status-summary') as HTMLElement | null;
        if (summaryEl) setToolStatus(summaryEl, icon, `${kind} · ${durSec}`);
        const resultEl = tracked.el.querySelector('.tool-body-result .tool-body-content') as HTMLElement | null;
        if (resultEl && typeof event.result === 'string') {
          resultEl.innerHTML = renderToolResultBlock(event.result, event.result_truncated);
        }
        backgroundCells.delete(event.tid);
        // HUD "▶ name" cleanup: this name might still have other concurrent
        // calls; only clear if it was the latest tracked.
        if (latestToolKey === tracked.name && !runningTools.has(tracked.name)) {
          latestToolKey = null;
          updateHudTool(null);
        }
        break;
      }

      case 'sub_agent_count': {
        updateHudSubAgents(event.running ?? 0);
        break;
      }

      case 'loop_start':
      case 'loop_end':
        // Quiet: loop lifecycle no longer clutters the transcript (user feedback).
        // HUD's running dot already conveys the information.
        break;

      case 'llm_call_usage': {
        // v2.0.19: per-LLM-call token accounting — drives the HUD's
        // context-% and realtime toks/s. Not rendered in the transcript.
        updateHudContext(event.context_tokens ?? null);
        updateHudSpeed(event.toks_per_s ?? null);
        if (event.usage) updateHudTokens(event.usage);
        break;
      }

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

  function updateHudSubAgents(count: number) {
    const sep = el.querySelector('.hud-subagent-sep') as HTMLElement | null;
    const item = el.querySelector('.hud-subagent') as HTMLElement | null;
    const text = el.querySelector('.hud-subagent-text') as HTMLElement | null;
    if (!sep || !item || !text) return;
    if (count > 0) {
      sep.classList.remove('hidden');
      item.classList.remove('hidden');
      const noun = count === 1 ? 'sub-agent' : 'sub-agents';
      text.textContent = `⚙ ${count} ${noun} running`;
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

  // v2.0.19: model context window sourced from /api/hud. Cached so live
  // llm_call_usage events (which don't re-fetch /api/hud) can recompute the
  // ctx-% bar without a round trip. Reset by refreshHud on session switch.
  let hudMaxContextTokens = 200000;

  function updateHudContext(contextTokens: number | null | undefined) {
    const hudBar = el.querySelector('#hud-bar') as HTMLElement | null;
    if (!hudBar) return;
    const ctxEl = hudBar.querySelector('.hud-ctx-text') as HTMLElement | null;
    if (!ctxEl) return;
    if (contextTokens == null) {
      ctxEl.textContent = 'ctx —';
      ctxEl.title = 'no LLM call yet';
      return;
    }
    const pct = Math.min(100, Math.round((contextTokens / hudMaxContextTokens) * 100));
    ctxEl.textContent = `ctx ${pct}%`;
    ctxEl.title = `${contextTokens.toLocaleString()} / ${hudMaxContextTokens.toLocaleString()} tokens`;
  }

  function updateHudSpeed(toksPerS: number | null | undefined) {
    const hudBar = el.querySelector('#hud-bar') as HTMLElement | null;
    if (!hudBar) return;
    const sep = hudBar.querySelector('.hud-speed-sep') as HTMLElement | null;
    const wrap = hudBar.querySelector('.hud-speed') as HTMLElement | null;
    const txt = hudBar.querySelector('.hud-speed-text') as HTMLElement | null;
    if (!sep || !wrap || !txt) return;
    if (toksPerS == null || toksPerS <= 0) {
      sep.classList.add('hidden');
      wrap.classList.add('hidden');
      return;
    }
    const display = toksPerS >= 100 ? toksPerS.toFixed(0) : toksPerS.toFixed(1);
    txt.textContent = `⚡ ${display} tok/s`;
    sep.classList.remove('hidden');
    wrap.classList.remove('hidden');
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

      // Context: real token count from the latest LLM call (v2.0.19).
      // The byte/4 heuristic is gone — if no call has finished yet,
      // context_tokens is null and we show "ctx —" rather than a bogus 0%.
      hudMaxContextTokens = data.max_context_tokens || 200000;
      updateHudContext(data.context_tokens);

      // Realtime toks/s from the latest LLM call. Hidden when null.
      updateHudSpeed(data.toks_per_s);

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
      // Restore the sub-agent badge on attach / page refresh — derived
      // from the on-disk panel by the HUD endpoint, since the SSE stream
      // only re-broadcasts sub_agent_count when a child changes state.
      const subAgentsRunning = (data as { sub_agents_running?: number }).sub_agents_running ?? 0;
      updateHudSubAgents(subAgentsRunning);
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
      // v2.0.19 (parallel): render the cell even when the provider returned
      // an empty summary — the persisted block still carries duration_ms
      // (and reasoning_tokens from the attributor), which is what the
      // "Thought Xs for N tokens" pill shows. Returning null here used to
      // drop the cell AND shift reload-order for every following block,
      // because ipc.py pulls one persisted entry per reasoning marker.
      div.className = 'msg msg-thinking';
      if (event.block_id) div.dataset.blockId = event.block_id;
      const durSec = event.duration_ms != null
        ? (event.duration_ms / 1000).toFixed(1) + 's'
        : '';
      const tokens = event.reasoning_tokens ?? 0;
      let label: string;
      if (durSec && tokens > 0) {
        label = `Thought ${durSec} for ${tokens} tokens`;
      } else if (durSec) {
        label = `Thought ${durSec}`;
      } else {
        label = 'Thought';
      }
      const body = event.content ?? '';
      const bodyHtml = body
        ? `<div class="thinking-body markdown-body">${renderMarkdown(body)}</div>`
        : `<div class="thinking-body thinking-empty"><em>No thinking body exposed by the provider.</em></div>`;
      div.innerHTML = `
        <details class="thinking-details">
          <summary class="thinking-summary">
            <span class="thinking-label" data-duration-label="${escapeHtml(durSec)}">${escapeHtml(label)}</span>
            <span class="thinking-toggle-hint"></span>
          </summary>
          ${bodyHtml}
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
      div.className = 'msg msg-tool done';
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
  // Inline-expand layout (v2.0.19): matches the thinking cell.
  //   <details>
  //     <summary>  icon  name  arg-preview  meta  ts  [caret ▸]  </summary>
  //     <div class="tool-body">
  //       input block (always populated)
  //       result block (populated by tool_done; empty until then)
  //     </div>
  //   </details>
  // Live transitions (tool / tool_done / tool_finalize) only rewrite the
  // summary icon + meta and the result body — the name, arg, and input
  // spans survive.
  const name = event.name ?? 'unknown';
  const input = event.input ?? {};
  const preview = toolArgPreview(name, input);
  const expanded = toolArgExpanded(name, input);

  const argHtml = preview
    ? `<span class="tool-status-arg" title="${escapeHtml(preview)}">${escapeHtml(preview)}</span>`
    : '<span class="tool-status-arg"></span>';

  const hasResult = typeof event.result === 'string';
  const resultBlock = hasResult
    ? renderToolResultBlock(event.result as string, event.result_truncated)
    : '<em class="tool-body-empty">(pending)</em>';

  return `
    <details class="tool-details">
      <summary class="tool-status-summary">
        <span class="tool-status-icon">✓</span>
        <span class="tool-status-name">${escapeHtml(name)}</span>
        ${argHtml}
        <span class="tool-status-meta"></span>
        <span class="msg-ts">${formatTs(event.ts)}</span>
      </summary>
      <div class="tool-body">
        <div class="tool-body-section tool-body-input">
          <div class="tool-body-label">input</div>
          <div class="tool-body-content">${expanded || `<pre>${escapeHtml(JSON.stringify(input, null, 2))}</pre>`}</div>
        </div>
        <div class="tool-body-section tool-body-result">
          <div class="tool-body-label">result</div>
          <div class="tool-body-content">${resultBlock}</div>
        </div>
      </div>
    </details>
  `;
}

function renderToolResultBlock(result: string, truncated?: boolean): string {
  if (!result) return '<em class="tool-body-empty">(empty)</em>';
  const suffix = truncated ? '\n\n…[truncated]' : '';
  return `<pre>${escapeHtml(result + suffix)}</pre>`;
}

function setToolStatus(summaryEl: HTMLElement, icon: string, meta: string): void {
  // Update only the icon and meta spans on a tool-status-summary row;
  // the name and (v2.0.18) inline arg spans are preserved. Using
  // innerHTML = ... on the whole summary would clobber the arg and
  // force every call site to re-pass the tool input, which is not
  // available to tool_done / tool_progress / tool_finalize handlers.
  const iconEl = summaryEl.querySelector<HTMLElement>('.tool-status-icon');
  const metaEl = summaryEl.querySelector<HTMLElement>('.tool-status-meta');
  if (iconEl) iconEl.textContent = icon;
  if (metaEl) metaEl.textContent = meta;
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
