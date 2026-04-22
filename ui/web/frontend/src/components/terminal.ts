import { api } from '../api';

type LogEntry = {
  ts: number;
  source: string;
  text: string;
  seq?: number;        // server-assigned monotonic id; absent on optimistic echoes
  optimistic?: boolean; // client-drew this before the server confirmed
};
type TerminalState = {
  active: boolean;
  cwd: string | null;
  cwd_display: string | null;
  home: string | null;
  venv: string | null;
  git_branch: string | null;
  git_dirty: boolean | null;
  last_active_at: number | null;
  foreground_pid: number | null;
  foreground_cmd: string | null;
  locked_by: 'agent' | 'user' | null;
  shell_pid: number | null;
};

const defaultState = (): TerminalState => ({
  active: false,
  cwd: null,
  cwd_display: null,
  home: null,
  venv: null,
  git_branch: null,
  git_dirty: null,
  last_active_at: null,
  foreground_pid: null,
  foreground_cmd: null,
  locked_by: null,
  shell_pid: null,
});

/**
 * Replace the user's home prefix in an absolute path with ``~``.
 *
 * Prefers ``state.home`` when the backend wrote it (new daemon). Falls
 * back to the well-known Mac (``/Users/<name>``) / Linux
 * (``/home/<name>``) prefix heuristics so the HUD still looks right
 * during an upgrade where the daemon is pre-v2.0.34 but the frontend
 * is fresh. Worst case we leave the absolute path untouched.
 */
function homifyPath(cwd: string, home: string | null): string {
  if (!cwd) return cwd;
  if (home && home !== '~') {
    if (cwd === home) return '~';
    if (cwd.startsWith(home + '/')) return '~' + cwd.slice(home.length);
  }
  // Regex fallback: chomp the first "/Users/<anyone>" or "/home/<anyone>".
  const m = /^\/(Users|home)\/[^/]+(\/|$)/.exec(cwd);
  if (m) {
    const tail = cwd.slice(m[0].length);
    return tail ? '~/' + tail : '~';
  }
  return cwd;
}

function escHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Pinned-at-top Terminal card for the Panel tab.
 *
 * Holds all visual state (log buffer, input draft, scroll position) outside
 * the DOM so panel.ts's wholesale `innerHTML = …` re-render can't nuke
 * in-progress user typing. Consumers embed `renderHtml()` into the panel
 * markup and call `rebind(container)` after innerHTML settles.
 *
 * SSE dispatch: `main.ts` forwards `terminal_log` / `terminal_state` /
 * `terminal_rejected` events via `handleEvent(evt)`. The controller updates
 * its buffers and applies incremental DOM updates when already mounted.
 */
class TerminalController {
  private sessionId: string | null = null;
  private state: TerminalState = defaultState();
  // Single source of truth. Every server-confirmed entry has a monotonic
  // `seq` (assigned by TerminalLogger._append_entry); optimistic echoes
  // carry `optimistic:true` and no seq until the matching SSE confirms.
  private entries: LogEntry[] = [];
  private latestSeq: number = -1;
  private inputDraft: string = '';
  // HUD row default is collapsed: long prompts like
  // "(base) ~/agent_core/butterfly-agent: feature/terminal-panel..."
  // are clipped with ``…`` and the whole row becomes a click target
  // that flips to the expanded view (wraps, shows full text). Kept on
  // the controller so panel re-renders don't reset the toggle.
  private hudExpanded: boolean = false;
  private scrollPinnedToBottom: boolean = true;
  // Distance from the bottom of the scrollable output in pixels. Saved
  // every scroll tick so we can restore the user's viewport across
  // panel re-renders (each render() blows .terminal-output's innerHTML
  // away, which resets scrollTop to 0 — making mid-terminal scrolling
  // jump to the top). Only used when `scrollPinnedToBottom` is false.
  private scrollFromBottom: number = 0;
  private loaded: boolean = false;
  private root: HTMLElement | null = null;

  /** Fetch initial state + log tail for the session. Safe to re-call. */
  async attachSession(id: string): Promise<void> {
    this.sessionId = id;
    this.state = defaultState();
    this.entries = [];
    this.latestSeq = -1;
    this.inputDraft = '';
    this.hudExpanded = false;
    this.scrollPinnedToBottom = true;
    this.scrollFromBottom = 0;
    this.loaded = false;
    try {
      const snap = await api.getTerminal(id, 500);
      if (this.sessionId !== id) return; // session switch raced
      this.state = snap.state;
      this.entries = (snap.log as LogEntry[]).slice();
      // Seed latestSeq from the snapshot so concurrent SSE events that
      // were also captured in the snapshot are dropped by the dedup
      // path instead of rendering twice.
      for (const e of this.entries) {
        if (typeof e.seq === 'number' && e.seq > this.latestSeq) {
          this.latestSeq = e.seq;
        }
      }
      this.loaded = true;
      this.applyStateToChrome();
      this.renderOutputFull();
    } catch (err) {
      console.error('Failed to fetch terminal state:', err);
      this.loaded = true;
      this.applyStateToChrome();
    }
  }

  /** Called by main.ts for each SSE event. No-op for non-terminal types. */
  handleEvent(event: { type?: string; [k: string]: unknown }): void {
    if (event.type === 'terminal_log') {
      const seq = typeof event.seq === 'number' ? (event.seq as number) : undefined;
      // Strict dedup on seq: ignore anything we've already processed
      // (covers the SSE-vs-initial-fetch race). Un-sequenced entries
      // shouldn't happen from the server — if one appears, fall back
      // to append-without-dedup rather than dropping it.
      if (typeof seq === 'number' && seq <= this.latestSeq) return;

      const e: LogEntry = {
        ts: Number(event.ts ?? Date.now() / 1000),
        source: String(event.source ?? 'system'),
        text: String(event.text ?? ''),
        seq,
      };

      // Optimistic-echo reconcile: if we have a pending user_cmd draw
      // (client-side, no seq) whose text matches, upgrade that existing
      // entry in place instead of pushing a duplicate.
      if (e.source === 'user_cmd') {
        const idx = this.entries.findIndex(
          (x) => x.optimistic && x.source === 'user_cmd' && x.text.trim() === e.text.trim()
        );
        if (idx >= 0) {
          this.entries[idx] = { ...this.entries[idx], seq, optimistic: false, ts: e.ts };
          if (typeof seq === 'number') this.latestSeq = seq;
          return;
        }
      }

      this.entries.push(e);
      if (typeof seq === 'number') this.latestSeq = seq;
      this.appendLogLine(e);
    } else if (event.type === 'terminal_state') {
      const s = event.state as Partial<TerminalState> | undefined;
      if (s) {
        this.state = { ...this.state, ...s };
        this.applyStateToChrome();
      }
    } else if (event.type === 'terminal_rejected') {
      this.flashRejected(String(event.reason ?? 'locked'));
    }
  }

  /** HTML the panel embeds. rebind() must follow.
   *
   * Idle / "shell not open" state is surfaced via the input-box
   * placeholder + disabled attribute — no floating overlay in the body.
   */
  renderHtml(): string {
    const statusHtml = this.renderStatusRow();
    const hudHtml = this.renderHudRow();
    return `
      <div class="terminal-container" data-loaded="${this.loaded ? '1' : '0'}">
        <div class="terminal-chrome">
          <div class="terminal-title">Terminal</div>
          ${statusHtml}
        </div>
        <div class="terminal-body">
          <pre class="terminal-output" role="log" aria-live="polite"></pre>
        </div>
        ${hudHtml}
        <div class="terminal-inputbar">
          <span class="terminal-prompt">$</span>
          <input type="text" class="terminal-input"
                 autocomplete="off" autocapitalize="off" spellcheck="false"
                 placeholder="Type a command…" />
          <button type="button" class="terminal-btn-interrupt" title="Send Ctrl-C">⚡</button>
        </div>
      </div>
    `;
  }

  private bindHud(root: HTMLElement): void {
    const hud = root.querySelector('.terminal-hud') as HTMLElement | null;
    if (!hud) return;
    const toggle = () => {
      this.hudExpanded = !this.hudExpanded;
      // Flip attribute in-place — no need for a full re-render, the
      // CSS selector keys off [data-expanded]. Keeps focus + avoids a
      // DOM teardown mid-click.
      hud.setAttribute('data-expanded', this.hudExpanded ? '1' : '0');
      hud.setAttribute(
        'title', this.hudExpanded ? 'Click to collapse' : 'Click to expand'
      );
    };
    hud.addEventListener('click', (e) => {
      // Swallow clicks that are really on the input below — the HUD
      // sits snug to the input bar and iOS mis-targets by a px or two.
      // Safe because .terminal-hud has no interactive children.
      e.stopPropagation();
      toggle();
    });
    hud.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    });
  }

  /** Reconnect listeners + restore state into the freshly-innerHTML'd DOM. */
  rebind(root: HTMLElement): void {
    this.root = root;
    this.bindHud(root);
    const input = root.querySelector('.terminal-input') as HTMLInputElement | null;
    if (input) {
      input.value = this.inputDraft;
      input.addEventListener('input', () => {
        this.inputDraft = input.value;
      });
      // Chinese IME: Enter that confirms a candidate must not submit.
      // Mirrors chat.ts:1035-1047 — track composition state locally so a
      // mid-composition Enter is treated as a candidate-picker keypress.
      let isComposing = false;
      input.addEventListener('compositionstart', () => { isComposing = true; });
      input.addEventListener('compositionend', () => { isComposing = false; });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !isComposing && !e.isComposing) {
          e.preventDefault();
          this.submitInput();
        }
      });
      this.applyStateToInput(input);
      // Auto-focus when panel opens, unless input is disabled.
      if (!this.inputDisabled()) {
        // Defer so other rebinds in the same tick don't steal focus.
        setTimeout(() => {
          if (document.activeElement !== input) {
            // Only steal focus if the user hasn't clicked something else.
            if (!document.activeElement || document.activeElement === document.body) {
              input.focus();
            }
          }
        }, 0);
      }
    }
    const btn = root.querySelector('.terminal-btn-interrupt') as HTMLButtonElement | null;
    if (btn) {
      btn.addEventListener('click', () => this.sendInterrupt());
      if (this.inputDisabled()) btn.setAttribute('disabled', 'disabled');
    }
    const output = root.querySelector('.terminal-output') as HTMLElement | null;
    if (output) {
      output.addEventListener('scroll', () => {
        // Pin to bottom only if user is near the bottom when they scroll.
        const fromBottom =
          output.scrollHeight - output.scrollTop - output.clientHeight;
        this.scrollPinnedToBottom = fromBottom < 24;
        this.scrollFromBottom = Math.max(0, fromBottom);
      });
    }
    this.renderOutputFull();
    this.applyStateToChrome();
  }

  // ── internals ──────────────────────────────────────────────────────

  private agentLocked(): boolean {
    return this.state.locked_by === 'agent';
  }

  private inputDisabled(): boolean {
    // Disabled when the agent holds the lock OR when the shell has not
    // been opened yet (fix: users couldn't use a shell the agent hadn't
    // started — a typed command would silently queue against a
    // never-alive pty).
    return this.agentLocked() || !this.state.active;
  }

  private async submitInput(): Promise<void> {
    if (!this.sessionId) return;
    if (this.inputDisabled()) return;
    const input = this.root?.querySelector('.terminal-input') as HTMLInputElement | null;
    if (!input) return;
    const raw = input.value;
    if (!raw) return;
    // Optimistically echo the user's command line. Mark optimistic so
    // the matching server-side terminal_log can reconcile this entry in
    // place (no duplicate, no out-of-order render).
    const optimistic: LogEntry = {
      ts: Date.now() / 1000,
      source: 'user_cmd',
      text: raw,
      optimistic: true,
    };
    this.entries.push(optimistic);
    this.appendLogLine(optimistic);
    input.value = '';
    this.inputDraft = '';
    try {
      await api.postTerminalInput(this.sessionId, raw + '\n');
    } catch (err) {
      console.error('Failed to submit terminal input:', err);
      this.flashRejected('send-failed');
    }
  }

  private async sendInterrupt(): Promise<void> {
    if (!this.sessionId || this.inputDisabled()) return;
    try {
      await api.postTerminalInterrupt(this.sessionId);
    } catch (err) {
      console.error('Failed to send terminal interrupt:', err);
    }
  }

  private renderHudRow(): string {
    // Mirror of state.json's env fingerprint rendered as a single
    // terminal-prompt-style line above the input bar. Format:
    //   (<venv>) <path>: <git_branch><* if dirty>
    // git part omitted when not in a repo:
    //   (<venv>) <path>
    // Path is ``~``-shortened by the backend (state.cwd_display) with
    // a client-side fallback for pre-v2.0.34 daemons.
    //
    // Display modes:
    //   collapsed (default) — single line, truncated with ``…``,
    //     click anywhere on the row to expand.
    //   expanded            — wraps across lines, shows full text,
    //     click again to collapse.
    if (!this.state.active) {
      return `<div class="terminal-hud"><span class="terminal-hud-empty">—</span></div>`;
    }
    const venv = this.state.venv || '';
    const cwd = this.state.cwd_display
      || homifyPath(this.state.cwd || '', this.state.home);
    const branch = this.state.git_branch || '';
    const dirty = this.state.git_dirty === true;

    let inner = '';
    if (venv) {
      inner += `<span class="terminal-hud-venv">(${escHtml(venv)})</span>`;
    }
    if (cwd) {
      if (inner) inner += ' ';
      inner += `<span class="terminal-hud-path">${escHtml(cwd)}</span>`;
    }
    if (branch) {
      // "git(main*)" — the wrapper self-identifies the field and the
      // dirty marker sits inside the parens next to the branch name.
      // Two colour zones: the "git(" + ")" shell is pink
      // (.terminal-hud-git-label); the branch + "*" inside is sky
      // blue (.terminal-hud-git-branch).
      const body = `${escHtml(branch)}${dirty ? '*' : ''}`;
      inner += (
        `<span class="terminal-hud-sep">:</span> `
        + `<span class="terminal-hud-git-label">git(</span>`
        + `<span class="terminal-hud-git-branch">${body}</span>`
        + `<span class="terminal-hud-git-label">)</span>`
      );
    }
    if (!inner) {
      return `<div class="terminal-hud"><span class="terminal-hud-empty">—</span></div>`;
    }
    const expandedAttr = this.hudExpanded ? '1' : '0';
    const titleHint = this.hudExpanded ? 'Click to collapse' : 'Click to expand';
    return `
      <div class="terminal-hud" data-expanded="${expandedAttr}" role="button"
           tabindex="0" title="${titleHint}">
        <div class="terminal-hud-line">${inner}</div>
      </div>
    `;
  }

  private renderStatusRow(): string {
    if (!this.loaded) return `<div class="terminal-status">loading…</div>`;
    if (!this.state.active) {
      return `<div class="terminal-status terminal-status-idle">idle</div>`;
    }
    const lock = this.state.locked_by;
    const lockPill = lock === 'agent'
      ? '<span class="terminal-lock-pill lock-agent">🔒 agent using</span>'
      : lock === 'user'
        ? '<span class="terminal-lock-pill lock-user">user typing…</span>'
        : '<span class="terminal-lock-pill lock-free">ready</span>';
    return `<div class="terminal-status">${lockPill}</div>`;
  }

  private applyStateToChrome(): void {
    if (!this.root) return;
    const status = this.root.querySelector('.terminal-status');
    if (status) status.outerHTML = this.renderStatusRow();
    const hud = this.root.querySelector('.terminal-hud');
    if (hud) {
      hud.outerHTML = this.renderHudRow();
      // outerHTML replaces the node → the click listener we attached
      // in bindHud is on the gone-stale element. Re-bind against the
      // fresh one that querySelector will find next.
      this.bindHud(this.root);
    }
    const input = this.root.querySelector('.terminal-input') as HTMLInputElement | null;
    if (input) this.applyStateToInput(input);
    const btn = this.root.querySelector('.terminal-btn-interrupt') as HTMLButtonElement | null;
    if (btn) {
      if (this.inputDisabled()) btn.setAttribute('disabled', 'disabled');
      else btn.removeAttribute('disabled');
    }
  }

  private applyStateToInput(input: HTMLInputElement): void {
    if (this.agentLocked()) {
      input.setAttribute('disabled', 'disabled');
      input.placeholder = 'Agent is using terminal…';
    } else if (!this.state.active) {
      input.setAttribute('disabled', 'disabled');
      input.placeholder = 'Shell not open yet — waiting for the agent to call terminal_create';
    } else {
      input.removeAttribute('disabled');
      input.placeholder = 'Type a command…';
    }
  }

  private renderOutputFull(): void {
    if (!this.root) return;
    const output = this.root.querySelector('.terminal-output') as HTMLElement | null;
    if (!output) return;
    output.innerHTML = this.entries.map(e => this.renderEntry(e)).join('');
    if (this.scrollPinnedToBottom) {
      output.scrollTop = output.scrollHeight;
    } else {
      // Restore the user's scroll position (measured as distance from
      // the bottom) so a mid-terminal viewport survives panel re-renders.
      const target = output.scrollHeight - output.clientHeight - this.scrollFromBottom;
      output.scrollTop = Math.max(0, target);
    }
  }

  private appendLogLine(e: LogEntry): void {
    if (!this.root) return;
    const output = this.root.querySelector('.terminal-output') as HTMLElement | null;
    if (!output) return;
    output.insertAdjacentHTML('beforeend', this.renderEntry(e));
    if (this.scrollPinnedToBottom) output.scrollTop = output.scrollHeight;
    // If the user is scrolled-up, do NOT auto-scroll — honour their position.
  }

  private renderEntry(e: LogEntry): string {
    const src = e.source;
    let cls = 'term-line';
    let prefix = '';
    if (src === 'agent_cmd') {
      cls += ' term-agent-cmd';
      prefix = '<span class="term-prompt-agent">$ </span>';
    } else if (src === 'user_cmd') {
      cls += ' term-user-cmd';
      prefix = '<span class="term-prompt-user">$ </span>';
    } else if (src === 'system') {
      cls += ' term-system';
    } else if (src === 'agent_out') {
      cls += ' term-agent-out';
    } else if (src === 'user_out') {
      cls += ' term-user-out';
    } else {
      cls += ' term-other';
    }
    // Do NOT re-escape whitespace — output already contains \n which the
    // <pre> preserves. Embed text in a span so the prefix stays fixed.
    return `<span class="${cls}">${prefix}<span class="term-text">${escHtml(e.text)}</span></span>`;
  }

  private flashRejected(reason: string): void {
    if (!this.root) return;
    const status = this.root.querySelector('.terminal-status');
    if (!status) return;
    const orig = status.innerHTML;
    status.innerHTML = `<span class="terminal-status-rejected">⚠ ${escHtml(reason)}</span>`;
    setTimeout(() => {
      // Re-render status from current state rather than restoring the
      // snapshot — state may have changed during the flash.
      this.applyStateToChrome();
    }, 1800);
    void orig;
  }
}

export const terminalController = new TerminalController();
