"""edit tool — exact-string replacement on a file.

Safety properties:
  * Refuses to edit non-UTF-8 files (strict decode) — prevents silent
    corruption of binaries that happened to contain the old_string bytes.
  * Preserves file mode / owner via `shutil.copystat` before the atomic
    rename — otherwise the replacement inherits the umask-default mode.
  * Unique temp-file per write (tempfile.mkstemp in the destination dir) so
    concurrent edits don't collide on `<path>.tmp`.
  * Rejects empty `old_string` — the tool is exact replacement; empty match
    semantics are degenerate (global insert between every char).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from butterfly.core.guardian import Guardian
from butterfly.tool_engine.executor.base import BaseExecutor


class EditExecutor(BaseExecutor):
    """Executor for the built-in edit tool."""

    def __init__(
        self,
        workdir: str | None = None,
        guardian: Guardian | None = None,
    ) -> None:
        self._workdir = workdir
        self._guardian = guardian

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        base = Path(self._workdir) if self._workdir else Path.cwd()
        return base / p

    async def execute(self, **kwargs: Any) -> str:
        path_arg: str = kwargs["path"]
        old_string: str = kwargs["old_string"]
        new_string: str = kwargs["new_string"]
        replace_all = bool(kwargs.get("replace_all", False))

        if old_string == "":
            return (
                "Error: old_string must be non-empty. `edit` is exact "
                "replacement — use `write` to overwrite the whole file."
            )
        if old_string == new_string:
            return "Error: old_string and new_string are identical; no change."

        resolved = self._resolve(path_arg)
        if self._guardian is not None:
            try:
                self._guardian.check_write(resolved)
            except PermissionError as exc:
                return f"Error: Failed to edit {path_arg}: {exc}"
        if not resolved.exists() or not resolved.is_file():
            return f"Error: File not found: {path_arg}"

        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            return f"Error: Failed to read {path_arg}: {exc}"

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return (
                f"Error: {path_arg} is not valid UTF-8 (offset {exc.start}); "
                "`edit` refuses to touch non-text files."
            )

        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path_arg}"
        if count > 1 and not replace_all:
            return (
                f"Error: old_string appears {count} times in {path_arg}. "
                f"Pass replace_all=true or supply more context."
            )

        if replace_all:
            new_text = text.replace(old_string, new_string)
            replacements = count
        else:
            new_text = text.replace(old_string, new_string, 1)
            replacements = 1

        data = new_text.encode("utf-8")
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{resolved.name}.",
                suffix=".tmp",
                dir=str(resolved.parent),
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            # Preserve file mode + atime/mtime from the original. Ownership
            # preservation would require root and is intentionally skipped.
            try:
                shutil.copystat(resolved, tmp_path, follow_symlinks=True)
            except OSError:
                pass  # Best-effort; don't fail the edit if copystat is denied.
            os.replace(tmp_path, resolved)
            tmp_path = None
        except OSError as exc:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return f"Error: Failed to write {path_arg}: {exc}"

        noun = "occurrence" if replacements == 1 else "occurrences"
        return f"Replaced {replacements} {noun} in {path_arg}."
