// BfEvent → DisplayEvent adapter.
//
// PR #57 ("Refactor: events.jsonl as single source of truth + unified
// runtime.io") replaced the dual context.jsonl/events.jsonl streams with a
// single events_v1.jsonl whose envelope is `{id, ts, type, for_llm, payload}`.
// The pre-refactor frontend (restored in this PR) speaks the old DisplayEvent
// shape with a flat `{type, content, ...}` schema. This module is the single
// translation seam — every BfEvent that crosses the wire (SSE or /history
// replay) is funneled through `bfToDisplay()` before any view code sees it.
//
// Mapping is best-effort: retired event types from §3.6 (partial_text,
// agent_output_start/done, thinking_start/done, loop_start/end,
// iteration_usage, thinking_tokens_update, tool_finalize) are simply not
// emitted by the new server, so the corresponding old code paths just go
// quiet — final renders still arrive via agent_text / agent_thinking /
// agent_tool_result. UX cost: no streaming text mid-call (text appears at
// call end), no live thinking spinner. Acceptable for restoring presentation.

import type { DisplayEvent } from './types';

export interface BfEvent {
  id: number;
  ts: number;
  type: string;
  for_llm: boolean;
  payload: Record<string, any>;
}

function tsToIso(ts: number): string {
  // BfEvent.ts is unix epoch seconds (float). DisplayEvent.ts is an ISO string.
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return new Date().toISOString();
  return new Date(ts * 1000).toISOString();
}

function withCommon(e: BfEvent, fields: Partial<DisplayEvent>): DisplayEvent {
  return {
    id: String(e.id),
    ts: tsToIso(e.ts),
    ...fields,
  } as DisplayEvent;
}

/** Translate one BfEvent into zero or more DisplayEvents. Returning [] drops
 *  the event silently; the most common reason is a control-only signal that
 *  the old UI never rendered. */
export function bfToDisplay(e: BfEvent): DisplayEvent[] {
  const p = e.payload || {};
  switch (e.type) {
    // ── User-side ─────────────────────────────────────────────────────
    case 'user_input':
      return [withCommon(e, {
        type: 'user',
        content: typeof p.text === 'string' ? p.text : '',
        source: p.source ?? undefined,
        caller: p.caller ?? undefined,
        tool_name: p.tool_name ?? undefined,
        display_name: p.display_name ?? undefined,
        sub_agent_mode: p.sub_agent_mode ?? undefined,
        prompt: p.prompt ?? undefined,
        card: p.card ?? undefined,
      })];

    case 'user_interrupt':
      // text=null is a pure control signal — drop it. text="..." is a
      // user-typed interrupt note; render as a regular user message so the
      // chat shows what the operator said when interrupting.
      if (p.text == null || p.text === '') return [];
      return [withCommon(e, {
        type: 'user',
        content: String(p.text),
        source: p.source ?? 'user',
      })];

    // ── Agent-side ────────────────────────────────────────────────────
    case 'agent_text':
      return [withCommon(e, {
        type: 'agent',
        content: typeof p.text === 'string' ? p.text : '',
        // model field carried for HUD label parity.
        // (DisplayEvent.value isn't model — we just stuff it into a known slot
        // by reusing the open-ended map; old code reads .value occasionally.)
      })];

    case 'agent_thinking':
      return [withCommon(e, {
        type: 'thinking',
        text: typeof p.text === 'string' ? p.text : (p.summary ?? ''),
        duration_ms: typeof p.duration_ms === 'number' ? p.duration_ms : undefined,
        interrupted: Boolean(p.interrupted),
        reasoning_tokens: typeof p.reasoning_tokens === 'number' ? p.reasoning_tokens : undefined,
        // signature/summary kept on the event for any code that reads them.
        block_id: p.block_id ?? undefined,
      })];

    case 'agent_tool_call':
      return [withCommon(e, {
        type: 'tool',
        name: typeof p.tool_name === 'string' ? p.tool_name : '',
        input: (p.args && typeof p.args === 'object') ? p.args : {},
        // Old DisplayEvent didn't formalise tool_use_id (it was added later)
        // — store on the loose record so chat.ts's `event.tool_use_id`
        // accesses still work.
        ...({ tool_use_id: p.tool_use_id } as Partial<DisplayEvent>),
      })];

    case 'agent_tool_result': {
      const result = p.result == null ? '' : String(p.result);
      return [withCommon(e, {
        type: 'tool_done',
        result,
        result_len: result.length,
        is_error: Boolean(p.is_error),
        // is_background on new schema = "this finished in bg"; the old UI
        // expects tool_done(is_background=true) as a placeholder waiting for
        // tool_finalize. Since the new server emits no follow-up, we lie and
        // mark is_background=false so the cell finalises immediately.
        is_background: false,
        duration_ms: typeof p.duration_ms === 'number' ? p.duration_ms : undefined,
        ...({ tool_use_id: p.tool_use_id, tool_name: p.tool_name } as Partial<DisplayEvent>),
      })];
    }

    // ── System: HUD / model state ─────────────────────────────────────
    case 'model_status':
      return [withCommon(e, {
        type: 'model_status',
        state: p.status ?? p.state,
        source: p.source ?? null,
        // No `.model` field on DisplayEvent — chat.ts reads modelState from
        // /api/hud, so omitting here is fine.
      })];

    case 'llm_call_usage':
      return [withCommon(e, {
        type: 'llm_call_usage',
        iteration: p.iteration,
        usage: p.usage,
        context_tokens: p.context_tokens,
        toks_per_s: p.toks_per_s,
        duration_ms: p.duration_ms,
      })];

    // ── System: tasks / todo ──────────────────────────────────────────
    case 'task_card_changed':
      return [withCommon(e, {
        type: 'task_card_changed',
        card: p.name ?? p.card,
        value: p.change ?? undefined,
      })];

    case 'task_script_check':
      return [withCommon(e, {
        type: 'task_check',
        card: p.name ?? p.card,
        state: p.status ?? p.state ?? undefined,
      })];

    case 'task_script_error':
      return [withCommon(e, {
        type: 'task_check_error',
        card: p.name ?? p.card,
        message: p.error ?? p.message ?? '',
      })];

    case 'task_finished':
      return [withCommon(e, {
        type: 'task_finished',
        card: p.name ?? p.card,
        triggered_by: p.reason ?? undefined,
      })];

    case 'todo_list_changed':
      // todo_list payload carried; old UI just refreshes via /todo_list when
      // it sees this event, so a bare type passthrough is enough.
      return [withCommon(e, {
        type: 'todo_list_changed',
        ...(p.todo_list && typeof p.todo_list === 'object' ? { } : { }),
      })];

    // ── System: panel / sub-agents / tool progress ────────────────────
    case 'panel_entry_changed':
      // Old name: 'panel_update'. Payload shape (tid + entry) carried in
      // ``payload`` — old code re-fetches /panel on this signal so we just
      // need the type to match.
      return [withCommon(e, {
        type: 'panel_update',
        tid: p.tid ?? undefined,
        ...(p.entry && typeof p.entry === 'object' ? { state: (p.entry as any).status } : {}),
      })];

    case 'sub_agent_count':
      return [withCommon(e, {
        type: 'sub_agent_count',
        running: typeof p.running === 'number' ? p.running : 0,
      })];

    case 'tool_progress':
      return [withCommon(e, {
        type: 'tool_progress',
        tid: p.tid ?? undefined,
        summary: p.summary ?? '',
        ...({ tool_use_id: p.tool_use_id } as Partial<DisplayEvent>),
      })];

    // ── System: terminal ──────────────────────────────────────────────
    case 'terminal_log':
      return [withCommon(e, { type: 'terminal_log', ...p } as DisplayEvent)];
    case 'terminal_state':
      return [withCommon(e, { type: 'terminal_state', ...p } as DisplayEvent)];
    case 'terminal_input':
      // Old UI subscribed to 'terminal_log' for both directions; surface as
      // a log entry so the existing handler renders it.
      return [withCommon(e, {
        type: 'terminal_log',
        source: p.source ?? 'user',
        text: typeof p.content === 'string' ? p.content : (p.text ?? ''),
        ...p,
      } as DisplayEvent)];

    // ── System: notices / errors ──────────────────────────────────────
    case 'system_notice':
      return [withCommon(e, {
        type: 'system_notice',
        message: typeof p.message === 'string' ? p.message : (p.text ?? ''),
        ...p,
      } as DisplayEvent)];

    case 'error':
      return [withCommon(e, {
        type: 'error',
        content: typeof p.message === 'string' ? p.message
               : typeof p.content === 'string' ? p.content
               : '',
        ...p,
      } as DisplayEvent)];

    // ── Lifecycle / control — drop, old UI didn't render these ────────
    case 'session_created':
    case 'session_started':
    case 'session_stopped':
    case 'session_deleted':
    case 'control_interrupt':
    case 'control_start':
    case 'control_stop':
    case 'config_changed':
    case 'prompt_changed':
    case 'asset_changed':
      return [];

    default:
      // Unknown new event — pass through with type only. Old chat.ts ignores
      // unknown types; the side panels may peek at the raw payload.
      return [withCommon(e, { type: e.type, ...p } as DisplayEvent)];
  }
}
