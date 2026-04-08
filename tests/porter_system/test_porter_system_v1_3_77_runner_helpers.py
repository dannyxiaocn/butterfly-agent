from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from porter_test_support import (
    PORTER_TEST_VERSION,
    PORTER_TESTS_ROOT,
    build_pytest_command,
    iter_porter_test_files,
    porter_components,
    repo_root_from,
    run_porter_suite,
)


class RunnerHelperTests(unittest.TestCase):
    def test_repo_root_from_finds_project_root(self) -> None:
        root = repo_root_from(Path(__file__))
        self.assertTrue((root / "pyproject.toml").exists())

    def test_build_pytest_command_targets_component_files(self) -> None:
        command = build_pytest_command(component="session_engine", verbosity=0)
        self.assertEqual(command[:3], [command[0], "-m", "pytest"])
        self.assertTrue(any("test_session_engine_" in arg for arg in command[3:]))
        self.assertIn("-q", command)

    def test_iter_porter_test_files_filters_by_component(self) -> None:
        files = {path.name for path in iter_porter_test_files("session_engine")}
        self.assertIn(f"test_session_engine_{PORTER_TEST_VERSION}_session_engine.py", files)
        self.assertIn(f"test_session_engine_{PORTER_TEST_VERSION}_task_cards.py", files)
        self.assertFalse(any(name.startswith("test_runtime_") for name in files))

    def test_porter_components_reports_major_suites(self) -> None:
        components = porter_components()
        self.assertIn("session_engine", components)
        self.assertIn("tool_engine", components)
        self.assertIn("porter_system", components)

    def test_porter_suite_root_exists(self) -> None:
        self.assertTrue(PORTER_TESTS_ROOT.exists())

    def test_pytest_supports_non_project_temp_dir(self) -> None:
        with TemporaryDirectory() as td:
            test_dir = Path(td) / "ad_hoc_tests"
            test_dir.mkdir()
            (test_dir / "test_smoke.py").write_text(
                "def test_ok():\n"
                "    assert True\n",
                encoding="utf-8",
            )
            root = repo_root_from(Path(__file__))
            command = [sys.executable, "-m", "pytest", str(test_dir), "-q"]
            status = subprocess.run(command, cwd=root).returncode

        self.assertEqual(status, 0)

    def test_run_porter_suite_executes_component_subset(self) -> None:
        status = run_porter_suite(component="session_engine", verbosity=0)
        self.assertEqual(status, 0)
