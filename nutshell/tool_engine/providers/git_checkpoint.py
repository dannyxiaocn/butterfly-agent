"""git_checkpoint — built-in tool for agents to commit workspace changes.

Agents working in a git repository (e.g. playground/nutshell/) can call this
tool to stage all changes and create a checkpoint commit. Designed for the
nutshell_dev workflow where the agent works in an isolated playground clone
and wants to persist its progress without needing raw bash git commands.

Usage:
    git_checkpoint(message="feat: implement X", workdir="playground/nutshell")
    # → "Committed abc1234: feat: implement X  (3 files changed, +42 -5)"
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"


async def git_checkpoint(
    *,
    message: str,
    workdir: str = "",
    _sessions_base: Path | None = None,
) -> str:
    """Stage all changes and create a checkpoint commit in a git repository.

    If there is nothing to commit, returns a "(nothing to commit)" message
    without creating an empty commit.

    Args:
        message: Commit message (required — forces the agent to describe intent).
        workdir: Path to the git repository, relative to the session directory
                 (e.g. "playground/nutshell"). Defaults to the session directory
                 itself if empty.

    Returns:
        Commit hash + summary on success, or an error/status string.
    """
    session_id = os.environ.get("NUTSHELL_SESSION_ID", "")
    if not session_id:
        return "Error: no active session (NUTSHELL_SESSION_ID not set)."

    sessions_base = _sessions_base or _DEFAULT_SESSIONS_BASE
    session_dir = sessions_base / session_id

    if workdir:
        cwd = (session_dir / workdir).resolve()
    else:
        cwd = session_dir.resolve()

    if not cwd.exists():
        return f"Error: workdir not found: {cwd}"

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )

    # Verify it's a git repo
    check = _run(["git", "rev-parse", "--git-dir"])
    if check.returncode != 0:
        return f"Error: not a git repository at {cwd}"

    # Stage all changes
    add_result = _run(["git", "add", "-A"])
    if add_result.returncode != 0:
        return f"Error staging changes: {add_result.stderr.strip()}"

    # Check if there's anything staged
    diff_result = _run(["git", "diff", "--cached", "--stat"])
    if not diff_result.stdout.strip():
        return "(nothing to commit: working tree clean)"

    # Commit
    commit_result = _run(["git", "commit", "-m", message])
    if commit_result.returncode != 0:
        return f"Error committing: {commit_result.stderr.strip()}"

    # Extract short hash from output
    hash_result = _run(["git", "rev-parse", "--short", "HEAD"])
    short_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else "?"

    # Build summary from commit output (last line of --stat summary)
    lines = commit_result.stdout.strip().splitlines()
    # Find the "N files changed" summary line
    summary = ""
    for line in lines:
        if "changed" in line:
            summary = f"  ({line.strip()})"
            break

    return f"Committed {short_hash}: {message}{summary}"
