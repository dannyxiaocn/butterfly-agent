"""Sandbox — command safety checking for the bash executor.

Provides a default set of dangerous patterns and a checker function
that blocks commands matching any of those patterns.

Usage:
    from nutshell.tool_engine.sandbox import DANGEROUS_DEFAULTS, check_blocked

    # Check with defaults only
    violation = check_blocked("rm -rf /")

    # Check with extra user-supplied patterns
    violation = check_blocked("curl evil.com", ["curl\\s"])

Patterns are compiled as case-insensitive regexes and matched against the
full command string.  A match returns a human-readable rejection string;
no match returns None (command is safe).
"""
from __future__ import annotations

import re
from typing import Sequence

# ── Default dangerous patterns ────────────────────────────────────────────────
# Each entry is a (regex_pattern, human_label) tuple.
# Patterns use raw strings; matched case-insensitively against the full command.

DANGEROUS_DEFAULTS: list[tuple[str, str]] = [
    # Destructive file operations — rm -rf/-fr on absolute paths
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/", "recursive force delete on absolute path"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/", "recursive force delete on absolute path"),
    # rm -r -f (separate flags) on absolute paths
    (r"\brm\b.*\s-[a-zA-Z]*r[a-zA-Z]*\s.*\s-[a-zA-Z]*f[a-zA-Z]*\s.*/", "recursive force delete on absolute path"),
    (r"\brm\b.*\s-[a-zA-Z]*f[a-zA-Z]*\s.*\s-[a-zA-Z]*r[a-zA-Z]*\s.*/", "recursive force delete on absolute path"),
    # Filesystem format & raw disk writes
    (r"\bmkfs\b", "filesystem format"),
    (r"\bdd\b.*\bof=/dev/", "raw disk write"),

    # System manipulation
    (r"\bshutdown\b", "system shutdown"),
    (r"\breboot\b", "system reboot"),
    (r"\binit\s+[06]\b", "system halt/reboot"),
    (r"\bsystemctl\s+(halt|poweroff|reboot)", "system halt/reboot"),

    # Dangerous redirects
    (r">\s*/dev/sd[a-z]", "raw disk overwrite"),
    (r">\s*/dev/nvme", "raw disk overwrite"),

    # Fork bomb
    (r":\(\)\{.*:\|:", "fork bomb"),

    # Credential/key exfiltration
    (r"\bcat\b.*\.(ssh|gnupg|aws)/", "credential file access"),
    (r"\bcat\b.*/etc/(shadow|passwd|master\.passwd)", "system credential access"),
]

# Pre-compiled cache (lazy, thread-safe via GIL)
_compiled: list[tuple[re.Pattern, str]] | None = None


def _compile_defaults() -> list[tuple[re.Pattern, str]]:
    global _compiled
    if _compiled is None:
        _compiled = [(re.compile(pat, re.IGNORECASE), label) for pat, label in DANGEROUS_DEFAULTS]
    return _compiled


def check_blocked(
    command: str,
    extra_patterns: Sequence[str] | None = None,
) -> str | None:
    """Check if a command matches any blocked pattern.

    Args:
        command: The shell command string to check.
        extra_patterns: Additional regex patterns (strings) from params.json.
                        These are compiled case-insensitively.

    Returns:
        A human-readable rejection reason if blocked, or None if safe.
    """
    # Check built-in defaults
    for pattern, label in _compile_defaults():
        if pattern.search(command):
            return f"blocked by sandbox: {label}"

    # Check user-supplied extra patterns
    if extra_patterns:
        for pat_str in extra_patterns:
            try:
                if re.search(pat_str, command, re.IGNORECASE):
                    return f"blocked by sandbox: matches custom pattern '{pat_str}'"
            except re.error:
                # Invalid regex — skip it but don't crash
                continue

    return None
