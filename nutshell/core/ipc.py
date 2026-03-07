"""FileIPC: file-based IPC between nutshell daemon and chat UI."""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator


class FileIPC:
    """File-based IPC for a single instance.

    Layout inside instance_dir/:
        inbox.jsonl   — UI → daemon (user messages, append-only)
        outbox.jsonl  — daemon → UI (all output, append-only)
        daemon.pid    — daemon PID (written by daemon, read by UI)
    """

    def __init__(self, instance_dir: Path) -> None:
        self.instance_dir = instance_dir
        self.inbox_path = instance_dir / "inbox.jsonl"
        self.outbox_path = instance_dir / "outbox.jsonl"
        self.pid_path = instance_dir / "daemon.pid"

        if not self.inbox_path.exists():
            self.inbox_path.touch()
        if not self.outbox_path.exists():
            self.outbox_path.touch()

    # ── Daemon-side ──────────────────────────────────────────────────

    def write_pid(self) -> None:
        """Write current process PID to daemon.pid."""
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def clear_pid(self) -> None:
        """Remove daemon.pid when daemon stops."""
        if self.pid_path.exists():
            self.pid_path.unlink()

    def read_pid(self) -> int | None:
        if not self.pid_path.exists():
            return None
        try:
            return int(self.pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def is_daemon_alive(self) -> bool:
        """Check if the daemon process owning this instance is alive."""
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def poll_inbox(self, offset: int) -> tuple[list[dict], int]:
        """Read new messages from inbox starting at byte offset.

        Returns (messages, new_offset).
        """
        if not self.inbox_path.exists():
            return [], offset
        with self.inbox_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()

        messages: list[dict] = []
        for line in data.splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return messages, new_offset

    def append_outbox(self, event: dict) -> None:
        """Append an event to outbox.jsonl (daemon → UI, append-only)."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.outbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── Client-side ──────────────────────────────────────────────────

    def send_message(self, content: str, msg_id: str | None = None) -> str:
        """Append a user message to inbox.jsonl. Returns message id."""
        msg_id = msg_id or str(uuid.uuid4())
        event = {
            "type": "user",
            "content": content,
            "id": msg_id,
            "ts": datetime.now().isoformat(),
        }
        with self.inbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return msg_id

    def tail_outbox(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield (event, new_offset) tuples from outbox starting at offset."""
        if not self.outbox_path.exists():
            return
        with self.outbox_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line), f.tell()
                    except json.JSONDecodeError:
                        pass

    def outbox_size(self) -> int:
        """Current outbox file size in bytes (use as a quick-change sentinel)."""
        if not self.outbox_path.exists():
            return 0
        return self.outbox_path.stat().st_size
