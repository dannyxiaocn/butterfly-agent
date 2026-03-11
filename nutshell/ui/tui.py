"""Nutshell TUI — Textual-based terminal UI for nutshell-server.

Usage:
    nutshell-tui                        # create new instance (random ID)
    nutshell-tui --create my-project    # create named instance
    nutshell-tui --attach my-project    # attach to existing instance
    nutshell-tui --instances-dir DIR    # custom instances directory
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Markdown, Static

INSTANCES_DIR = Path("instances")
_DEFAULT_ENTITY = "entity/agent_core"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


# ── Utility ────────────────────────────────────────────────────────────────

def _set_manifest_status(manifest_path: Path, status: str) -> None:
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_instances(instances_dir: Path) -> list[dict]:
    if not instances_dir.exists():
        return []
    result = []
    for d in sorted(instances_dir.iterdir()):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        result.append({
            "id": d.name,
            "entity": manifest.get("entity", "?"),
            "alive": _pid_alive(manifest.get("pid")),
        })
    return result


def _create_instance(instances_dir: Path, instance_id: str, entity: str, heartbeat: float = 10.0) -> Path:
    instance_dir = instances_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "files").mkdir(exist_ok=True)
    (instance_dir / "context.jsonl").touch(exist_ok=True)
    manifest = {
        "instance_id": instance_id,
        "entity": entity,
        "created_at": datetime.now().isoformat(),
        "heartbeat": heartbeat,
    }
    (instance_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return instance_dir


# ── Widgets ────────────────────────────────────────────────────────────────

class ChatMessage(Static):
    """Single chat message rendered in the chat view."""

    DEFAULT_CSS = """
    ChatMessage {
        padding: 0 1;
        margin-bottom: 1;
    }
    ChatMessage.agent { color: $accent; }
    ChatMessage.user  { color: $success; }
    ChatMessage.tool  { color: $warning; opacity: 0.8; }
    ChatMessage.heartbeat { color: $warning; opacity: 0.7; }
    ChatMessage.status { color: $text-muted; opacity: 0.6; }
    ChatMessage.error  { color: $error; }
    """

    def __init__(self, event: dict) -> None:
        etype = event.get("type", "")
        content = self._format(event)
        super().__init__(content, classes=etype)

    @staticmethod
    def _format(event: dict) -> str:
        etype = event.get("type", "")
        if etype == "agent":
            return f"agent❯ {event.get('content', '')}"
        if etype == "user":
            return f"you  ❯ {event.get('content', '')}"
        if etype == "tool":
            return f"  [tool] {event.get('name')}({event.get('input', {})})"
        if etype == "heartbeat":
            return f"  [heartbeat] {event.get('content', '')}"
        if etype == "heartbeat_finished":
            return "  [instance finished — all tasks done]"
        if etype == "status":
            return f"  [status: {event.get('value')}]"
        if etype == "error":
            return f"  [error] {event.get('content')}"
        return str(event)


class ChatView(ScrollableContainer):
    """Scrollable chat history panel."""

    DEFAULT_CSS = """
    ChatView {
        border: round $primary;
        padding: 0;
        height: 1fr;
    }
    """

    def add_event(self, event: dict) -> None:
        msg = ChatMessage(event)
        self.mount(msg)
        self.scroll_end(animate=False)


class KanbanPanel(Static):
    """Right-side panel showing kanban.md content."""

    DEFAULT_CSS = """
    KanbanPanel {
        border: round $secondary;
        padding: 1;
        width: 28;
        height: auto;
        min-height: 8;
    }
    """

    def update_kanban(self, content: str) -> None:
        text = content.strip() if content.strip() else "(empty)"
        self.update(f"[bold]Kanban[/bold]\n{'─' * 20}\n{text}")


class InstancePanel(Static):
    """Right-side panel listing instances."""

    DEFAULT_CSS = """
    InstancePanel {
        border: round $secondary;
        padding: 1;
        width: 28;
        height: 1fr;
    }
    """

    def update_instances(self, instances: list[dict], current_id: str | None) -> None:
        lines = ["[bold]Instances[/bold]", "─" * 20]
        for inst in instances:
            bullet = "●" if inst["alive"] else "○"
            marker = " ◀" if inst["id"] == current_id else ""
            lines.append(f"{bullet} {inst['id']}{marker}")
        if not instances:
            lines.append("(none)")
        self.update("\n".join(lines))


class StatusBar(Static):
    """Bottom status bar."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-background;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def set_status(self, instance_id: str | None, server_alive: bool) -> None:
        srv = "[green]running[/green]" if server_alive else "[red]stopped[/red]"
        inst = instance_id or "none"
        self.update(f"server: {srv}  │  instance: {inst}  │  q: quit  │  ctrl+n: new")


# ── Main App ───────────────────────────────────────────────────────────────

class NutshellTUI(App):
    """Nutshell terminal UI."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        layout: horizontal;
        height: 1fr;
    }
    #left {
        layout: vertical;
        width: 1fr;
    }
    #right {
        layout: vertical;
        width: 28;
    }
    #input-row {
        height: 3;
        border: round $accent;
        padding: 0 1;
    }
    Input {
        border: none;
        height: 1;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c,q", "quit", "Quit"),
        Binding("ctrl+n", "new_instance", "New Instance"),
    ]

    def __init__(self, instances_dir: Path, instance_id: str | None, entity: str) -> None:
        super().__init__()
        self._instances_dir = instances_dir
        self._entity = entity
        self._instance_id: str | None = instance_id
        self._ipc = None
        self._context_offset = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield ChatView(id="chat")
                with Horizontal(id="input-row"):
                    yield Input(placeholder="Type a message... (/status /kanban /exit)", id="input")
            with Vertical(id="right"):
                yield KanbanPanel(id="kanban")
                yield InstancePanel(id="instances")
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        if self._instance_id:
            self._attach_instance(self._instance_id)
        self._poll_worker()
        self._refresh_panels()

    def _attach_instance(self, instance_id: str) -> None:
        from nutshell.core.ipc import FileIPC
        instance_dir = self._instances_dir / instance_id
        self._instance_id = instance_id
        self._ipc = FileIPC(instance_dir)
        self._context_offset = 0
        # Replay existing context
        chat = self.query_one("#chat", ChatView)
        for event, offset in self._ipc.tail_display(0):
            self._context_offset = offset
            chat.add_event(event)

    @work(exclusive=False)
    async def _poll_worker(self) -> None:
        """Background worker: poll outbox every 0.3s."""
        while True:
            await asyncio.sleep(0.3)
            if self._ipc is None:
                continue
            chat = self.query_one("#chat", ChatView)
            for event, offset in self._ipc.tail_display(self._context_offset):
                self._context_offset = offset
                self.call_from_thread(chat.add_event, event)

    @work(exclusive=False)
    async def _refresh_panels(self) -> None:
        """Background worker: refresh kanban + instances every 2s."""
        while True:
            await asyncio.sleep(2.0)
            self._do_refresh()

    def _do_refresh(self) -> None:
        # Kanban
        kanban_panel = self.query_one("#kanban", KanbanPanel)
        if self._instance_id:
            kanban_path = self._instances_dir / self._instance_id / "kanban.md"
            content = kanban_path.read_text(encoding="utf-8") if kanban_path.exists() else ""
            kanban_panel.update_kanban(content)

        # Instances
        inst_panel = self.query_one("#instances", InstancePanel)
        instances = _load_instances(self._instances_dir)
        inst_panel.update_instances(instances, self._instance_id)

        # Status bar
        status = self.query_one("#status", StatusBar)
        alive = False
        if self._instance_id:
            try:
                manifest_path = self._instances_dir / self._instance_id / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                alive = _pid_alive(manifest.get("pid"))
            except Exception:
                pass
        status.set_status(self._instance_id, alive)

    @on(Input.Submitted, "#input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).clear()
        if not text:
            return

        # Built-in commands
        if text.lower() in ("/exit", "/quit", "/q"):
            self.exit()
            return

        if text.lower() == "/kanban":
            kanban_path = self._instances_dir / self._instance_id / "kanban.md" if self._instance_id else None
            content = kanban_path.read_text(encoding="utf-8").strip() if (kanban_path and kanban_path.exists()) else "(empty)"
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "status", "value": f"kanban:\n{content}"})
            return

        if text.lower() == "/status":
            pid = None
            alive = False
            if self._instance_id:
                try:
                    manifest_path = self._instances_dir / self._instance_id / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    pid = manifest.get("pid")
                    alive = _pid_alive(pid)
                except Exception:
                    pass
            msg = f"server: {'running (pid ' + str(pid) + ')' if alive else 'not running'}"
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "status", "value": msg})
            return

        if text.lower() == "/stop":
            if self._instance_id:
                _set_manifest_status(self._instances_dir / self._instance_id / "manifest.json", "stopped")
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "status", "value": "heartbeat paused (/start to resume)"})
            return

        if text.lower() == "/start":
            if self._instance_id:
                _set_manifest_status(self._instances_dir / self._instance_id / "manifest.json", "active")
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "status", "value": "heartbeat resumed"})
            return

        if text.startswith("/"):
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "error", "content": f"Unknown command: {text}"})
            return

        # Send to server
        if self._ipc is None:
            chat = self.query_one("#chat", ChatView)
            chat.add_event({"type": "error", "content": "No instance attached. Use --create or --attach."})
            return

        self._ipc.send_message(text)
        chat = self.query_one("#chat", ChatView)
        chat.add_event({"type": "user", "content": text})

    def action_new_instance(self) -> None:
        instance_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_instance(self._instances_dir, instance_id, self._entity)
        self._attach_instance(instance_id)
        self._do_refresh()
        chat = self.query_one("#chat", ChatView)
        chat.add_event({"type": "status", "value": f"Created instance: {instance_id}"})


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Nutshell TUI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--create", metavar="ID", nargs="?", const="", help="Create new instance")
    group.add_argument("--attach", metavar="ID", help="Attach to existing instance")
    parser.add_argument("--entity", "-e", default=_DEFAULT_ENTITY)
    parser.add_argument("--instances-dir", default=str(INSTANCES_DIR), metavar="DIR")
    args = parser.parse_args()

    instances_dir = Path(args.instances_dir)
    instances_dir.mkdir(parents=True, exist_ok=True)

    instance_id: str | None = None

    if args.create is not None:
        instance_id = args.create or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_instance(instances_dir, instance_id, args.entity)
    elif args.attach:
        instance_id = args.attach
    else:
        # Default: create new
        instance_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_instance(instances_dir, instance_id, args.entity)

    app = NutshellTUI(instances_dir=instances_dir, instance_id=instance_id, entity=args.entity)
    app.run()


if __name__ == "__main__":
    main()
