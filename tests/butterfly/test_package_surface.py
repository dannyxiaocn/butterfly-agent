from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path

import butterfly


from conftest import REPO_ROOT


class PackageSurfaceTest(unittest.TestCase):
    def test_all_exports_are_importable(self) -> None:
        for name in butterfly.__all__:
            self.assertTrue(hasattr(butterfly, name), name)

    def test_subsystem_design_docs_exist(self) -> None:
        # Phase 10 purged impl.md/todo.md. `runtime/` was split into
        # `events.md` + `io.md` so it lives as two docs rather than one
        # design.md; checked separately.
        for name in [
            "core",
            "llm_engine",
            "service",
            "session_engine",
            "skill_engine",
            "tool_engine",
        ]:
            self.assertTrue((REPO_ROOT / "docs" / "butterfly" / name / "design.md").exists(), name)
        for fname in ("events.md", "io.md"):
            self.assertTrue((REPO_ROOT / "docs" / "butterfly" / "runtime" / fname).exists(), fname)

    def test_public_modules_import_cleanly(self) -> None:
        for module_name in [
            "butterfly.core.agent",
            "butterfly.session_engine.session",
            "butterfly.runtime.ipc",
            "butterfly.tool_engine.loader",
        ]:
            self.assertIsNotNone(importlib.import_module(module_name))

    def test_pyproject_exports_runtime_entrypoints(self) -> None:
        # v2.0.16 unified CLI: `butterfly-server` and `butterfly-web` entry
        # points were removed; everything lives under the single `butterfly`
        # entry (server auto-daemonizes, web is in-process).
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["butterfly"], "ui.cli.main:main")
        self.assertNotIn("butterfly-server", scripts)
        self.assertNotIn("butterfly-web", scripts)


if __name__ == "__main__":
    unittest.main()
