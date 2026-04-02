import pytest

from nutshell.tool_engine.sandbox import BashSandbox, FSSandbox, ToolSandbox


@pytest.mark.asyncio
async def test_tool_sandbox_check_returns_none():
    sandbox = ToolSandbox()
    assert await sandbox.check("tool", {}) is None


@pytest.mark.asyncio
async def test_bash_sandbox_rejects_rm_rf_root():
    sandbox = BashSandbox()
    result = await sandbox.check("bash", {"command": "rm -rf /"})
    assert result is not None
    assert "blocked" in result.lower()


@pytest.mark.asyncio
async def test_bash_sandbox_allows_ls_la():
    sandbox = BashSandbox()
    result = await sandbox.check("bash", {"command": "ls -la"})
    assert result is None


@pytest.mark.asyncio
async def test_fs_sandbox_truncates_long_string():
    sandbox = FSSandbox(max_chars=10)
    result = await sandbox.filter_result("fetch", "a" * 20)
    assert result == "a" * 10 + "\n... [truncated: 20 chars total]"


@pytest.mark.asyncio
async def test_fs_sandbox_does_not_truncate_normal_string():
    sandbox = FSSandbox(max_chars=10)
    result = await sandbox.filter_result("fetch", "hello")
    assert result == "hello"
