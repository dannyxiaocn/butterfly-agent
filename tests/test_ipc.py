import json
import asyncio

import pytest

from nutshell.abstract.provider import Provider
from nutshell.core.agent import Agent
from nutshell.runtime.ipc import FileIPC, _to_display_events
from nutshell.runtime.session import Session
from nutshell.runtime.status import read_session_status


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None):
        return next(self._responses)


def _write_manifest(session_dir):
    (session_dir / "manifest.json").write_text(
        json.dumps({"session_id": session_dir.name}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_to_display_events_expands_turn_and_passes_model_status():
    turn = {
        "type": "turn",
        "triggered_by": "heartbeat",
        "ts": "2026-03-11T12:00:00",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "1", "name": "bash", "input": {"cmd": "ls"}},
                    {"type": "text", "text": "# Title\n\nbody"},
                ],
            }
        ],
    }

    events = _to_display_events(turn)

    assert events == [
        {"type": "heartbeat_trigger", "ts": "2026-03-11T12:00:00"},
        {"type": "tool", "name": "bash", "input": {"cmd": "ls"}, "ts": "2026-03-11T12:00:00"},
        {
            "type": "agent",
            "content": "# Title\n\nbody",
            "ts": "2026-03-11T12:00:00",
            "triggered_by": "heartbeat",
        },
    ]

    model_status = {"type": "model_status", "state": "running", "source": "user", "ts": "2026-03-11T12:00:01"}
    assert _to_display_events(model_status) == [model_status]


@pytest.mark.asyncio
async def test_session_chat_writes_model_status_around_turn(tmp_path):
    provider = MockProvider([("**done**", [])])
    agent = Agent(provider=provider)
    session = Session(agent=agent, session_id="demo", base_dir=tmp_path)
    _write_manifest(session.session_dir)
    ipc = FileIPC(session.session_dir)
    session._ipc = ipc

    await session.chat("hello")

    events = [
        json.loads(line)
        for line in (session.session_dir / "context.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert [event["type"] for event in events] == ["model_status", "turn", "model_status"]
    assert events[0]["state"] == "running"
    assert events[0]["source"] == "user"
    assert events[2]["state"] == "idle"
    assert events[2]["source"] == "user"
    status = read_session_status(session.session_dir)
    assert status["model_state"] == "idle"
    assert status["model_source"] == "user"


@pytest.mark.asyncio
async def test_session_chat_writes_idle_on_cancellation(tmp_path):
    class CancellingProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None):
            raise asyncio.CancelledError()

    agent = Agent(provider=CancellingProvider())
    session = Session(agent=agent, session_id="demo", base_dir=tmp_path)
    _write_manifest(session.session_dir)
    ipc = FileIPC(session.session_dir)
    session._ipc = ipc

    with pytest.raises(asyncio.CancelledError):
        await session.chat("hello")

    events = [
        json.loads(line)
        for line in (session.session_dir / "context.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert [event["type"] for event in events] == ["model_status", "model_status"]
    assert events[0]["state"] == "running"
    assert events[1]["state"] == "idle"
    status = read_session_status(session.session_dir)
    assert status["model_state"] == "idle"
