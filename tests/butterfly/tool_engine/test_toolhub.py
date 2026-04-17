"""Tests for toolhub tools: memory_recall, web_search, and ToolLoader toolhub integration.

The unified `manage_task` executor was replaced in v2.0.5 by per-verb toolhub
modules (task_create/task_update/task_list/task_pause/task_resume/task_finish);
coverage lives in tests/butterfly/tool_engine/test_pr19_task_verbs.py.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from butterfly.tool_engine.loader import (
    ToolLoader,
    _load_tool_schema,
    _read_tool_md,
)


# ── memory_recall executor ───────────────────────────────────────────────────


class TestMemoryRecallExecutor:
    @pytest.fixture
    def executor(self, tmp_path):
        from toolhub.memory_recall.executor import MemoryRecallExecutor
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "facts.md").write_text("Important fact 1\nFact 2")
        (memory_dir / "empty.md").write_text("")
        return MemoryRecallExecutor(memory_dir=memory_dir)

    @pytest.mark.asyncio
    async def test_list_layers(self, executor):
        result = await executor.execute()
        assert "facts" in result
        # Empty file should not appear
        assert "empty" not in result

    @pytest.mark.asyncio
    async def test_read_layer(self, executor):
        result = await executor.execute(name="facts")
        assert "Important fact 1" in result
        assert "# Memory: facts" in result

    @pytest.mark.asyncio
    async def test_read_missing_layer(self, executor):
        result = await executor.execute(name="nonexistent")
        assert "not found" in result
        assert "facts" in result  # should suggest available

    @pytest.mark.asyncio
    async def test_read_empty_layer(self, executor):
        result = await executor.execute(name="empty")
        assert "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_no_memory_dir_error(self):
        from toolhub.memory_recall.executor import MemoryRecallExecutor
        executor = MemoryRecallExecutor(memory_dir=None)
        result = await executor.execute()
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_sanitizes_path_traversal(self, executor):
        result = await executor.execute(name="../../../etc/passwd")
        assert "not found" in result

    def test_schema_allows_empty_name_for_listing(self):
        """tool.json must not require 'name' so the LLM can list layers."""
        schema = _load_tool_schema("memory_recall")
        assert schema is not None
        required = schema.get("input_schema", {}).get("required", [])
        assert "name" not in required


# ── ToolLoader toolhub integration ───────────────────────────────────────────


class TestToolLoaderToolhub:
    def test_load_tool_schema_from_toolhub(self):
        """_load_tool_schema loads tool.json from the real toolhub."""
        schema = _load_tool_schema("bash")
        assert schema is not None
        assert schema["name"] == "bash"
        assert "input_schema" in schema

    def test_load_tool_schema_missing(self, tmp_path):
        """_load_tool_schema returns None for missing tool."""
        schema = _load_tool_schema("nonexistent", toolhub_dir=tmp_path)
        assert schema is None

    def test_read_tool_md(self, tmp_path):
        """_read_tool_md reads a tool.md file."""
        tool_md = tmp_path / "tool.md"
        tool_md.write_text("bash\nweb_search\n# comment\n\nskill\n")
        names = _read_tool_md(tool_md)
        assert names == ["bash", "web_search", "skill"]

    def test_read_tool_md_missing(self, tmp_path):
        """_read_tool_md returns empty for missing file."""
        names = _read_tool_md(tmp_path / "nonexistent")
        assert names == []

    def test_load_from_tool_md(self, tmp_path):
        """ToolLoader.load_from_tool_md loads tools listed in tool.md."""
        tool_md = tmp_path / "tool.md"
        tool_md.write_text("bash\n")
        loader = ToolLoader()
        tools = loader.load_from_tool_md(tool_md)
        names = [t.name for t in tools]
        assert "bash" in names

    def test_load_from_toolhub_bash(self):
        """ToolLoader.load_from_toolhub loads bash tool."""
        loader = ToolLoader()
        tool = loader.load_from_toolhub("bash")
        assert tool is not None
        assert tool.name == "bash"

    def test_load_from_toolhub_missing(self, tmp_path):
        """ToolLoader.load_from_toolhub returns None for missing tool."""
        loader = ToolLoader(toolhub_dir=tmp_path)
        tool = loader.load_from_toolhub("nonexistent")
        assert tool is None

    def test_load_local_tools(self, tmp_path):
        """ToolLoader.load_local_tools loads .json+.sh pairs."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        schema = {
            "name": "my_tool",
            "description": "A tool",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }
        (tools_dir / "my_tool.json").write_text(json.dumps(schema))
        sh = tools_dir / "my_tool.sh"
        sh.write_text('#!/bin/bash\necho "ok"')
        sh.chmod(0o755)

        loader = ToolLoader()
        tools = loader.load_local_tools(tools_dir)
        assert len(tools) == 1
        assert tools[0].name == "my_tool"

    def test_load_local_tools_ignores_json_without_sh(self, tmp_path):
        """ToolLoader.load_local_tools skips .json files without matching .sh."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        schema = {"name": "orphan", "description": "No script"}
        (tools_dir / "orphan.json").write_text(json.dumps(schema))

        loader = ToolLoader()
        tools = loader.load_local_tools(tools_dir)
        assert len(tools) == 0

    def test_load_local_tools_empty_dir(self, tmp_path):
        """ToolLoader.load_local_tools returns empty for empty dir."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        loader = ToolLoader()
        assert loader.load_local_tools(tools_dir) == []

    def test_load_local_tools_nonexistent_dir(self, tmp_path):
        """ToolLoader.load_local_tools returns empty for nonexistent dir."""
        loader = ToolLoader()
        assert loader.load_local_tools(tmp_path / "nope") == []
