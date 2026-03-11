"""Tests for the built-in bash tool."""
import pytest
from nutshell.runtime.tools.bash import create_bash_tool
from nutshell.core.tool import Tool


def test_create_bash_tool_returns_tool():
    t = create_bash_tool()
    assert isinstance(t, Tool)
    assert t.name == "bash"


def test_bash_tool_schema():
    t = create_bash_tool()
    props = t.schema["properties"]
    assert "command" in props
    assert "timeout" in props
    assert "workdir" in props
    assert "pty" in props
    assert t.schema["required"] == ["command"]


@pytest.mark.asyncio
async def test_basic_echo():
    t = create_bash_tool()
    result = await t.execute(command="echo hello")
    assert "hello" in result
    assert "[exit 0]" in result


@pytest.mark.asyncio
async def test_stderr_merged():
    t = create_bash_tool()
    result = await t.execute(command="echo err >&2")
    assert "err" in result
    assert "[exit 0]" in result


@pytest.mark.asyncio
async def test_nonzero_exit_code():
    t = create_bash_tool()
    result = await t.execute(command="exit 7")
    assert "[exit 7]" in result


@pytest.mark.asyncio
async def test_timeout():
    t = create_bash_tool()
    result = await t.execute(command="sleep 60", timeout=0.3)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_workdir(tmp_path):
    t = create_bash_tool()
    result = await t.execute(command="pwd", workdir=str(tmp_path))
    assert str(tmp_path) in result


@pytest.mark.asyncio
async def test_output_truncation():
    t = create_bash_tool(max_output=50)
    result = await t.execute(command="python3 -c \"print('x' * 200)\"")
    assert "truncated" in result


@pytest.mark.asyncio
async def test_factory_default_workdir(tmp_path):
    t = create_bash_tool(workdir=str(tmp_path))
    result = await t.execute(command="pwd")
    assert str(tmp_path) in result


@pytest.mark.asyncio
async def test_pty_basic():
    t = create_bash_tool()
    result = await t.execute(command="echo pty-hello", pty=True)
    if "[pty unavailable" in result:
        pytest.skip("PTY not available in this environment")
    assert "pty-hello" in result
    assert "[exit 0]" in result


@pytest.mark.asyncio
async def test_pty_exit_code():
    t = create_bash_tool()
    result = await t.execute(command="exit 3", pty=True)
    if "[pty unavailable" in result:
        pytest.skip("PTY not available in this environment")
    assert "[exit 3]" in result


@pytest.mark.asyncio
async def test_pty_timeout():
    t = create_bash_tool()
    result = await t.execute(command="sleep 60", timeout=0.3, pty=True)
    if "[pty unavailable" in result:
        pytest.skip("PTY not available in this environment")
    assert "timed out" in result.lower()
