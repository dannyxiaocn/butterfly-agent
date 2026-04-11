"""Tests for the reload_capabilities built-in tool."""
import pytest
from nutshell.tool_engine.reload import create_reload_tool
from nutshell.core.tool import Tool


class _Named:
    def __init__(self, name: str):
        self.name = name


class _MockSession:
    def __init__(self):
        self.reload_count = 0
        self._agent = type("AgentStub", (), {
            "tools": [_Named("bash"), _Named("reload_capabilities"), _Named("skill"), _Named("web_search")],
            "skills": [_Named("creator-mode"), _Named("coding-style")],
        })()

    def _load_session_capabilities(self) -> None:
        self.reload_count += 1


def test_create_reload_tool_returns_tool():
    session = _MockSession()
    t = create_reload_tool(session)
    assert isinstance(t, Tool)
    assert t.name == "reload_capabilities"


def test_reload_tool_schema():
    session = _MockSession()
    t = create_reload_tool(session)
    assert t.schema["type"] == "object"
    assert t.schema["properties"] == {}
    assert t.schema["required"] == []


@pytest.mark.asyncio
async def test_reload_tool_calls_load_capabilities():
    session = _MockSession()
    t = create_reload_tool(session)
    result = await t.execute()
    assert session.reload_count == 1
    assert "reloaded" in result.lower()
    assert "Tools (4): bash, reload_capabilities, skill, web_search" in result
    assert "Skills (2): creator-mode, coding-style" in result


@pytest.mark.asyncio
async def test_reload_tool_reports_none_when_empty():
    session = _MockSession()
    session._agent.tools = []
    session._agent.skills = []
    t = create_reload_tool(session)
    result = await t.execute()
    assert "Tools (0): none" in result
    assert "Skills (0): none" in result


@pytest.mark.asyncio
async def test_reload_tool_multiple_calls():
    session = _MockSession()
    t = create_reload_tool(session)
    await t.execute()
    await t.execute()
    assert session.reload_count == 2
