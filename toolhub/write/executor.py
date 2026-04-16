"""write tool — atomic whole-file write with parent-dir creation.

Uses a unique temp file per call (tempfile.mkstemp) so concurrent writes to
the same path don't collide on a fixed `<path>.tmp` name.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from butterfly.core.guardian import Guardian
from butterfly.tool_engine.executor.base import BaseExecutor


class WriteExecutor(BaseExecutor):
    """Executor for the built-in write tool."""

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
        content: str = kwargs["content"]

        resolved = self._resolve(path_arg)
        if self._guardian is not None:
            try:
                self._guardian.check_write(resolved)
            except PermissionError as exc:
                return f"Error: {exc}"
        data = content.encode("utf-8")
        tmp_path: str | None = None
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp gives us a unique name in the destination dir so two
            # concurrent writes to the same path don't collide on `<path>.tmp`.
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{resolved.name}.",
                suffix=".tmp",
                dir=str(resolved.parent),
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
            except BaseException:
                # Close-on-error handled by fdopen context; nothing more to do.
                raise
            os.replace(tmp_path, resolved)
            tmp_path = None  # replace consumed it
        except OSError as exc:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return f"Error: Failed to write {path_arg}: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return f"Error: Failed to write {path_arg}: {exc}"

        return f"Wrote {len(data)} bytes to {path_arg}."
