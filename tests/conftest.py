"""Shared pytest configuration and helpers for the nutshell test suite."""
from __future__ import annotations

from pathlib import Path

import pytest

# Single source of truth for the repository root path.
# tests/conftest.py is always at <repo_root>/tests/conftest.py.
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    """Pytest fixture providing the repository root path."""
    return REPO_ROOT
