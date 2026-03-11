"""Nutshell TUI — Textual-based terminal UI for nutshell-server.

Usage:
    nutshell-tui                        # create new session (random ID)
    nutshell-tui --create my-project    # create named session
    nutshell-tui --attach my-project    # attach to existing session
    nutshell-tui --sessions-dir DIR     # custom sessions directory
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
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Markdown, Static, TextArea
from nutshell.runtime.status import ensure_session_status, read_session_status

SESSIONS_DIR = Path("sessions")
_DEFAULT_ENTITY = "entity/agent_core"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def _set_manifest_status(manifest_path: Path, status: str) -> None:
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _read_session_info(session_dir: Path) -> dict | None:
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    tasks_path = session_dir / "tasks.md"
    has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
    pid_alive = _pid_alive(manifest.get("pid"))
    status = manifest.get("status", "active")
    status_payload = read_session_status(session_dir)
    return {
        "id": session_dir.name,
        "entity": manifest.get("entity", "?"),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
    }


def _load_sessions(sessions_dir: Path) -> list[dict]:
    if not sessions_dir.exists():
        return []
    sessions: list[dict] = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        info = _read_session_info(session_dir)
        if info is not None:
            sessions.append(info)
    return sessions


def _create_session(sessions_dir: Path, session_id: str, entity: str, heartbeat: float = 10.0) -> Path:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "files").mkdir(exist_ok=True)
    (session_dir / "context.jsonl").touch(exist_ok=True)
    (session_dir / "tasks.md").touch(exist_ok=True)
    manifest = {
        "session_id": session_id,
        "entity": entity,
        "created_at": datetime.now().isoformat(),
        "heartbeat": heartbeat,
        "status": "active",
    }
    (session_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ensure_session_status(session_dir)
    return session_dir


def _session_tone(session: dict, model_status: str) -> tuple[str, str]:
    if session.get("status") == "stopped":
        return "stopped", "stopped"
    effective_state = model_status or session.get("model_state", "idle")
    if session.get("pid_alive") and effective_state == "running":
        return "running", "running"
    if session.get("has_tasks"):
        return "queued", "tasks queued"
    return "idle", "idle"


class SessionListItem(ListItem):
    def __init__(self, session: dict, model_status: str = "idle") -> None:
        tone, label = _session_tone(session, model_status)
        self.session_id = session["id"]
        self._base_classes = f"session-item {tone}"
        super().__init__(
            Label(f"{session['id']}\n{session['entity']}\n[{label}]"),
            classes=self._base_classes,
        )

    def refresh_state(self, session: dict, model_status: str = "idle", selected: bool = False) -> None:
        tone, label = _session_tone(session, model_status)
        suffix = " selected" if selected else ""
        self.set_class(False, "running")
        self.set_class(False, "queued")
        self.set_class(False, "idle")
        self.set_class(False, "stopped")
        self.set_class(False, "selected")
        self.add_class("session-item")
        self.add_class(tone)
        if selected:
            self.add_class("selected")
        label_widget = self.query_one(Label)
        label_widget.update(f"{session['id']}\n{session['entity']}\n[{label}]")


class ChatMessage(Static):
    DEFAULT_CSS = """
    ChatMessage {
        height: auto;
        margin: 0 0 1 0;
    }
    ChatMessage .shell {
        background: $surface;
        border-left: wide $panel;
        padding: 0 1 1 1;
    }
    ChatMessage.agent .shell { border-left: wide $accent; }
    ChatMessage.user .shell { border-left: wide $success; }
    ChatMessage.tool .shell { border-left: wide $warning; }
    ChatMessage.error .shell { border-left: wide $error; }
    ChatMessage.status .shell,
    ChatMessage.heartbeat_trigger .shell,
    ChatMessage.heartbeat_finished .shell {
        border-left: wide $primary;
        background: $surface-lighten-1;
    }
    ChatMessage .label {
        color: $text-muted;
        text-style: bold;
        padding-bottom: 1;
    }
    ChatMessage.agent .label { color: $accent; }
    ChatMessage.user .label { color: $success; }
    ChatMessage.tool .label { color: $warning; }
    ChatMessage.error .label { color: $error; }
    ChatMessage Markdown {
        background: transparent;
        padding: 0;
    }
    """

    def __init__(self, event: dict) -> None:
        super().__init__("", classes=event.get("type", ""))
        self._event = event

    def compose(self) -> ComposeResult:
        label, body, markdown = self._render_parts(self._event)
        with Vertical(classes="shell"):
            if label:
                yield Static(label, classes="label")
            if markdown:
                yield Markdown(body)
            else:
                yield Static(body)

    @staticmethod
    def _render_parts(event: dict) -> tuple[str, str, bool]:
        etype = event.get("type", "")
        if etype == "agent":
            title = "agent (heartbeat)" if event.get("triggered_by") == "heartbeat" else "agent"
            return title, event.get("content", ""), True
        if etype == "user":
            return "you", event.get("content", ""), True
        if etype == "tool":
            payload = json.dumps(event.get("input", {}), ensure_ascii=False, indent=2)
            return f"tool: {event.get('name', 'unknown')}", f"```json\n{payload}\n```", True
        if etype == "heartbeat_trigger":
            return "heartbeat", "Background heartbeat started.", False
        if etype == "heartbeat_finished":
            return "heartbeat", "Session finished. All tasks are done.", False
        if etype == "status":
            return "status", event.get("value", ""), False
        if etype == "error":
            return "error", event.get("content", ""), False
        return "", str(event), False


class ChatView(ScrollableContainer):
    DEFAULT_CSS = """
    ChatView {
        border: round $primary;
        height: 1fr;
        padding: 1;
        overflow-y: auto;
    }
    """

    def add_event(self, event: dict) -> None:
        if event.get("type") == "model_status":
            return
        self.mount(ChatMessage(event))
        self.scroll_end(animate=False)

    def clear_events(self) -> None:
        self.remove_children()


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 2;
        padding: 0 1;
        background: $surface-lighten-1;
        content-align: left middle;
    }
    StatusBar.running { color: $success; }
    StatusBar.queued { color: $warning; }
    StatusBar.idle { color: $text-muted; }
    StatusBar.stopped { color: $error; }
    """

    def update_status(self, session: dict | None, model_status: str) -> None:
        for name in ("running", "queued", "idle", "stopped"):
            self.set_class(False, name)
        if session is None:
            self.add_class("idle")
            self.update("No session selected")
            return
        tone, label = _session_tone(session, model_status)
        self.add_class(tone)
        self.update(f"session: {session['id']}  |  state: {label}  |  model: {model_status}")


class NutshellTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        layout: horizontal;
        height: 1fr;
    }
    #sidebar {
        width: 32;
        min-width: 24;
        border-right: solid $panel;
        padding: 1;
        layout: vertical;
    }
    #sidebar-title {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #sessions {
        height: 1fr;
        border: round $secondary;
        margin-bottom: 1;
    }
    .session-item {
        padding: 0 1;
        height: auto;
    }
    .session-item.selected {
        background: $boost;
    }
    .session-item.running { border-left: wide $success; }
    .session-item.queued { border-left: wide $warning; }
    .session-item.idle { border-left: wide $panel; }
    .session-item.stopped { border-left: wide $error; }
    #sidebar-actions {
        height: auto;
        layout: horizontal;
    }
    #sidebar-actions Button {
        width: 1fr;
        margin-right: 1;
    }
    #sidebar-actions Button:last-child {
        margin-right: 0;
    }
    #center {
        width: 1fr;
        padding: 1;
        layout: vertical;
    }
    #chat-status {
        margin-bottom: 1;
    }
    #input-row {
        height: 3;
        margin-top: 1;
    }
    #input {
        width: 1fr;
    }
    #rightbar {
        width: 36;
        min-width: 28;
        border-left: solid $panel;
        padding: 1;
        layout: vertical;
    }
    #tasks-title {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #tasks {
        height: 1fr;
        border: round $secondary;
    }
    """

    BINDINGS = [
        Binding("ctrl+c,q", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New Session"),
        Binding("ctrl+r", "refresh_sessions", "Refresh"),
        Binding("ctrl+j", "focus_sessions", "Sessions"),
        Binding("ctrl+l", "focus_input", "Input"),
        Binding("ctrl+s", "stop_session", "Stop"),
        Binding("ctrl+g", "start_session", "Start"),
    ]

    def __init__(self, sessions_dir: Path, session_id: str | None, entity: str) -> None:
        super().__init__()
        self._sessions_dir = sessions_dir
        self._entity = entity
        self._session_id = session_id
        self._ipc = None
        self._context_offset = 0
        self._model_status = "idle"
        self._session_cache: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("Sessions", id="sidebar-title")
                yield ListView(id="sessions")
                with Horizontal(id="sidebar-actions"):
                    yield Button("New", id="new-session", variant="primary")
                    yield Button("Start", id="start-session")
                    yield Button("Stop", id="stop-session", variant="error")
            with Vertical(id="center"):
                yield StatusBar(id="chat-status")
                yield ChatView(id="chat")
                with Horizontal(id="input-row"):
                    yield Input(placeholder="Type a message or /tasks /status /stop /start /quit", id="input")
            with Vertical(id="rightbar"):
                yield Static("Tasks", id="tasks-title")
                yield TextArea("", id="tasks", read_only=True, show_line_numbers=False)
        yield Footer()

    def on_mount(self) -> None:
        self._reload_sessions()
        if self._session_id is not None:
            self._attach_session(self._session_id)
        elif self._session_cache:
            self._attach_session(self._session_cache[0]["id"])
        self._poll_worker()
        self._refresh_loop()
        self.action_focus_input()

    def _current_session(self) -> dict | None:
        for session in self._session_cache:
            if session["id"] == self._session_id:
                return session
        return None

    def _reload_sessions(self) -> None:
        self._session_cache = _load_sessions(self._sessions_dir)
        list_view = self.query_one("#sessions", ListView)
        list_view.clear()
        selected_index = 0
        for index, session in enumerate(self._session_cache):
            is_current = session["id"] == self._session_id
            if is_current:
                selected_index = index
            tone_status = self._model_status if is_current else session.get("model_state", "idle")
            list_view.mount(SessionListItem(session, tone_status))
        if self._session_cache:
            list_view.index = selected_index
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        self.query_one("#chat-status", StatusBar).update_status(self._current_session(), self._model_status)

    def _refresh_tasks(self) -> None:
        tasks_widget = self.query_one("#tasks", TextArea)
        if not self._session_id:
            tasks_widget.load_text("")
            return
        tasks_path = self._sessions_dir / self._session_id / "tasks.md"
        content = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""
        tasks_widget.load_text(content)

    def _attach_session(self, session_id: str) -> None:
        from nutshell.runtime.ipc import FileIPC
        session_dir = self._sessions_dir / session_id
        if not session_dir.exists():
            return
        self._session_id = session_id
        self._ipc = FileIPC(session_dir)
        self._context_offset = 0
        self._model_status = read_session_status(session_dir).get("model_state", "idle")
        chat = self.query_one("#chat", ChatView)
        chat.clear_events()
        for event, offset in self._ipc.tail_display(0):
            self._context_offset = offset
            self._consume_event(event)
            chat.add_event(event)
        self._reload_sessions()
        self._refresh_tasks()
        self.action_focus_input()

    def _consume_event(self, event: dict) -> None:
        if event.get("type") != "model_status":
            return
        self._model_status = event.get("state", "idle")
        self._refresh_status_bar()
        self._reload_sessions()

    @work(exclusive=False)
    async def _poll_worker(self) -> None:
        while True:
            await asyncio.sleep(0.3)
            if self._ipc is None:
                continue
            chat = self.query_one("#chat", ChatView)
            for event, offset in self._ipc.tail_display(self._context_offset):
                self._context_offset = offset
                self.call_from_thread(self._consume_event, event)
                self.call_from_thread(chat.add_event, event)

    @work(exclusive=False)
    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            self.call_from_thread(self._reload_sessions)
            self.call_from_thread(self._refresh_tasks)

    @on(ListView.Selected, "#sessions")
    def on_session_selected(self, event: ListView.Selected) -> None:
        item = event.item
        session_id = getattr(item, "session_id", None)
        if session_id:
            self._attach_session(session_id)

    @on(Button.Pressed, "#new-session")
    def on_new_button(self) -> None:
        self.action_new_session()

    @on(Button.Pressed, "#stop-session")
    def on_stop_button(self) -> None:
        self.action_stop_session()

    @on(Button.Pressed, "#start-session")
    def on_start_button(self) -> None:
        self.action_start_session()

    @on(Input.Submitted, "#input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        if text.lower() in {"/quit", "/q", "/exit"}:
            self.exit()
            return
        if text.lower() == "/tasks":
            tasks_path = self._sessions_dir / self._session_id / "tasks.md" if self._session_id else None
            content = tasks_path.read_text(encoding="utf-8").strip() if tasks_path and tasks_path.exists() else "(empty)"
            self.query_one("#chat", ChatView).add_event({"type": "status", "value": f"tasks:\n{content}"})
            return
        if text.lower() == "/status":
            session = self._current_session()
            if session is None:
                message = "no session selected"
            else:
                _, label = _session_tone(session, self._model_status)
                message = f"session: {session['id']} | state: {label} | model: {self._model_status}"
            self.query_one("#chat", ChatView).add_event({"type": "status", "value": message})
            return
        if text.lower() == "/stop":
            self.action_stop_session()
            return
        if text.lower() == "/start":
            self.action_start_session()
            return
        if text.startswith("/"):
            self.query_one("#chat", ChatView).add_event({"type": "error", "content": f"Unknown command: {text}"})
            return
        if self._ipc is None:
            self.query_one("#chat", ChatView).add_event({"type": "error", "content": "No session attached."})
            return
        self._ipc.send_message(text)
        self.query_one("#chat", ChatView).add_event({"type": "user", "content": text})

    def action_new_session(self) -> None:
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_session(self._sessions_dir, session_id, self._entity)
        self._reload_sessions()
        self._attach_session(session_id)
        self.query_one("#chat", ChatView).add_event({"type": "status", "value": f"Created session: {session_id}"})

    def action_refresh_sessions(self) -> None:
        self._reload_sessions()
        self._refresh_tasks()

    def action_focus_sessions(self) -> None:
        self.query_one("#sessions", ListView).focus()

    def action_focus_input(self) -> None:
        self.query_one("#input", Input).focus()

    def action_stop_session(self) -> None:
        if not self._session_id:
            return
        _set_manifest_status(self._sessions_dir / self._session_id / "manifest.json", "stopped")
        self._reload_sessions()
        self._refresh_status_bar()
        self.query_one("#chat", ChatView).add_event({"type": "status", "value": "heartbeat paused"})

    def action_start_session(self) -> None:
        if not self._session_id:
            return
        _set_manifest_status(self._sessions_dir / self._session_id / "manifest.json", "active")
        self._reload_sessions()
        self._refresh_status_bar()
        self.query_one("#chat", ChatView).add_event({"type": "status", "value": "heartbeat resumed"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Nutshell TUI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--create", metavar="ID", nargs="?", const="", help="Create new session")
    group.add_argument("--attach", metavar="ID", help="Attach to existing session")
    parser.add_argument("--entity", "-e", default=_DEFAULT_ENTITY)
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR), metavar="DIR")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_id: str | None
    if args.create is not None:
        session_id = args.create or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_session(sessions_dir, session_id, args.entity)
    elif args.attach:
        session_id = args.attach
    else:
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_session(sessions_dir, session_id, args.entity)

    NutshellTUI(sessions_dir=sessions_dir, session_id=session_id, entity=args.entity).run()


if __name__ == "__main__":
    main()
