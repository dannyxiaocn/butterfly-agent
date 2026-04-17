"""Verify the test directory layout mirrors the source code structure."""
from __future__ import annotations

from pathlib import Path

from conftest import REPO_ROOT

TESTS_DIR = REPO_ROOT / "tests"

# Expected test directories that mirror the source tree.
EXPECTED_DIRS = [
    "butterfly/core",
    "butterfly/llm_engine",
    "butterfly/runtime",
    "butterfly/service",
    "butterfly/session_engine",
    "butterfly/skill_engine",
    "butterfly/tool_engine",
    "agent",
    "ui/cli",
    "ui/web",
    "integration",
]


def test_expected_test_directories_exist():
    """Every expected component directory exists under tests/."""
    missing = []
    for d in EXPECTED_DIRS:
        if not (TESTS_DIR / d).is_dir():
            missing.append(d)
    assert not missing, f"Missing test directories: {missing}"


def test_expected_test_directories_contain_tests():
    """Each component directory contains at least one test_*.py file."""
    empty = []
    for d in EXPECTED_DIRS:
        test_files = list((TESTS_DIR / d).glob("test_*.py"))
        if not test_files:
            empty.append(d)
    assert not empty, f"Test directories with no test files: {empty}"


def test_no_stale_porter_system_directory():
    """The legacy tests/porter_system/ directory must not exist."""
    assert not (TESTS_DIR / "porter_system").exists(), \
        "tests/porter_system/ should have been removed during migration"
