from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path

import nutshell
from porter_test_support import repo_root_from


REPO_ROOT = repo_root_from(Path(__file__))


class PackageSurfaceTest(unittest.TestCase):
    def test_all_exports_are_importable(self) -> None:
        for name in nutshell.__all__:
            self.assertTrue(hasattr(nutshell, name), name)

    def test_subsystem_readmes_exist(self) -> None:
        for name in [
            "core",
            "llm_engine",
            "runtime",
            "session_engine",
            "skill_engine",
            "tool_engine",
        ]:
            self.assertTrue((REPO_ROOT / "nutshell" / name / "README.md").exists(), name)

    def test_readme_lists_all_runtime_subsystems(self) -> None:
        text = (REPO_ROOT / "nutshell" / "README.md").read_text(encoding="utf-8")
        for name in [
            "core/",
            "llm_engine/",
            "tool_engine/",
            "skill_engine/",
            "session_engine/",
            "runtime/",
        ]:
            self.assertIn(name, text)

    def test_public_modules_import_cleanly(self) -> None:
        for module_name in [
            "nutshell.core.agent",
            "nutshell.session_engine.session",
            "nutshell.runtime.ipc",
            "nutshell.tool_engine.loader",
        ]:
            self.assertIsNotNone(importlib.import_module(module_name))

    def test_pyproject_exports_runtime_entrypoints(self) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["nutshell"], "ui.cli.main:main")
        self.assertEqual(scripts["nutshell-server"], "nutshell.runtime.server:main")
        self.assertEqual(scripts["nutshell-web"], "ui.web.app:main")


if __name__ == "__main__":
    unittest.main()
