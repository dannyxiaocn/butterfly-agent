"""PR #28 review findings — executable regressions pinning the bugs.

Each test in this file asserts the CURRENT (buggy) behaviour or the
EXPECTED (post-fix) behaviour; the docstring of each test states which.
If these tests go red after a real fix, update the assertions accordingly.

Scope: correctness issues in the v2.0.13 sub_agent feature that the
existing structural tests do not cover.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

from butterfly.session_engine.session_init import init_session
from butterfly.session_engine.session import Session, _parse_background_tid
from butterfly.runtime.ipc import FileIPC
from butterfly.core.agent import Agent
from butterfly.tool_engine.sub_agent import SubAgentRunner


# ── Bug #1 (CRITICAL): initial_message is skipped by the child daemon ──────────


class _NoopProvider:
    """Stand-in so Agent() can be constructed without a real LLM."""
    name = "noop"

    async def complete(self, *args, **kwargs):  # noqa: D401
        from butterfly.core.types import TokenUsage
        return ("", [], TokenUsage())


def _minimal_agent(tmp_path: Path) -> Path:
    base = tmp_path / "agenthub"
    ag = base / "agent"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text(
        "name: agent\nmodel: test\nprovider: noop\n", encoding="utf-8"
    )
    (ag / "system.md").write_text("you are agent", encoding="utf-8")
    (ag / "task.md").write_text("", encoding="utf-8")
    (ag / "env.md").write_text("env", encoding="utf-8")
    (ag / "tools.md").write_text("", encoding="utf-8")
    return base


@pytest.mark.asyncio
async def test_initial_message_reaches_daemon_consumer(tmp_path: Path) -> None:
    """EXPECTED-POST-FIX: initial_message written by ``init_session`` is
    picked up by the child session's daemon and enqueued into the
    dispatcher inbox.

    CURRENT BEHAVIOUR (bug): ``run_daemon_loop`` sets
    ``input_offset = ipc.context_size()`` AFTER ``init_session`` has already
    written the ``user_input`` event, so ``poll_inputs`` starts past that
    offset and the initial message is never delivered. The daemon sits
    idle; ``sub_agent`` waits out its full timeout with no reply.

    This test fails today — it is the right invariant for a correct fix.
    """
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _minimal_agent(tmp_path)

    sid = "probe-child"
    init_session(
        session_id=sid,
        agent_name="agent",
        sessions_base=sessions_base,
        system_sessions_base=sys_base,
        agent_base=agent_base,
        initial_message="HELLO INITIAL",
        initial_message_id="probe-msg-id",
        parent_session_id="probe-parent",
        mode="explorer",
    )

    ctx_path = sys_base / sid / "context.jsonl"
    lines = ctx_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "init_session must persist the initial message"
    first = json.loads(lines[0])
    assert first["type"] == "user_input"
    assert first["content"] == "HELLO INITIAL"
    assert first["id"] == "probe-msg-id"

    # Build a Session exactly the way SessionWatcher does, then run the
    # daemon loop briefly and watch the dispatcher inbox.
    agent = Agent(provider=_NoopProvider())
    session = Session(
        agent, session_id=sid, base_dir=sessions_base, system_base=sys_base,
    )
    session.load_history()

    enqueued: list[str] = []
    orig_enqueue = session._enqueue

    async def recording_enqueue(item):
        enqueued.append(getattr(item, "content", repr(item)))
        # Intentionally DO NOT actually run — we just want to know
        # whether the daemon saw the initial message.

    session._enqueue = recording_enqueue  # type: ignore[assignment]

    stop_event = asyncio.Event()
    ipc = FileIPC(session.system_dir)

    async def run_briefly() -> None:
        try:
            await asyncio.wait_for(
                session.run_daemon_loop(ipc, stop_event=stop_event),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            pass

    task = asyncio.create_task(run_briefly())
    await asyncio.sleep(2.0)
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()

    # Post-fix invariant: the initial message MUST have been enqueued.
    # Today this assertion fails because input_offset skips past it.
    assert any("HELLO INITIAL" in c for c in enqueued), (
        "Bug: child daemon's input_offset is initialized to context_size() "
        "AFTER init_session wrote the initial_message; the child never "
        "processes the delegated task. The sub_agent feature can't "
        "function end-to-end until run_daemon_loop rewinds input_offset "
        "on a fresh session (or init_session defers the user_input write "
        "until after the daemon signals ready)."
    )


# ── Bug #2: _summarize_child_state field mismatch ─────────────────────────────


class _FakeIPC:
    def __init__(self, events_path: Path) -> None:
        self.events_path = events_path


def test_summarize_child_state_matches_session_event_schema(tmp_path: Path) -> None:
    """EXPECTED-POST-FIX: ``SubAgentRunner._summarize_child_state`` returns
    a non-empty summary when the child has been emitting real
    ``tool_call`` / ``model_status`` events in the schema that
    ``Session._make_tool_call_callback`` / ``Session._set_model_status``
    actually write.

    CURRENT BEHAVIOUR (bug): the summariser checks
    ``evt.get("type") == "tool"`` — but the session writes
    ``"tool_call"``. It also reads ``evt["value"]`` / ``evt["status"]``
    for model_status — but the session writes ``evt["state"]``. The
    summariser never produces output, so the sub_agent panel thumbnail
    stays on ``starting…`` and no ``tool_progress`` UI events ever fire.
    """
    ep = tmp_path / "events.jsonl"
    ep.write_text(
        json.dumps({"type": "tool_call", "name": "bash", "input": {"command": "ls"}})
        + "\n"
        + json.dumps({"type": "model_status", "state": "running", "source": "user"})
        + "\n",
        encoding="utf-8",
    )
    summary = SubAgentRunner._summarize_child_state(_FakeIPC(ep))
    assert summary, (
        "Bug: _summarize_child_state checks type='tool' and reads "
        "evt['value']/evt['status'], but Session writes type='tool_call' "
        "with 'name', and type='model_status' with 'state'. The field "
        "mismatch silences every progress tick — last_child_state is "
        "never stamped on the panel entry."
    )
    assert "bash" in summary or "running" in summary


# ── Bug #3: _parse_background_tid false positive ──────────────────────────────


def test_parse_background_tid_rejects_non_placeholder_strings() -> None:
    """EXPECTED-POST-FIX: ``_parse_background_tid`` returns None for any
    result string that isn't the exact background-spawn placeholder
    generated by ``butterfly/core/agent.py::_execute_tools``.

    CURRENT BEHAVIOUR (bug): the regex ``task_id="([^"]+)"`` matches any
    substring. A regular (non-background) bash result that happens to
    contain ``task_id="…"`` (e.g. the agent cat'ing a previous
    ``tool_output`` call) gets flagged ``is_background=True`` with a
    synthetic tid; the frontend then parks the chat cell as yellow and
    waits forever for a ``tool_finalize`` event that will never arrive.
    """
    # A plausible regular bash result — an agent ran ``cat /tmp/foo`` which
    # happened to contain a reference to an earlier background call.
    non_placeholder = (
        "Saw earlier log line: tool_output(task_id=\"stale_tid_123\")\n"
        "[exit 0, duration 0.1s, truncated false]"
    )
    tid = _parse_background_tid(non_placeholder)
    assert tid is None, (
        "Bug: _parse_background_tid should only match the exact "
        "placeholder returned by Agent._execute_tools (prefix "
        "'Task started. task_id='). As written, any tool result that "
        "contains the substring task_id=\"...\" is mis-tagged as "
        "backgrounded, leaving the chat cell permanently yellow."
    )


def test_parse_background_tid_still_matches_real_placeholder() -> None:
    """Sanity: a correct fix must still accept the real placeholder."""
    real = (
        "Task started. task_id=bg_abc123. Output will arrive in a later "
        'turn as a notification; fetch anytime with '
        'tool_output(task_id="bg_abc123"). Task is visible in the session panel.'
    )
    assert _parse_background_tid(real) == "bg_abc123"
