"""Rename sweep — pin that PR #52's ``sub_agent`` → ``subagent_new`` tool-name
rename is complete.

The rename is a pure literal-string swap: every runtime check that used to
compare ``tool_name == "sub_agent"`` must now compare against
``"subagent_new"``. If a future commit re-introduces the OLD literal in a
runtime path, the new child session's notifications, HUD count, or panel
classification would silently misroute.

What remains legitimate:
  - The panel TYPE constant ``TYPE_SUB_AGENT = "sub_agent"`` in panel.py —
    intentionally unchanged so already-persisted panel entries still render.
  - Prose in comments/docstrings that quote the old name.
  - The directory rename itself (``toolhub/sub_agent/`` → ``toolhub/subagent_new/``).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _iter_production_py() -> list[Path]:
    """Yield every non-test, non-cache .py file under the scannable dirs."""
    roots = [_REPO_ROOT / "butterfly", _REPO_ROOT / "toolhub", _REPO_ROOT / "ui"]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            files.append(path)
    return files


def test_no_stale_sub_agent_tool_name_in_runtime_code() -> None:
    """The literal ``"sub_agent"`` in production .py may only appear in:
      (a) the ``TYPE_SUB_AGENT = "sub_agent"`` constant definition
      (b) lines that are pure comments (leading `#` after strip)

    Any other occurrence is a runtime equality/branch that PR #52 was
    supposed to migrate to ``"subagent_new"``.
    """
    offenders: list[str] = []
    for path in _iter_production_py():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if '"sub_agent"' not in line:
                continue
            stripped = line.strip()
            is_constant_def = stripped.startswith("TYPE_SUB_AGENT")
            is_comment_only = stripped.startswith("#")
            if is_constant_def or is_comment_only:
                continue
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)}:{lineno}: {stripped}"
            )

    assert not offenders, (
        "Found runtime uses of the OLD tool-name literal \"sub_agent\" — "
        "after PR #52 this must be \"subagent_new\":\n  "
        + "\n  ".join(offenders)
    )


def test_toolhub_sub_agent_directory_renamed() -> None:
    """The old ``toolhub/sub_agent/`` dir was renamed to ``toolhub/subagent_new/``.

    Leaving the old path around would make ``session_init::_TOOLHUB_DIR /
    "sub_agent" / mode.md`` lookups (or similar) resolve to stale files
    alongside the new canonical path.
    """
    old = _REPO_ROOT / "toolhub" / "sub_agent"
    new = _REPO_ROOT / "toolhub" / "subagent_new"
    assert not old.exists(), f"Stale `toolhub/sub_agent/` still present at {old}"
    assert new.is_dir(), f"Renamed `toolhub/subagent_new/` missing at {new}"
    assert (new / "tool.json").exists(), "toolhub/subagent_new/tool.json missing"


def test_new_subagent_tools_are_discoverable() -> None:
    """subagent_new / subagent_list / subagent_resume must all ship with a
    tool.json so the ToolLoader can pick them up at runtime.
    """
    for name in ("subagent_new", "subagent_list", "subagent_resume"):
        tool_json = _REPO_ROOT / "toolhub" / name / "tool.json"
        assert tool_json.exists(), f"{tool_json} missing"
