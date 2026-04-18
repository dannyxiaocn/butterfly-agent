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
      // v2.0.20: the live cell mirrors the thinking-running chrome — a
      // compact "Outputting…" pill with a caret, body empty until the
      // 'agent' event replaces it with the finalized output + duration.
      // Streaming text is accumulated silently in ``streamingText`` so
      // the intermediate-finalize path (mid-turn tool_call) still has the
      // interim content when no canonical 'agent' event has arrived yet.
      streamingEl.className = 'msg msg-agent msg-agent-streaming';
      streamingEl.dataset.startedAt = String(Date.now());
      // Mirrors the tool cell layout: a constant name ("Agent") followed by
      // a live status pill. While streaming the status reads "typing…";
      // finalizeStreamingBubble flips it to the measured duration once the
      // 'agent' event arrives — same slot, same dim styling as the tool
      // cell's "running… → 2.4s" transition.
      streamingEl.innerHTML = `
        <details class="agent-details">
          <summary class="agent-summary">
            <span class="agent-label">
              <span class="agent-word">Agent</span>
              <span class="agent-meta">typing…</span>
            </span>
            <span class="msg-ts">${formatTs(new Date().toISOString())}</span>
          </summary>
          <div class="agent-body agent-empty"><em>(still typing…)</em></div>
        </details>
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

  function finalizeStreamingBubble(canonicalText?: string, usage?: any, finalTs?: string, serverDurMs?: number): void {
    // Promote the current streaming bubble into a permanent msg-agent cell,
    // or drop it if nothing was streamed. Called:
    //   * on the turn's agent event (canonicalText = event.content) — final
    //     in-place promotion with duration + usage.
    //   * on tool_call mid-turn (canonicalText omitted) — freeze the
    //     accumulator as an intermediate output cell, preserving the
    //     iteration-ordered display of interleaved text+tool patterns.
    //   * on model_status:idle (canonicalText omitted) — cancel path, keep
    //     whatever text was streamed visible so the user sees where it stopped.
    //
    // v2.0.20: the finalized cell uses the same <details> chrome as the
    // thinking-done state — a "Output Xs" summary pill over the rendered
    // markdown body. Duration is measured client-side from dataset.startedAt,
    // stamped by getOrCreateStreamingBubble on the first partial_text. This
    // keeps the live cell's pill identical to what history replay renders
    // from events.jsonl (``agent_output_done.duration_ms``).
    if (!streamingEl) return;
    const text = canonicalText ?? streamingText;
    if (!text) {
      streamingEl.remove();
      streamingEl = null;
      streamingText = '';
      return;
    }
    // Prefer the server-measured duration (carried on the 'agent' event or
    // stamped onto the cell by the earlier agent_output_done SSE event) so
    // live and history replay agree. Falls back to the client's first-partial
    // → finalize delta when no server number is available (cancel path,
    // pre-v2.0.20 sessions).
    const startedAt = Number(streamingEl.dataset.startedAt ?? Date.now());
    const datasetDur = Number(streamingEl.dataset.serverDurationMs);
    const durMs = typeof serverDurMs === 'number' && serverDurMs > 0
      ? serverDurMs
      : (Number.isFinite(datasetDur) && datasetDur > 0
          ? datasetDur
          : Date.now() - startedAt);
    const durSec = (durMs / 1000).toFixed(1) + 's';
    streamingEl.classList.remove('msg-agent-streaming');
    streamingEl.classList.add('msg-agent-done');
    // Use a server-supplied timestamp when the caller provided one; fall
    // back to "now" (in ISO form so formatTs parses it) for the cancel /
    // mid-turn-tool paths that finalize without a canonical event — the
    // old code produced the placeholder "—" string, which looked like a
    // bug when the cell stayed mounted across a reload.
    const displayTs = finalTs ?? new Date().toISOString();
    // v2.0.23 round-7: tokens live in the dim footer inside the agent-body.
    // Primary path — the canonical 'agent' event passes ``usage`` explicitly
    // (per-call from turn.agent_output_usages). Fallback — mid-turn tool_call
    // / cancel paths omit it; read the iteration_usage stash that the SSE
    // handler parked on ``dataset.pendingUsage`` so the footer still appears.
    let usageForFooter = usage;
    if (!usageForFooter && streamingEl.dataset.pendingUsage) {
      try {
        usageForFooter = JSON.parse(streamingEl.dataset.pendingUsage);
      } catch {
        // Malformed stash — fall through and render without a footer.
      }
    }
    const footer = renderUsageFooter(usageForFooter);
    streamingEl.innerHTML = `
      <details class="agent-details" open>
        <summary class="agent-summary">
          <span class="agent-label">
            <span class="agent-word">Agent</span>
            <span class="agent-meta">${escapeHtml(durSec)}</span>
          </span>
          <span class="msg-ts">${formatTs(displayTs)}</span>
        </summary>
        <div class="agent-body markdown-body">${renderMarkdown(text)}</div>
        ${footer}
      </details>
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
      // The running cell uses the same <details> chrome as the done state,
      // so the interrupt path just needs to swap the label text. No icon,
      // no "cancelled" meta — the dimmed border + .msg-thinking-interrupted
      // opacity are enough to signal that the thought was cut short.
      const word = cell.querySelector('.thinking-word') as HTMLElement | null;
      const meta = cell.querySelector('.thinking-meta') as HTMLElement | null;
      if (word) word.textContent = 'Thinking interrupted';
      if (meta) meta.textContent = '';
      cell.classList.remove('msg-thinking-running');
      cell.classList.add('msg-thinking-done', 'msg-thinking-interrupted');
      cell.dataset.interrupted = '1';
      runningThinking.delete(blockId);
    }
  }

  function markRunningToolsInterrupted() {
    // v2.0.23: when the run is cancelled mid-tool (user hit ⚡ Interrupt, or
    // a new interrupt-mode chat came in), no ``tool_done`` is ever emitted
    // for the in-flight call. Without this, the yellow running cell spins
    // forever and the user thinks nothing happened. Mirror the thinking
    // interrupt: scan ``.msg-tool`` cells that haven't flipped to ``.done``
    // yet and mark them interrupted. Safe to call on every idle — when
    // the run ended cleanly every tool already transitioned, so the DOM
    // scan is a no-op.
    //
    // v2.0.24: SKIP cells tagged as background (``data-bg-tid``). A bg
    // task (bash run_in_background=true, or sub_agent bg) outlives the
    // parent's turn by design — the parent goes idle but the child keeps
    // working, and ``tool_finalize`` will arrive on its own schedule to
    // flip the cell done. Clearing ``backgroundCells`` or marking the
    // cell interrupted here orphans it: the later finalize event finds
    // nothing to upgrade and the cell stays frozen until a history
    // reload pairs the tool_use with its tool_result.
    const liveCells = Array.from(messages.querySelectorAll('.msg-tool:not(.done)')) as HTMLElement[];
    for (const cell of liveCells) {
      if (cell.dataset.bgTid) continue;
      const summary = cell.querySelector('.tool-status-summary') as HTMLElement | null;
      const name = cell.dataset.toolName ?? '';
      const entry = runningTools.get(name);
      const startTs = entry?.startTs ?? Number(cell.dataset.startedAt ?? Date.now());
      const durSec = ((Date.now() - startTs) / 1000).toFixed(1) + 's';
      cell.classList.add('done', 'interrupted');
      if (summary) setToolStatus(summary, '✗', `interrupted ${durSec}`);
    }
    // runningTools: drop entries whose cell was swept; preserve entries
    // whose cell carries bg-tid (still legitimately running). Rebuild by
    // scanning DOM rather than tracking the delta inline.
    runningTools.clear();
    // Preserve backgroundCells — those entries still correspond to cells
    // waiting for tool_finalize. latestToolKey and the HUD pill point at
    // the most recent ACTIVE tool, which is gone now that the turn idled,
    // so those still get cleared.
    latestToolKey = null;
    updateHudTool(null);
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
          // v2.0.23: same sweep for in-flight tool cells. Without this,
          // the yellow "▶ bash running…" pill never flips and the user
          // thinks ⚡ Interrupt didn't work (backend actually cancelled).
          markRunningToolsInterrupted();
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

      case 'agent_output_start':
        // v2.0.20: server-emitted at the first text chunk of an LLM call so
        // the "Agent / typing…" cell appears without waiting for the 150-char
        // partial_text flush. Defensive gate mirrors the partial_text case.
        if (streamingFinalized) break;
        isStreaming = true;
        getOrCreateStreamingBubble();
        scrollToBottom();
        break;

      case 'agent_output_done':
        // v2.0.20: server-measured wall-clock for this LLM call's text
        // output. Stamp it on the live cell so finalizeStreamingBubble can
        // prefer the server number over the client's first-partial → finalize
        // delta. Keeps live-cell duration identical to the value history
        // replay pulls from events.jsonl. The stamp survives an intermediate
        // tool_call finalize because dataset stays on the node.
        if (streamingEl && typeof event.duration_ms === 'number') {
          streamingEl.dataset.serverDurationMs = String(event.duration_ms);
        }
        break;

      case 'partial_text':
        // v2.0.20: the live cell no longer renders streaming text — the
        // first chunk creates the "Agent / typing…" placeholder (or
        // agent_output_start has already opened it) and subsequent chunks
        // just accumulate into ``streamingText`` so the intermediate
        // finalize path (mid-turn tool_call) still has the interim content
        // if no canonical 'agent' event has arrived for this segment yet.
        //
        // Gate: after ``agent`` has finalized the current run, any late
        // chunks that the SSE merge re-ordered after the turn event must
        // be dropped — otherwise they'd create a fresh placeholder whose
        // accumulator is just the tail fragment, which the next idle event
        // would crystallise into a duplicate "Output" cell.
        if (streamingFinalized) break;
        if (!isStreaming) isStreaming = true;
        getOrCreateStreamingBubble();
        if (event.content) streamingText += event.content;
        scrollToBottom();
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
        // Render the running cell using the same <details> chrome as the
        // done state so the ▸ caret is present from the first paint.
        // Body stays empty until thinking_done replaces the innerHTML with
        // the finalized "Thought Xs…" + body markup.
        cell.innerHTML = `
          <details class="thinking-details">
            <summary class="thinking-summary">
              <span class="thinking-label">
                <span class="thinking-word">Thinking…</span>
              </span>
              <span class="msg-ts">${formatTs(event.ts)}</span>
            </summary>
            <div class="thinking-body thinking-empty"><em>(still thinking…)</em></div>
          </details>
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
        // Split the summary label so only "Thought" keeps the purple
        // emphasis — the duration (and later reasoning_tokens appended by
        // thinking_tokens_update) are rendered in the same dimmed hue as
        // the timestamp so they read as metadata, not headline.
        const tsHtml = `<span class="msg-ts">${formatTs(event.ts)}</span>`;
        const html = `
          <details class="thinking-details">
            <summary class="thinking-summary">
              <span class="thinking-label" data-duration-label="${escapeHtml(durSec)}">
                <span class="thinking-word">Thought</span>
                <span class="thinking-meta">${escapeHtml(durSec)}</span>
              </span>
              ${tsHtml}
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
        // the matching thinking block's label. Look up by iterating the DOM
        // rather than a CSS attribute selector — block_ids contain colons
        // which CSS.escape renders as ``\:`` inside the selector, which
        // some browsers match inconsistently depending on unescape rules.
        // Iterating + comparing dataset is unambiguous.
        const blockId = event.block_id ?? '';
        const tokens = event.reasoning_tokens ?? 0;
        if (!blockId || tokens <= 0) break;
        const cellEl = Array
          .from(messages.querySelectorAll<HTMLElement>('.msg-thinking'))
          .find(el => el.dataset.blockId === blockId);
        if (!cellEl) break;
        const labelEl = cellEl.querySelector<HTMLElement>('.thinking-label');
        if (!labelEl) break;
        let metaEl = labelEl.querySelector<HTMLElement>('.thinking-meta');
        const dur = labelEl.dataset.durationLabel ?? '';
        const metaText = dur ? `${dur} for ${tokens} toks` : `for ${tokens} toks`;
        if (!metaEl) {
          // Running-state cell (no thinking_done yet) has no .thinking-meta
          // — create one so the tokens surface immediately. thinking_done
          // will later rewrite innerHTML, but by then the meta text already
          // includes tokens, so the user never sees a "Thought Xs" flash
          // without the "for N tokens" suffix.
          metaEl = document.createElement('span');
          metaEl.className = 'thinking-meta';
          labelEl.appendChild(metaEl);
        }
        metaEl.textContent = metaText;
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
          finalizeStreamingBubble(event.content, event.usage, event.ts, event.duration_ms);
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
          // Populate the body with the placeholder result ("Task started.
          // task_id=…") so it reads the same as history replay, where the
          // paired tool_result block carries the same text. Without this,
          // the cell body stays "(pending)" until the user reloads. Keep
          // the cell non-done so the yellow running state persists until
          // tool_finalize arrives with the real output.
          const resultEl = target.querySelector('.tool-body-result .tool-body-content') as HTMLElement | null;
          if (resultEl && typeof event.result === 'string') {
            resultEl.innerHTML = renderToolResultBlock(event.result, event.result_truncated);
          }
          // Don't drop runningTools entry — the HUD ▶ name pill should stay.
          break;
        }
        if (target) {
          target.classList.add('done');
          // v2.0.23: classifier (or core/agent exception path) flagged this
          // call as a failure — flip icon to ✗ and add `.error` class so the
          // cell picks up the red chrome from style.css. Mutually exclusive
          // with the `.interrupted` terminal state set by the idle-sweep.
          if (event.is_error) target.classList.add('error');
          const durSec = (durationMs / 1000).toFixed(1) + 's';
          const summary = target.querySelector('.tool-status-summary') as HTMLElement | null;
          if (summary) setToolStatus(summary, event.is_error ? '✗' : '✓', durSec);
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
        if (summaryEl) {
          const status = kind === 'completed' ? durSec : `${kind} ${durSec}`;
          setToolStatus(summaryEl, icon, status);
        }
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

      case 'iteration_usage': {
        // v2.0.23 round-7: per-LLM-call usage for LIVE footer stamping.
        // Applies the ↑/⛀/↓ dim footer to:
        //   1. Every tool cell whose ``data-tool-use-id`` is in
        //      ``event.tool_use_ids`` — these are the tool_use blocks the
        //      LLM emitted in this iteration.
        //   2. The streaming agent cell (if ``event.has_text``) — the
        //      usage rides along so the live footer matches what history
        //      replay will paint after reload.
        // History replay doesn't use this event — ipc.py pairs
        // per_iteration_usages positionally onto the cells there.
        const usage = event.usage;
        if (!usage) break;
        const ids = event.tool_use_ids ?? [];
        for (const tuid of ids) {
          const cell = Array
            .from(messages.querySelectorAll<HTMLElement>('.msg-tool'))
            .find(el => el.dataset.toolUseId === tuid);
          if (!cell) continue;
          // Append (or replace) the footer as a sibling of .tool-body (inside
          // the <details> wrapper). Matches the agent-cell layout — putting
          // it inside .tool-body would stack padding-bottoms and leave a
          // visible chin under the dashed divider.
          const details = cell.querySelector('.tool-details');
          if (!details) continue;
          const existing = details.querySelector(':scope > .cell-usage-footer');
          if (existing) existing.remove();
          details.insertAdjacentHTML('beforeend', renderUsageFooter(usage));
        }
        if (event.has_text && streamingEl) {
          // Stash on the element so finalizeStreamingBubble() can read it
          // during the mid-turn tool_call path (finalize called with no
          // usage arg — it falls back to dataset.pendingUsage to still
          // render the footer). The primary path — the canonical 'agent'
          // event arriving later — passes usage explicitly so the stash is
          // only a safety net for interleaved turns.
          streamingEl.dataset.pendingUsage = JSON.stringify(usage);
        }
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
      //
      // v2.0.20: if the block was interrupted (turn cancelled before
      // on_thinking_end fired, placeholder survived in thinking_blocks),
      // render the same "Thinking interrupted" terminal state the live
      // stopped handler paints.
      div.className = 'msg msg-thinking';
      if (event.interrupted) div.classList.add('msg-thinking-done', 'msg-thinking-interrupted');
      if (event.block_id) div.dataset.blockId = event.block_id;
      const durSec = event.duration_ms != null
        ? (event.duration_ms / 1000).toFixed(1) + 's'
        : '';
      // v2.0.23 round-7: thinking cell's token info lives ENTIRELY in the
      // summary pill ("Thought Xs for N toks") — no ↑/⛀/↓ footer here.
      // Input/cache_read are prompt-level (shared with sibling tool/agent
      // cells from the same call), and ``reasoning_tokens`` IS the thinking
      // block's output. ``event.reasoning_tokens`` is the attributor-stamped
      // fresher value; ``event.usage.reasoning`` is the per_iteration_usages
      // positional pairing. Prefer the former, fall back to the latter so
      // history replay matches live. Drop to "Thought Xs" when neither is
      // positive (Anthropic doesn't report reasoning_tokens).
      let word = 'Thought';
      let metaText = '';
      if (event.interrupted) {
        word = 'Thinking interrupted';
      } else if (durSec) {
        const reasoning = event.reasoning_tokens
          ?? event.usage?.reasoning
          ?? 0;
        metaText = reasoning > 0 ? `${durSec} for ${reasoning} toks` : durSec;
      }
      const body = event.content ?? '';
      const bodyHtml = body
        ? `<div class="thinking-body markdown-body">${renderMarkdown(body)}</div>`
        : `<div class="thinking-body thinking-empty"><em>${event.interrupted ? '(interrupted before the provider delivered a thought)' : 'No thinking body exposed by the provider.'}</em></div>`;
      const metaHtml = metaText
        ? `<span class="thinking-meta">${escapeHtml(metaText)}</span>`
        : '';
      div.innerHTML = `
        <details class="thinking-details">
          <summary class="thinking-summary">
            <span class="thinking-label" data-duration-label="${escapeHtml(durSec)}">
              <span class="thinking-word">${escapeHtml(word)}</span>
              ${metaHtml}
            </span>
            <span class="msg-ts">${formatTs(event.ts)}</span>
          </summary>
          ${bodyHtml}
        </details>
      `;
      break;
    }

    case 'agent': {
      if (!event.content) return null;
      // v2.0.20: agent output cell matches the thinking-done chrome — a
      // <details> wrapper with a "Output Xs" summary pill + expandable
      // body. Default OPEN so the content is visible on first paint; the
      // ▸/▾ caret lets the user collapse it (parity with thinking cells).
      div.className = 'msg msg-agent msg-agent-done';
      const durSec = event.duration_ms != null
        ? (event.duration_ms / 1000).toFixed(1) + 's'
        : '';
      const metaHtml = durSec
        ? `<span class="agent-meta">${escapeHtml(durSec)}</span>`
        : '';
      // v2.0.23 round-6: tokens moved from the summary pill into a dim
      // footer inside the expanded body. Keeps the summary line compact
      // (just "Agent 2.4s · 18:01") and matches thinking/tool cell chrome.
      const footer = renderUsageFooter(event.usage);
      div.innerHTML = `
        <details class="agent-details" open>
          <summary class="agent-summary">
            <span class="agent-label">
              <span class="agent-word">Agent</span>
              ${metaHtml}
            </span>
            <span class="msg-ts">${formatTs(event.ts)}</span>
          </summary>
          <div class="agent-body markdown-body">${renderMarkdown(event.content)}</div>
          ${footer}
        </details>
      `;
      break;
    }

    case 'user': {
      if (!event.content) return null;
      // v2.0.23: collapsible glass-card chrome matching thinking/tool/agent
      // cells. Three visual variants keyed off the backend-propagated
      // caller/source fields:
      //   - bg tool output: caller=system + source=panel → orange-yellow
      //   - (reserved) task wakeup user_input: caller=task → sky blue
      //   - everything else (human chat): green glass "You"
      const variant = userCellVariant(event);
      const { labelTitle, labelDim } = userCellLabel(event, variant);
      div.className = `msg msg-user msg-user-${variant}`;
      div.innerHTML = `
        <details class="user-details" open>
          <summary class="user-summary">
            <span class="user-label">
              <span class="user-word">${labelTitle}</span>
              ${labelDim ? `<span class="user-meta">${escapeHtml(labelDim)}</span>` : ''}
            </span>
            <span class="msg-ts">${formatTs(event.ts)}</span>
          </summary>
          <div class="user-body markdown-body">${renderMarkdown(event.content)}</div>
        </details>
      `;
      break;
    }

    case 'tool': {
      // Default to "done" styling because renderEvent is also used by history
      // replay (completed turns). handleEvent's live `tool` case explicitly
      // strips the .done class after appending to show the running state.
      // v2.0.23: history replay also needs to surface error-classified calls
      // — ipc.py stamps `is_error` onto reloaded tool events from the paired
      // tool_result block so the red cell survives page reloads.
      div.className = event.is_error
        ? 'msg msg-tool done error'
        : 'msg msg-tool done';
      div.dataset.toolName = event.name ?? 'tool';
      // v2.0.23 round-7: stamp ``data-tool-use-id`` so the live
      // ``iteration_usage`` handler can target this cell by id (reliable
      // when two concurrent calls of the same tool fire in one iteration).
      // ipc.py forwards it as ``event.id`` for both live (tool_call → tool)
      // and history-replay (turn tool_use block) paths.
      if (event.id) div.dataset.toolUseId = event.id;
      div.innerHTML = renderToolEvent(event);
      break;
    }

    // tool_done / loop_start / loop_end are handled in handleEvent (transition
    // the live msg-tool to "done" and update the HUD); they don't produce
    // separate log lines anymore. This keeps the transcript quiet and
    // matches the uniform `✓ name (duration)` pattern the user asked for.

    case 'task_wakeup': {
      // v2.0.23: unified chrome with the `user` variants — sky-blue metallic
      // "Wakeup" card. Summary shows "Wakeup — <card>"; body renders the
      // resolved task prompt (session.py::_do_tick stamps it on the event
      // from v2.0.23) as plain text so the wakeup is self-describing without
      // needing to expand the turn below to figure out what the agent was
      // told to do.
      div.className = 'msg msg-user msg-user-task';
      const cardDim = event.card ? `— ${event.card}` : '';
      const promptBody = (event.prompt || '').trim();
      const body = promptBody
        ? `<div class="user-body">${renderMarkdown(promptBody)}</div>`
        : `<div class="user-body"><em class="user-body-empty">(no prompt captured)</em></div>`;
      div.innerHTML = `
        <details class="user-details">
          <summary class="user-summary">
            <span class="user-label">
              <span class="user-word">Wakeup</span>
              ${cardDim ? `<span class="user-meta">${escapeHtml(cardDim)}</span>` : ''}
            </span>
            <span class="msg-ts">${formatTs(event.ts)}</span>
          </summary>
          ${body}
        </details>
      `;
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

  // History replay path: ipc.py stamps duration_ms onto the tool event so
  // the "✓ bash 2.4s …" pill survives reload (the live tool_done that would
  // populate this span isn't replayed on history fetch).
  const durationText = typeof event.duration_ms === 'number'
    ? (event.duration_ms / 1000).toFixed(1) + 's'
    : '';

  // v2.0.23: flip the initial icon to ✗ when history replay or live tool_done
  // carries is_error — renderEvent's `tool` case already added the `.error`
  // class so CSS colours it red; this just swaps the glyph.
  const icon = event.is_error ? '✗' : '✓';
  // v2.0.23 round-7: footer lives as a SIBLING of .tool-body (not inside)
  // so it matches the agent cell chrome — tool-body's own padding-bottom
  // would otherwise stack with the footer's padding and produce a visible
  // "chin" under the dashed divider.
  const footer = renderUsageFooter(event.usage);
  return `
    <details class="tool-details">
      <summary class="tool-status-summary">
        <span class="tool-status-icon">${icon}</span>
        <span class="tool-status-name">${escapeHtml(name)}</span>
        <span class="tool-status-duration">${escapeHtml(durationText)}</span>
        ${argHtml}
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
      ${footer}
    </details>
  `;
}

// v2.0.23 round-7: dim token footer for tool/agent cell bodies. A single
// LLM call returns one ``usage`` dict — the same ↑/⛀/↓ values apply to
// every content block that call emits (thinking + tool_use + text). We
// cannot split input/cache across blocks (prompt-level, not block-level)
// but we CAN split ``output``: ``reasoning_tokens`` is provider-reported
// as a subset of ``output_tokens`` (OpenAI spec, mirrored by Anthropic
// extended thinking + Codex + Kimi), so the thinking portion is attributed
// to the thinking cell's summary pill ("for N toks") and the tool/agent
// footer shows the *non-reasoning* remainder: ``output - reasoning``.
// When reasoning is 0 (Anthropic standard, non-thinking models) the
// footer falls back to full ``output`` — there's nothing else to take out.
// Thinking cells do NOT call this helper; their token info lives entirely
// in the summary pill per the round-7 design.
function renderUsageFooter(usage?: DisplayEvent['usage']): string {
  if (!usage) return '';
  const u = usage as Record<string, number | undefined>;
  const parts: string[] = [];
  if (u.input != null) parts.push(`↑ ${u.input}`);
  if (u.cache_read != null && u.cache_read > 0) parts.push(`⛀ ${u.cache_read}`);
  // ↓ = non-reasoning output. If reasoning wasn't reported (0 or undefined),
  // fall back to full output so the cell still shows something meaningful.
  const output = u.output ?? 0;
  const reasoning = u.reasoning ?? 0;
  const nonReasoning = reasoning > 0 && output > reasoning ? output - reasoning : output;
  if (nonReasoning > 0) parts.push(`↓ ${nonReasoning}`);
  if (!parts.length) return '';
  return `<div class="cell-usage-footer">${escapeHtml(parts.join('  '))}</div>`;
}

// v2.0.23: classify a 'user' display event into one of four card variants.
// Pure function of the fields the backend propagates from the originating
// user_input event (see runtime/ipc.py _context_event_to_display and
// session.py _drain_background_events). `sub-agent` is a refinement of the
// `tool-output` path — when the background tool is `sub_agent` it gets its
// own orange metallic chrome + display_name sub-label, so the cell reads as
// "Sub-agent — my-helper" instead of the generic "Tool output — sub_agent".
type UserVariant = 'you' | 'tool-output' | 'sub-agent' | 'task';
function userCellVariant(event: DisplayEvent): UserVariant {
  if (event.caller === 'system' && event.source === 'panel') {
    return event.tool_name === 'sub_agent' ? 'sub-agent' : 'tool-output';
  }
  if (event.caller === 'task') return 'task';
  return 'you';
}

function userCellLabel(event: DisplayEvent, variant: UserVariant): { labelTitle: string; labelDim: string } {
  if (variant === 'sub-agent') {
    const name = event.display_name || event.tid || '';
    return { labelTitle: 'Sub-agent', labelDim: name ? `— ${name}` : '' };
  }
  if (variant === 'tool-output') {
    const tool = event.tool_name || event.name || '';
    return { labelTitle: 'Tool output', labelDim: tool ? `— ${tool}` : '' };
  }
  if (variant === 'task') {
    return { labelTitle: 'Wakeup', labelDim: event.card ? `— ${event.card}` : '' };
  }
  return { labelTitle: 'You', labelDim: '' };
}

function renderToolResultBlock(result: string, truncated?: boolean): string {
  if (!result) return '<em class="tool-body-empty">(empty)</em>';
  const suffix = truncated ? '\n\n…[truncated]' : '';
  return `<pre>${escapeHtml(result + suffix)}</pre>`;
}

function setToolStatus(summaryEl: HTMLElement, icon: string, status: string): void {
  // Update the icon + inline status pill on a tool-status-summary row; the
  // name and (v2.0.18) inline arg spans are preserved. The status pill is
  // the span that shows "running…" while the tool is live and flips to the
  // execution duration (e.g. "2.4s") when it finishes. innerHTML on the
  // whole summary would clobber the arg and force every caller to re-pass
  // the tool input, which is not available to tool_done / tool_progress /
  // tool_finalize handlers.
  const iconEl = summaryEl.querySelector<HTMLElement>('.tool-status-icon');
  const statusEl = summaryEl.querySelector<HTMLElement>('.tool-status-duration');
  if (iconEl) iconEl.textContent = icon;
  if (statusEl) statusEl.textContent = status;
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
