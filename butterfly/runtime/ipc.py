"""FileIPC: file-based IPC between butterfly daemon and chat UI.

Two files per session:

  context.jsonl  — PURE conversation history (user_input + turn only).
                   Sole purpose: restore the full conversation so the daemon
                   can reconstruct agent._history and send correct context to
                   Claude on every new run. Nothing else lives here.

  events.jsonl   — ALL runtime / UI signalling events:
                   model_status, partial_text, tool_call, task_wakeup,
                   task_finished, status, error.
                   Consumed by the SSE stream; never used by load_history().

context.jsonl event types:
  user_input   — UI → daemon: {"type": "user_input", "content": "...", "id": "...",
                               "ts": "...", "mode": "interrupt|wait" (optional, default
                               via pending_inputs.default_mode_for_source: user/panel
                               → interrupt, task → wait)}
  turn         — daemon → UI: {"type": "turn", "triggered_by": "user|task:<name>",
                               "messages": [...], "ts": "...",
                               "merged_user_input_ids": [...] (optional; set when more
                               than one user_input event was merged into the turn)}
  task_wakeup  — daemon → UI: {"type": "task_wakeup", "card": "...", "prompt": "...",
                               "ts": "..."} — v2.0.23: moved to context.jsonl so
                               history replay can render the Wakeup card. Daemon's
                               ``poll_inputs`` filters on ``type=="user_input"`` so
                               this marker does NOT re-enqueue the task.

events.jsonl event types:
  model_status       — {"type": "model_status", "state": "running|idle", "source": "...", "ts": "..."}
  partial_text       — {"type": "partial_text", "content": "...", "ts": "..."}
  tool_call          — {"type": "tool_call", "name": "...", "input": {...}, "ts": "..."}
  tool_done          — {"type": "tool_done", "name": "...", "result_len": 123, "ts": "..."}
  thinking_start     — {"type": "thinking_start", "block_id": "th:...", "ts": "..."}
  thinking_done      — {"type": "thinking_done", "block_id": "th:...", "text": "...", "duration_ms": 1234, "ts": "..."}
  loop_start         — {"type": "loop_start", "ts": "..."}
  loop_end           — {"type": "loop_end", "iterations": 2, "usage": {...}, "ts": "..."}
  task_finished       — {"type": "task_finished", "card": "...", "ts": "..."}
  status             — {"type": "status", "value": "...", "ts": "..."}
  error              — {"type": "error", "content": "...", "ts": "..."}
  system_notice      — {"type": "system_notice", "message": "...", "meta_version": "...", "session_version": "...", "ts": "..."}

Display events derived for the UI:
  user             — from user_input (context); ALSO from task_wakeup (context,
                     v2.0.23) with ``caller=task`` + ``source=task`` so the
                     frontend renders a sky-blue Wakeup card identically live
                     and on reload.
  agent            — from turn last assistant message (context)
  tool             — from turn tool_use blocks (context) OR streaming tool_call (events)
  model_status, partial_text, tool_done, loop_start, loop_end,
  task_finished, status, error — pass-through (events)
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator


# ── Display event converters ──────────────────────────────────────────────────

def _context_entry_sort_ts(entry: dict) -> str:
    """Earliest wall-clock ts that reflects when this entry's content actually
    happened. Used by ``tail_history`` to sort context.jsonl entries so that
    a bg-tool notification (written by ``_drain_background_events`` at the
    instant of completion) can't leapfrog ahead of the turn it interrupted —
    they often share the same write-time top-level ``ts``.

    - user_input / task_wakeup: top-level ``ts`` is the write time, which
      already matches wall-clock (these aren't batched).
    - turn: the write-time ``ts`` is the END of the turn (or the interrupt
      instant for partial saves). Internal blocks carry better data:
        * ``thinking_blocks[].ts``  — stamped at on_thinking_end
        * ``messages[].content[].ts`` — stamped by core/agent.py per commit
      Take the minimum across both lists so the turn sorts at its true
      start. Falls back to top-level ``ts`` when no per-block ts is present
      (older persisted turns).
    """
    top = entry.get("ts") or ""
    if entry.get("type") != "turn":
        return top
    candidates: list[str] = []
    for tb in entry.get("thinking_blocks") or []:
        if isinstance(tb, dict):
            ts = tb.get("ts")
            if isinstance(ts, str) and ts:
                candidates.append(ts)
    for msg in entry.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        msg_ts = msg.get("ts")
        if isinstance(msg_ts, str) and msg_ts:
            candidates.append(msg_ts)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    ts = block.get("ts")
                    if isinstance(ts, str) and ts:
                        candidates.append(ts)
    if not candidates:
        return top
    earliest = min(candidates)
    # If top-level ts predates every internal ts (rare — would require a
    # legacy write-path that stamped the turn before content was serialised)
    # keep it; otherwise the internal earliest wins.
    return min(earliest, top) if top else earliest


def _context_event_to_display(
    event: dict,
    *,
    for_history: bool = False,
    tool_durations: dict[str, int] | None = None,
) -> list[dict]:
    """Convert a context.jsonl event (user_input or turn) to display events.

    Args:
        for_history: When True (history endpoint), always emit tools
                     from turn content — the streaming events
                     in events.jsonl are not available for history replay.
                     When False (SSE live tail), respect has_streaming_tools /
                     pre_triggered flags to avoid duplicating live-streamed items.
        tool_durations: Optional ``tool_use_id → duration_ms`` map built from
                     events.jsonl (see ``tail_history``). Lets a reloaded
                     tool cell show the "✓ bash 2.4s …" duration pill that
                     the live ``tool_done`` event would have populated —
                     events.jsonl isn't otherwise replayed on history fetch.

    Note: agent text-block durations are read directly from
    ``turn["agent_output_durations"]`` (a parallel list populated by the
    session writer). Pairing is per-turn and position-based — pairing
    across the whole events.jsonl was fragile when old turns lacked the
    instrumentation, leading to first-block cells receiving later cells'
    durations on reload.
    """
    etype = event.get("type")
    ts = event.get("ts", "")

    if etype == "task_wakeup":
        # v2.0.23: moved from events.jsonl to context.jsonl so history replay
        # renders the sky-blue "Wakeup" card for completed task runs. Emitted
        # as a ``user`` display event with ``caller=task`` + ``source=task``
        # so the frontend's existing three-variant switch (userCellVariant)
        # picks it up without a dedicated case. ``prompt`` goes into
        # ``content`` so the collapsed-card body renders the task body directly.
        display = {
            "type": "user",
            "content": event.get("prompt", "") or "",
            "ts": ts,
            "caller": "task",
            "source": "task",
        }
        if event.get("card"):
            display["card"] = event["card"]
        return [display]

    if etype == "user_input":
        display = {"type": "user", "content": event.get("content", ""), "ts": ts}
        if not for_history and event.get("id"):
            display["id"] = event["id"]
        # v2.0.23: forward origin fields so the frontend can render three
        # visual variants of the user-input cell (human chat, background-tool
        # notification, task wakeup). ``caller`` ("human"|"system"|"task") and
        # ``source`` ("user"|"panel"|"task") both help disambiguate — the
        # frontend keys on caller first, source as tiebreaker. ``tid`` /
        # ``kind`` / ``tool_name`` are only populated on bg-tool notifications
        # (see Session._drain_background_events); frontend uses tool_name as
        # the dim sub-label next to the "tool output" title.
        for field in (
            "caller",
            "source",
            "tid",
            "kind",
            "tool_name",
            "card",
            # v2.0.23: sub_agent notifications carry the child's display_name
            # (dim sub-label of the "Sub-agent" card) and its permission mode
            # (explorer / executor) — populated only when tool_name == sub_agent.
            "display_name",
            "sub_agent_mode",
        ):
            val = event.get(field)
            if val is not None:
                display[field] = val
        return [display]

    if etype == "turn":
        result: list[dict] = []
        persisted_raw = event.get("thinking_blocks") or []
        # v2.0.19 (parallel): do NOT filter out empty-text blocks — they still
        # carry duration_ms and (post-attributor) reasoning_tokens that the
        # UI renders as "Thought Xs for N tokens". Dropping them here also
        # desynced the position-based pairing below: each ``reasoning`` /
        # ``thinking`` content block pops one queue entry, so a filtered
        # queue leaves the last N reasoning markers with nothing to pop and
        # makes the preceding pops pull the wrong blocks (classic symptom:
        # "first N turns show Thought + tool, rest show tool only").
        persisted = [b for b in persisted_raw if isinstance(b, dict)] \
            if isinstance(persisted_raw, list) else []
        has_persisted = bool(persisted)
        has_streaming_tools = event.get("has_streaming_tools", False)
        has_streaming_thinking = event.get("has_streaming_thinking", False)
        usage = event.get("usage")
        # v2.0.20: per-turn agent output durations — one entry per LLM call
        # that produced text. Paired positionally with text content blocks
        # ENCOUNTERED INSIDE THIS TURN so a missing/extra text block in a
        # neighbouring turn can't shift the mapping (the cross-turn events.jsonl
        # scan we tried earlier had exactly that fragility).
        agent_output_durations_raw = event.get("agent_output_durations") or []
        agent_output_durations = (
            agent_output_durations_raw
            if isinstance(agent_output_durations_raw, list)
            else []
        )
        # v2.0.23: same-positional-order per-call usage snapshots. Overrides
        # the turn-aggregated ``usage`` on whichever text cell it pairs with.
        # Absent on pre-v2.0.23 turns — those fall back to the old behaviour
        # where ``agent_events[-1]["usage"] = turn.usage`` attaches the
        # cumulative total to the last cell.
        agent_output_usages_raw = event.get("agent_output_usages") or []
        agent_output_usages = (
            agent_output_usages_raw
            if isinstance(agent_output_usages_raw, list)
            else []
        )
        # v2.0.23 round-6: per-ITERATION usages (one entry per LLM call,
        # including tool-only / thinking-only calls — agent_output_usages
        # above only had entries for text-producing calls). Paired 1:1 with
        # assistant messages in turn.messages via ``assistant_msg_idx``; the
        # entry is stamped on every event emitted from that message's
        # content blocks (thinking + tool + agent) so the frontend can
        # render a dim token footer inside every cell body. When both
        # lists are present, per_iteration_usages wins.
        per_iteration_usages_raw = event.get("per_iteration_usages") or []
        per_iteration_usages = (
            per_iteration_usages_raw
            if isinstance(per_iteration_usages_raw, list)
            else []
        )
        agent_cursor_turn = 0
        assistant_msg_idx = 0
        messages = event.get("messages", []) or []

        # ── tool_use → tool_result pairing for history replay ─────────
        # events.jsonl isn't replayed by get_history, so tool_done (which
        # carries the live result payload) is unavailable on reload. Scan
        # all non-assistant messages for tool_result blocks and build a
        # map keyed by tool_use_id so each tool cell can render its
        # returned output.
        _MAX_RESULT = 8000
        tool_results: dict[str, dict] = {}
        if for_history:
            for msg in messages:
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    use_id = block.get("tool_use_id")
                    if not use_id:
                        continue
                    raw = block.get("content")
                    if isinstance(raw, list):
                        parts = [
                            b.get("text", "") if isinstance(b, dict) else ""
                            for b in raw
                        ]
                        text = "".join(parts)
                    elif isinstance(raw, str):
                        text = raw
                    else:
                        text = ""
                    truncated = len(text) > _MAX_RESULT
                    tool_results[use_id] = {
                        "result": text[:_MAX_RESULT],
                        "result_truncated": truncated,
                        "is_error": bool(block.get("is_error")),
                    }

        # ── Persisted thinking_blocks (v2.0.17+) ──────────────────────
        # Server-captured list of ``{block_id, text, duration_ms, ts}`` dicts
        # from the session's on_thinking_end callback. Interleaved with the
        # tool/text blocks below by timestamp so the history-replay order
        # matches live playback (think → tool → think → tool → text). Pre-
        # v2.0.19 we dumped them all up front, which grouped every "Thought"
        # before every tool cell on reload. Skipped on live SSE — the
        # thinking_start/thinking_done events on events.jsonl already
        # painted those cells.
        pending_thinking: list[dict] = []
        if has_persisted and for_history:
            for i, block in enumerate(persisted):
                block_ts = block.get("ts") or ts
                thinking_ev: dict = {
                    "type": "thinking",
                    "content": block.get("text", ""),
                    "ts": block_ts,
                    "id": f"thinking:{ts}:persisted:{i}",
                }
                if block.get("duration_ms") is not None:
                    thinking_ev["duration_ms"] = block["duration_ms"]
                if block.get("block_id"):
                    thinking_ev["block_id"] = block["block_id"]
                # v2.0.19 (parallel): reasoning_tokens stamped by the attributor
                # so history replay can restore the "Thought Xs for N tokens"
                # label (codex/kimi only; Anthropic never sets this).
                if block.get("reasoning_tokens"):
                    thinking_ev["reasoning_tokens"] = block["reasoning_tokens"]
                # v2.0.20: placeholder seeded by on_thinking_start survives a
                # turn interrupt, so the frontend knows to render this cell
                # as "Thinking interrupted" rather than a normal "Thought".
                if block.get("interrupted"):
                    thinking_ev["interrupted"] = True
                pending_thinking.append(thinking_ev)

        def _emit_next_thinking(iter_usage: dict | None = None) -> None:
            """Emit the next pending thinking block (position-based pairing).

            Persisted thinking_blocks are ordered chronologically by the
            `on_thinking_end` stream, which is 1:1 with provider reasoning
            items. Codex / gpt-5 stream each reasoning item as a
            ``{"type": "reasoning"}`` content block — we surface one
            persisted thinking per reasoning-block position instead of
            comparing timestamps. Tool_use blocks don't carry a per-block
            ts (they fall back to the turn's commit ts, which sorts
            AFTER every reasoning ts), so a ts-compare approach would
            flush every thought before the first tool on reload.

            v2.0.23 round-6: stamps ``iter_usage`` on the emitted event if
            provided so the frontend can render the dim token footer at
            the bottom of the thinking cell body.
            """
            if pending_thinking:
                ev = pending_thinking.pop(0)
                if iter_usage:
                    ev["usage"] = iter_usage
                result.append(ev)

        # ── Tool + text + legacy thinking: iterate in message order ──
        # This preserves interleaved-mode sequencing (think → tool → text →
        # tool → text) that kimi / codex / gpt-5 commonly emit in one agent
        # run. Old code collected all tool events, then all thinking, then
        # only the LAST assistant text, so intermediate text outputs were
        # silently dropped on history replay.
        agent_events: list[dict] = []  # tracked for usage attachment on last
        thinking_idx = 0
        agent_idx = 0

        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            msg_ts = msg.get("ts", ts)
            content = msg.get("content", [])

            # v2.0.23 round-6: this assistant message corresponds to a single
            # ``provider.complete()`` iteration in Agent.run. The usage for
            # that call is ``per_iteration_usages[assistant_msg_idx]`` when
            # present. Every display event emitted from this message's
            # blocks (thinking / tool / agent) gets the same usage stamp
            # so the frontend can render the dim token footer at the
            # bottom of each cell's expanded body.
            iter_usage: dict | None = None
            if assistant_msg_idx < len(per_iteration_usages):
                raw = per_iteration_usages[assistant_msg_idx]
                if isinstance(raw, dict):
                    iter_usage = raw
            # Consumed at function-scope cursor; bump before we might
            # ``continue`` so skipped messages still advance the index.
            assistant_msg_idx += 1

            # Some providers round-trip the assistant message as a bare string
            # (no block structure). Treat it as a single text block.
            if isinstance(content, str):
                if content:
                    ev: dict = {"type": "agent", "content": content, "ts": msg_ts}
                    if not for_history:
                        ev["id"] = f"turn:{ts}:{agent_idx}"
                    # v2.0.23: bare-string assistant bodies also consume the
                    # per-turn agent_output_durations queue, matching the
                    # structured ``btype == "text"`` path below. Codex /
                    # gpt-5.4 tool-driven turns typically serialise the
                    # post-tool text message as a plain string (see
                    # 2026-04-18_17-26-41-e1f9 for repro); without this the
                    # "Agent Xs" pill only appeared on history replay when
                    # the provider happened to emit a full content-list
                    # shape, which was the pre-regression default for the
                    # turns used to validate v2.0.20/21.
                    if agent_cursor_turn < len(agent_output_durations):
                        dur = agent_output_durations[agent_cursor_turn]
                        if isinstance(dur, int):
                            ev["duration_ms"] = dur
                        if agent_cursor_turn < len(agent_output_usages):
                            u = agent_output_usages[agent_cursor_turn]
                            if isinstance(u, dict):
                                ev["usage"] = u
                        agent_cursor_turn += 1
                    # per_iteration_usages wins over agent_output_usages
                    # (both paths exist for back-compat; this one covers
                    # iterations that produced text but whose entry was
                    # somehow absent from agent_output_usages, and also
                    # matches the tool/thinking footer semantics).
                    if iter_usage is not None:
                        ev["usage"] = iter_usage
                    agent_idx += 1
                    agent_events.append(ev)
                    result.append(ev)
                continue

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "thinking":
                    # Persisted thinking_blocks take precedence when present
                    # (providers like Anthropic store both an inline block
                    # AND the persisted entry). Use the inline block as a
                    # position marker so the persisted text lands at the
                    # right spot in the transcript.
                    if has_persisted:
                        if for_history:
                            _emit_next_thinking(iter_usage)
                        continue
                    if not for_history and has_streaming_thinking:
                        continue
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        ev = {
                            "type": "thinking",
                            "content": thinking_text,
                            "ts": ts,
                            "id": f"thinking:{ts}:{thinking_idx}",
                        }
                        if iter_usage is not None:
                            ev["usage"] = iter_usage
                        thinking_idx += 1
                        result.append(ev)

                elif btype == "reasoning":
                    # Codex / gpt-5 position marker for a reasoning item.
                    # Its human-readable text lives in the persisted
                    # thinking_blocks list; surface one per marker at
                    # the correct position so the transcript reads as
                    # reasoning → tool → reasoning → tool instead of
                    # all reasoning bunched up before any tool.
                    if for_history and has_persisted:
                        _emit_next_thinking(iter_usage)

                elif btype == "tool_use":
                    if for_history or not has_streaming_tools:
                        block_ts = block.get("ts", msg_ts)
                        use_id = block.get("id")
                        tool_ev: dict = {
                            "type": "tool",
                            "name": block["name"],
                            "input": block.get("input", {}),
                            "ts": block_ts,
                        }
                        if use_id:
                            tool_ev["id"] = use_id
                        if iter_usage is not None:
                            tool_ev["usage"] = iter_usage
                        # Pair with tool_result from a later message so the
                        # reloaded cell shows the returned output instead of
                        # "(pending)". Live sessions still overlay the live
                        # tool_done result on top of this baseline.
                        if for_history and use_id and use_id in tool_results:
                            tr = tool_results[use_id]
                            tool_ev["result"] = tr["result"]
                            tool_ev["result_len"] = len(tr["result"])
                            if tr["result_truncated"]:
                                tool_ev["result_truncated"] = True
                            if tr["is_error"]:
                                tool_ev["is_error"] = True
                        if (
                            for_history
                            and use_id
                            and tool_durations is not None
                            and use_id in tool_durations
                        ):
                            tool_ev["duration_ms"] = tool_durations[use_id]
                        result.append(tool_ev)

                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        ev = {"type": "agent", "content": text, "ts": msg_ts}
                        if not for_history:
                            ev["id"] = f"turn:{ts}:{agent_idx}"
                        # v2.0.20: per-turn pairing — pull the next unused
                        # duration from this turn's agent_output_durations
                        # list. Surplus text blocks (rare provider quirk
                        # where one LLM call emits multiple text blocks)
                        # render without a duration pill. Applied to both
                        # live SSE and history so reloaded cells show the
                        # exact same "Agent Xs" label the live cell did.
                        # v2.0.23: same-index consumption of agent_output_usages
                        # so this specific text block's cell shows its own
                        # LLM call's tokens, not the turn-cumulative sum.
                        if agent_cursor_turn < len(agent_output_durations):
                            dur = agent_output_durations[agent_cursor_turn]
                            if isinstance(dur, int):
                                ev["duration_ms"] = dur
                            if agent_cursor_turn < len(agent_output_usages):
                                u = agent_output_usages[agent_cursor_turn]
                                if isinstance(u, dict):
                                    ev["usage"] = u
                            agent_cursor_turn += 1
                        # per_iteration_usages (round-6) wins over agent_output_usages.
                        if iter_usage is not None:
                            ev["usage"] = iter_usage
                        agent_idx += 1
                        agent_events.append(ev)
                        result.append(ev)

        # Any persisted thinking whose ts is later than every content block
        # (or whose ts is blank and thus never flushed) lands at the tail.
        if pending_thinking:
            result.extend(pending_thinking)
            pending_thinking.clear()

        # ── Usage attaches to LAST agent event (fallback only) ─────────
        # v2.0.23: per-call pairing via ``agent_output_usages`` above is
        # authoritative — each text cell already carries the tokens burnt
        # by the LLM call that produced it. Only attach the turn-cumulative
        # ``usage`` on the final cell when the per-call list was absent
        # (pre-v2.0.23 turns on disk) and no cell already got a usage via
        # pairing. This kills the 5-iter cache_read inflation: a tool-heavy
        # turn with 1 text cell no longer shows 5× the actual cached
        # prefix on its pill.
        if usage and agent_events and not any("usage" in a for a in agent_events):
            agent_events[-1]["usage"] = usage

        # ── Live SSE: emit only the LAST agent event ─────────────────
        # Intermediate text blocks in an interleaved turn (think → tool →
        # text → tool → text → text_final) are already rendered live by
        # the frontend via partial_text streaming plus the
        # ``finalize-on-tool_call`` boundary — each iteration's text
        # becomes its own permanent cell without any turn-derived event.
        # Emitting those intermediate agent events here duplicates the
        # cells (the frontend can't tell "this agent event matches a
        # cell I already finalized" from "this is a fresh history
        # replay"). For history replay we keep all N events so the
        # iteration-ordered transcript renders correctly on re-entry.
        if not for_history and len(agent_events) > 1:
            last_agent = agent_events[-1]
            result = [
                r for r in result
                if r.get("type") != "agent" or r is last_agent
            ]

        return result

    # Anything else (old-format context.jsonl with mixed events) — silently ignored.
    return []


def _runtime_event_to_display(event: dict) -> list[dict]:
    """Convert an events.jsonl event to display events.

    All runtime events pass through or are transformed for the SSE stream.
    """
    etype = event.get("type")
    ts = event.get("ts", "")

    if etype == "partial_text":
        return [{"type": "partial_text", "content": event.get("content", ""), "ts": ts}]

    if etype == "tool_call":
        out: dict = {"type": "tool", "name": event.get("name"), "input": event.get("input", {}), "ts": ts}
        # v2.0.23 round-7: forward ``tool_use_id`` as ``id`` (matching the
        # history-replay shape at _context_event_to_display) so the frontend
        # can key the DOM by it. Required for the iteration_usage live-footer
        # signal to target the right cell when two calls of the same tool run
        # concurrently via ``asyncio.gather`` in one iteration.
        tuid = event.get("tool_use_id")
        if tuid:
            out["id"] = tuid
        return [out]

    if etype in (
        "model_status",
        "tool_done",
        "thinking_start",
        "thinking_done",
        "loop_start",
        "loop_end",
        "task_wakeup",
        "task_finished",
        "status",
        "error",
        "system_notice",
        # Sub-agent / background-tool UI events. Frontend keys these by
        # ``tid`` to keep the placeholder tool cell yellow until the actual
        # work finishes (see ui/web/frontend/src/components/chat.ts).
        "tool_progress",
        "tool_finalize",
        "sub_agent_count",
        "panel_update",
        # v2.0.19: per-LLM-call usage powering the HUD's real-token context-%
        # and realtime toks/s. Emitted by Session after each provider.complete()
        # returns. See Session._make_llm_call_end_callback for the payload shape.
        "llm_call_usage",
        # v2.0.19: late-binding reasoning_tokens on a thinking cell. Emitted
        # at LLM call end for providers that expose reasoning_tokens in usage
        # (codex, Kimi); the frontend flips the cell label from
        # "Thought Xs" to "Thought Xs for N tokens" when it arrives.
        # Anthropic never emits this event (reasoning_tokens is 0 there).
        "thinking_tokens_update",
        # v2.0.20: first-text-chunk signal. Emitted by Session.on_chunk the
        # moment the LLM starts producing text so the frontend can open the
        # "Typing…" cell immediately — without waiting for the 150-char
        # partial_text flush to arrive.
        "agent_output_start",
        # v2.0.20: per-LLM-call text-output duration. Emitted by on_llm_call_end
        # whenever a call produced text. The live client stamps the running
        # cell with ``duration_ms`` so the finalized "Agent Xs" matches what
        # history replay reads from events.jsonl.
        "agent_output_done",
        # v2.0.23 round-7: per-LLM-call usage for LIVE footer stamping. Carries
        # ``usage`` + ``tool_use_ids`` + ``has_text`` so the frontend can append
        # the dim ↑/⛀/↓ footer to each running tool cell (matched by id) and
        # the streaming agent cell — without it, the footer only appears on
        # reload via ``per_iteration_usages`` positional pairing.
        "iteration_usage",
    ):
        return [event]

    return []


# ── FileIPC ───────────────────────────────────────────────────────────────────

class FileIPC:
    """File-based IPC for a single session.

    Two files:
        context.jsonl — conversation history only (user_input, turn)
        events.jsonl  — runtime/UI events (model_status, partial_text, etc.)
    """

    def __init__(self, system_dir: Path) -> None:
        self.system_dir = system_dir
        self.context_path = system_dir / "context.jsonl"
        self.events_path = system_dir / "events.jsonl"

    # ── Write ────────────────────────────────────────────────────────────────

    def append_context(self, event: dict) -> None:
        """Append a conversation event (user_input or turn) to context.jsonl."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.context_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def append_event(self, event: dict) -> None:
        """Append a runtime/UI event to events.jsonl."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def send_message(self, content: str, msg_id: str | None = None) -> str:
        """Append a user_input event to context.jsonl. Returns message id."""
        msg_id = msg_id or str(uuid.uuid4())
        self.append_context({"type": "user_input", "content": content, "id": msg_id})
        return msg_id

    def send_interrupt(self) -> None:
        """Append an interrupt control event to events.jsonl.

        The session's run_daemon_loop polls for this via poll_interrupt() and
        responds with a soft interrupt: drains pending inputs and skips the
        next task tick.
        """
        self.append_event({"type": "interrupt"})

    # ── Daemon-side read ─────────────────────────────────────────────────────

    def poll_interrupt(self, offset: int) -> tuple[bool, int]:
        """Read events.jsonl for an 'interrupt' event at or after offset.

        Returns (found, new_offset). The daemon calls this each cycle; when
        found=True it should drain pending inputs, skip the next task tick,
        and emit {"type": "interrupted"} back to events.jsonl.
        """
        if not self.events_path.exists():
            return False, offset
        found = False
        with self.events_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        for line in data.splitlines():
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("type") == "interrupt":
                        found = True
                except json.JSONDecodeError:
                    pass
        return found, new_offset

    def poll_inputs(self, offset: int) -> tuple[list[dict], int]:
        """Read new user_input events from context.jsonl starting at byte offset.

        Returns (user_input_events, new_offset).
        """
        if not self.context_path.exists():
            return [], offset
        with self.context_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        events: list[dict] = []
        for line in data.splitlines():
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("type") == "user_input":
                        events.append(event)
                except json.JSONDecodeError:
                    pass
        return events, new_offset

    # ── UI-side read ─────────────────────────────────────────────────────────

    def _readline_loop(
        self, path: Path, offset: int, converter
    ) -> Iterator[tuple[dict, int]]:
        """Shared readline loop: yield (display_event, line_end_offset) from path."""
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    for display in converter(event):
                        yield display, line_end
                except json.JSONDecodeError:
                    pass

    def tail_history(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from context.jsonl for the history endpoint.

        Always emits tools from turn content (for_history=True). events.jsonl
        is otherwise skipped on history replay, but we take a single pass over
        it up front to recover the ``tool_use_id → duration_ms`` map so the
        reloaded "✓ bash 2.4s …" pill matches what the live tool cell showed.
        Agent output durations are persisted directly on the turn event (see
        ``turn["agent_output_durations"]`` in Session), so no corresponding
        scan is needed.

        v2.0.24: context.jsonl is written by two async producers — the chat
        consumer task (turn entries) and the outer daemon loop
        (_drain_background_events, which appends ``user_input`` notifications
        for background-tool completions). A bg task that completes WHILE the
        parent turn is still finalising races the turn write: the notification
        lands in the file BEFORE the turn entry, producing
        ``user[1], user[bg], turn[1]`` file order even though wall-clock was
        ``turn[1]-end → bg-notif``. The live SSE stream doesn't hit this
        because each event is rendered in its own right via
        tail_runtime_events; only history replay (context-only) sees the
        ordering anomaly. Sort by ``ts`` (stable — same-ts entries keep file
        order) so the reloaded view matches what the user watched live.
        """
        tool_durations = self._scan_tool_durations()
        # Read the whole file first so we can sort by ts. Context files are
        # bounded by the session's turn count and tend to stay in the tens of
        # KB range even for long sessions; buffering them is cheap and spares
        # us a second pass.
        raw_events: list[tuple[dict, int]] = []
        max_line_end = offset
        for raw, line_end in self._read_context_lines(offset):
            raw_events.append((raw, line_end))
            if line_end > max_line_end:
                max_line_end = line_end
        # Sort by the earliest REAL content timestamp inside each entry, not
        # the write-time top-level ``ts``. A turn interrupted by a bg-tool
        # notification gets persisted AT THE INTERRUPT INSTANT — same ts as
        # the notification itself — so top-level ts is ambiguous and stable
        # sort preserves the buggy file order. Pulling from
        # ``thinking_blocks[].ts`` (wall-clock at thinking_end) and message
        # content block ``ts`` (wall-clock at commit, stamped by
        # core/agent.py) gives the turn's actual start moment, which lands
        # BEFORE any bg notification that preempted it.
        raw_events.sort(key=lambda re: _context_entry_sort_ts(re[0]))
        # Yield ``max_line_end`` for every display, NOT the per-entry
        # ``line_end``. After sorting, the last-yielded entry is the one
        # with the latest ts — not the one at the largest byte offset —
        # and ``get_history`` consumes the final yielded offset as the
        # cursor. Without this, a bg-notif that physically lives past an
        # interrupted turn but sorts before it would leave the cursor
        # mid-file; the next SSE reconnect's ``context_since`` seeks there
        # and re-emits the already-displayed turn. Clamping to the
        # max-observed end guarantees the cursor sits at the EOF seen by
        # this read, so every physical line was covered.
        for raw, _unused_line_end in raw_events:
            for display in _context_event_to_display(
                raw,
                for_history=True,
                tool_durations=tool_durations,
            ):
                yield display, max_line_end

    def _read_context_lines(self, offset: int) -> Iterator[tuple[dict, int]]:
        """Iterate context.jsonl from ``offset``; yield (parsed_event, line_end).

        Skips malformed lines silently — matches the tolerance of
        ``_readline_loop`` which the live-tail path uses.
        """
        if not self.context_path.exists():
            return
        with self.context_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                yield event, line_end

    def _scan_tool_durations(self) -> dict[str, int]:
        """Build ``tool_use_id → duration_ms`` from events.jsonl tool_done lines.

        Cheap single-pass scan; events.jsonl is an append-only log and
        tool events are small. Missing/malformed lines are skipped
        silently so a corrupt tail doesn't break history replay.
        """
        durations: dict[str, int] = {}
        if not self.events_path.exists():
            return durations
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "tool_done":
                    continue
                use_id = ev.get("tool_use_id")
                dur = ev.get("duration_ms")
                if isinstance(use_id, str) and isinstance(dur, int):
                    durations[use_id] = dur
        return durations

    def tail_context(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from context.jsonl for the live SSE stream.

        Respects has_streaming_tools / pre_triggered flags to avoid duplicating
        items already delivered via the events.jsonl stream.
        """
        yield from self._readline_loop(
            self.context_path, offset,
            lambda e: _context_event_to_display(e, for_history=False),
        )

    def tail_runtime_events(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from events.jsonl for the live SSE stream."""
        yield from self._readline_loop(
            self.events_path, offset, _runtime_event_to_display,
        )

    def context_size(self) -> int:
        """Current context.jsonl size in bytes (used to initialize poll_inputs offset)."""
        if not self.context_path.exists():
            return 0
        return self.context_path.stat().st_size

    def events_size(self) -> int:
        """Current events.jsonl size in bytes (used to initialize SSE events offset)."""
        if not self.events_path.exists():
            return 0
        return self.events_path.stat().st_size

    def last_running_event_offset(self) -> int:
        """Byte offset of the last model_status:running line in events.jsonl.

        Used by the history endpoint when the session is actively running:
        returning this offset as events_since lets the SSE stream replay the
        in-progress turn (model_status:running + partial_text chunks) so a
        re-attaching client immediately sees the streaming state.

        Returns events_size() if:
        - No running event is found (safe default), OR
        - A model_status:idle event follows the last running event (turn already
          completed — replaying would cause duplicate tool events in the UI).
        """
        if not self.events_path.exists():
            return 0
        last_offset = -1
        has_idle_after = False
        with self.events_path.open("r", encoding="utf-8") as f:
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                    if ev.get("type") == "model_status":
                        if ev.get("state") == "running":
                            last_offset = line_start
                            has_idle_after = False  # reset on each new running event
                        elif ev.get("state") == "idle" and last_offset >= 0:
                            has_idle_after = True
                except Exception:
                    pass
        if last_offset < 0 or has_idle_after:
            # No running event, or turn already completed — no replay needed
            return self.events_size()
        return last_offset
