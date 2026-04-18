"""Central result classifier — decides whether a tool's return string
should be surfaced as an error (red cell) or a success (green cell).

Complements the exception/ tool-not-found / background-spawn-fail paths in
``butterfly.core.agent._execute_tools`` which already stamp ``is_error=True``
on raised-exception outcomes. This module handles the other half: tools that
complete normally but whose string output encodes a failure — the canonical
example is ``bash`` returning with a non-zero ``[exit N, ...]`` footer.

Called once per tool call from ``_execute_tools`` right after ``tool.execute()``
returns. The resulting boolean is threaded through ``on_tool_done`` into the
``tool_done`` event on ``events.jsonl``; the web UI flips the cell accordingly.

Per-tool rules live in ``_RULES``. A generic fallback catches the common
`Traceback (most recent call last):` / leading `Error:` patterns for any
executor we haven't given a tailored rule.
"""
from __future__ import annotations

import re
from typing import Callable


# Matches the structured footer that bash / session_shell append to every
# completed run: e.g. ``[exit 0, duration 0.1s, truncated false]`` or
# ``[exit 127, duration 0.0s, ...]``. We scan the whole buffer and use the
# last match so trailing multi-command output still classifies on the final
# exit code, not an earlier one that appeared in the command's own stdout.
_EXIT_CODE_RE = re.compile(r"\[exit (-?\d+)")

# Lines we treat as an error marker for the generic rule. Checked against
# the first non-empty line of the result — mid-output "Error:" substrings
# are too noisy (a successful run might echo a log line containing that
# word) and would false-positive. Traceback is matched anywhere because
# Python prints it on its own lines with consistent framing.
_ERROR_PREFIXES = ("Error:", "Error ", "ERROR:", "Traceback (most recent call last):")


def _default_rule(result: str) -> bool:
    if "Traceback (most recent call last):" in result:
        return True
    for line in result.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(_ERROR_PREFIXES)
    return False


def _bash_rule(result: str) -> bool:
    # Bash footer is the authoritative signal; fall back to the default
    # rule when the executor short-circuited (timeout, spill failure) and
    # skipped the normal `[exit N, ...]` line.
    if "[timed out after" in result:
        return True
    matches = _EXIT_CODE_RE.findall(result)
    if matches:
        try:
            return int(matches[-1]) != 0
        except ValueError:
            pass
    return _default_rule(result)


# Per-tool rules. Name keys match the names registered in ``toolhub/``.
# Tools without a dedicated rule fall through to ``_default_rule``.
_RULES: dict[str, Callable[[str], bool]] = {
    "bash": _bash_rule,
    # session_shell shares the same [exit N] footer format (see
    # tool_engine/executor/terminal/session_shell.py).
    "session_shell": _bash_rule,
}


def classify_tool_result(tool_name: str, result: str) -> bool:
    """Return ``True`` if ``result`` (as produced by a successful
    ``tool.execute`` return, i.e. no raised exception) encodes an error.

    ``False`` is safe as the default for unknown tools — a mis-classified
    success is a cosmetic (green cell that should be red) issue; a mis-
    classified error would wrongly colour a happy path red. The core/agent
    layer already stamps ``is_error=True`` for the exception path, so the
    worst this can do is let an error slip through as green.
    """
    if not isinstance(result, str) or not result:
        return False
    rule = _RULES.get(tool_name, _default_rule)
    return rule(result)


__all__ = ["classify_tool_result"]
