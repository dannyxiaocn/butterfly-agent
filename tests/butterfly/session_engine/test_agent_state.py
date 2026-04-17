from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butterfly.session_engine.agent_state import get_meta_dir, sync_from_agent


def _write_agent(agent_root: Path, name: str) -> Path:
    agent_dir = agent_root / name
    (agent_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (agent_dir / "playground").mkdir(exist_ok=True)
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
    (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
    (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
    (agent_dir / "config.yaml").write_text(
        "name: {}\nprompts:\n  system: prompts/system.md\n  task: prompts/task.md\n  env: prompts/env.md\n".format(name),
        encoding="utf-8",
    )
    return agent_dir


class AgentStateUnitTests(unittest.TestCase):
    def test_sync_from_agent_bootstraps_memory_when_absent(self) -> None:
        with TemporaryDirectory() as td, patch(
            "butterfly.session_engine.agent_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            agent_root = root / "agenthub"
            sessions_base = root / "sessions"
            agent = _write_agent(agent_root, "myagent")
            (agent / "memory.md").write_text("agent memory", encoding="utf-8")
            (agent / "memory" / "layer.md").write_text("layer content", encoding="utf-8")

            sync_from_agent("myagent", agent_base=agent_root, s_base=sessions_base)

            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            self.assertEqual((meta_dir / "core" / "memory.md").read_text(encoding="utf-8"), "agent memory")
            self.assertEqual((meta_dir / "core" / "memory" / "layer.md").read_text(encoding="utf-8"), "layer content")

    def test_sync_from_agent_does_not_overwrite_existing_meta_memory(self) -> None:
        with TemporaryDirectory() as td, patch(
            "butterfly.session_engine.agent_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            agent_root = root / "agenthub"
            sessions_base = root / "sessions"
            agent = _write_agent(agent_root, "myagent")
            (agent / "memory.md").write_text("agent memory", encoding="utf-8")

            # First sync — bootstraps
            sync_from_agent("myagent", agent_base=agent_root, s_base=sessions_base)
            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            # Meta memory is now "agent memory"
            (meta_dir / "core" / "memory.md").write_text("meta own memory", encoding="utf-8")

            # Second sync — should NOT overwrite because meta memory is non-empty
            sync_from_agent("myagent", agent_base=agent_root, s_base=sessions_base)
            self.assertEqual((meta_dir / "core" / "memory.md").read_text(encoding="utf-8"), "meta own memory")

    def test_sync_from_agent_bootstraps_playground_files(self) -> None:
        with TemporaryDirectory() as td, patch(
            "butterfly.session_engine.agent_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            agent_root = root / "agenthub"
            sessions_base = root / "sessions"
            agent = _write_agent(agent_root, "myagent")
            (agent / "playground" / "seed.txt").write_text("seed content", encoding="utf-8")

            sync_from_agent("myagent", agent_base=agent_root, s_base=sessions_base)

            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            self.assertEqual((meta_dir / "playground" / "seed.txt").read_text(encoding="utf-8"), "seed content")
