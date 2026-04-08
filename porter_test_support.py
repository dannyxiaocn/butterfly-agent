from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
PORTER_TESTS_ROOT = REPO_ROOT / "tests" / "porter_system"


def repo_root_from(start: Path) -> Path:
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"Could not locate repository root from {start}")


def porter_test_version() -> str:
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return "v" + payload["project"]["version"].replace(".", "_")


PORTER_TEST_VERSION = porter_test_version()


def _verbosity_args(verbosity: int) -> list[str]:
    if verbosity <= 0:
        return ["-q"]
    if verbosity == 1:
        return []
    return ["-" + ("v" * (verbosity - 1))]


def iter_porter_test_files(component: str | None = None) -> list[Path]:
    if component is None:
        pattern = "test_*.py"
    else:
        pattern = f"test_{component}_{PORTER_TEST_VERSION}_*.py"
    return sorted(PORTER_TESTS_ROOT.glob(pattern))


def porter_components() -> set[str]:
    needle = f"_{PORTER_TEST_VERSION}_"
    components: set[str] = set()
    for path in iter_porter_test_files():
        name = path.name.removeprefix("test_")
        if needle not in name:
            continue
        components.add(name.split(needle, 1)[0])
    return components


def build_pytest_command(*, component: str | None = None, verbosity: int = 0) -> list[str]:
    targets = iter_porter_test_files(component)
    if not targets:
        label = component or "porter_system"
        raise ValueError(f"no porter tests found for {label}")
    return [
        sys.executable,
        "-m",
        "pytest",
        *[str(path) for path in targets],
        *_verbosity_args(verbosity),
    ]


def run_porter_suite(*, component: str | None = None, verbosity: int = 0) -> int:
    completed = subprocess.run(build_pytest_command(component=component, verbosity=verbosity), cwd=REPO_ROOT)
    return completed.returncode
