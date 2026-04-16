"""Guardian — restrict filesystem writes to a single rooted subtree.

Used by sub-agent's ``explorer`` mode: every Write/Edit goes through
``check_write``; bash is given the guardian root as its cwd plus an env hint
(``BUTTERFLY_GUARDIAN_ROOT``) for the prompt to reason about.

Symlink escapes are blocked because we resolve the target before the
``relative_to`` check. Bash command-text inspection is intentionally NOT
attempted — it's bypassable in too many ways (heredocs, subshells, ``cd``);
the prompt-level rule plus Write/Edit hard blocks is the contract.
"""
from __future__ import annotations

from pathlib import Path


class Guardian:
    """A write boundary anchored at a single directory.

    The guardian root is resolved at construction time so callers can change
    cwd freely; relative paths handed to ``check_write`` are joined against
    the original cwd by Python's ``Path``, then resolved.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def __repr__(self) -> str:
        return f"Guardian(root={self.root!r})"

    def resolve_target(self, path: Path | str) -> Path:
        """Resolve `path` to an absolute path. Relative paths are joined to root."""
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return p.resolve()

    def is_allowed(self, path: Path | str) -> bool:
        """True if `path` resolves to somewhere under the guardian root."""
        target = self.resolve_target(path)
        try:
            target.relative_to(self.root)
            return True
        except ValueError:
            return False

    def check_write(self, path: Path | str) -> None:
        """Raise PermissionError if `path` resolves outside the guardian root."""
        if not self.is_allowed(path):
            target = self.resolve_target(path)
            raise PermissionError(
                f"guardian: write to {target} denied; allowed root is {self.root}"
            )
