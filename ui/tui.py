"""Nutshell TUI — Textual terminal client.

Works directly with sessions/ and _sessions/ directories.
Requires nutshell-server to be running (nutshell-web is NOT required).

Usage:
    nutshell-tui
    nutshell-tui --sessions-dir ./sessions --system-sessions-dir ./_sessions
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Header, Input, Label,
    ListItem, ListView, RichLog, Static, TextArea,
)

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"

_TONE_ICON  = {"running": "●", "napping": "◉", "stopped": "○", "idle": "·"}
_TONE_COLOR = {"running": "green", "napping": "yellow", "stopped": "red", "idle": "bright_black"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _session_tone(info: dict) -> str:
    if info.get("pid_alive") and info.get("model_state") == "running" and info.get("status") != "stopped":
        return "running"
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return "napping"
    if info.get("status") == "stopped":
        return "stopped"
    return "idle"


def _fmt_ago(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        diff = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        if diff < 60:    return f"{int(diff)}s"
        if diff < 3600:  return f"{int(diff / 60)}m"
        if diff < 86400: return f"{int(diff / 3600)}h"
        return datetime.fromisoformat(ts).strftime("%b %-d")
    except Exception:
        return ""


def _read_sessions(system_dir: Path, sessions_dir: Path) -> list[dict]:
    if not system_dir.exists():
        return []
    result = []
    for d in sorted(system_dir.iterdir()):
        if not d.is_dir() or not (d / "manifest.json").exists():
            continue
        try:
            manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except Exception:
            st = {}
        sid = d.name
        tasks_path = sessions_dir / sid / "core" / "tasks.md"
        has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
        pid_alive = _pid_alive(st.get("pid"))
        result.append({
            "id": sid,
            "entity": manifest.get("entity", "?"),
            "model_state": st.get("model_state", "idle"),
            "model_source": st.get("model_source"),
            "status": st.get("status", "active"),
            "pid_alive": pid_alive,
            "has_tasks": has_tasks,
            "last_run_at": st.get("last_run_at"),
        })
    _pri = {"running": 0, "napping": 1, "idle": 2, "stopped": 3}
    result.sort(key=lambda s: (
        _pri.get(_session_tone(s), 2),
        -(datetime.fromisoformat(s["last_run_at"]).timestamp() if s.get("last_run_at") else 0),
    ))
    return result


# ── New Session Modal ─────────────────────────────────────────────────────────

class NewSessionModal(ModalScreen[tuple[str, str] | None]):
    """Dialog for creating a new session."""

    DEFAULT_CSS = """
    NewSessionModal { align: center middle; }
    #modal-box {
        width: 54; height: auto;
        background: #161b22; border: solid #58a6ff; padding: 1 2;
    }
    #modal-title { text-align: center; text-style: bold; color: #58a6ff; margin-bottom: 1; }
    #modal-box Label { color: #8b949e; margin-top: 1; }
    #modal-box Input {
        background: #21262d; color: #c9d1d9;
        border: solid #30363d; margin-bottom: 1;
    }
    #modal-box Input:focus { border: solid #58a6ff; }
    #modal-btns { margin-top: 1; align: right middle; }
    #modal-btns Button { margin-left: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("New Session", id="modal-title")
            yield Label("Session ID (empty = timestamp):")
            yield Input(placeholder="my-session", id="modal-id")
            yield Label("Entity path:")
            yield Input(value="entity/agent", id="modal-entity")
            with Horizontal(id="modal-btns"):
                yield Button("Cancel", id="modal-cancel")
                yield Button("Create", id="modal-create", variant="success")

    def on_mount(self) -> None:
        self.query_one("#modal-id", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "modal-create":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "modal-id":
            self.query_one("#modal-entity", Input).focus()
        else:
            self._submit()

    def _submit(self) -> None:
        sid = self.query_one("#modal-id", Input).value.strip()
        entity = self.query_one("#modal-entity", Input).value.strip() or "entity/agent"
        self.dismiss((sid or datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), entity))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Session List Item ─────────────────────────────────────────────────────────

class SessionItem(ListItem):
    def __init__(self, info: dict) -> None:
        super().__init__()
        self.session_info = info

    def compose(self) -> ComposeResult:
        tone = _session_tone(self.session_info)
        icon, color = _TONE_ICON[tone], _TONE_COLOR[tone]
        entity = self.session_info.get("entity", "?").split("/")[-1]
        ago = _fmt_ago(self.session_info.get("last_run_at"))
        meta = entity + (f" · {ago}" if ago else "")
        yield Label(f"[{color}]{icon}[/] [white]{self.session_info['id']}[/white]")
        yield Label(f"[bright_black]  {meta}[/bright_black]")


# ── Main App ──────────────────────────────────────────────────────────────────

class NutshellTUI(App):
    """Nutshell terminal UI."""

    TITLE = "Nutshell"

    CSS = """
    Screen { background: #0d1117; }

    /* Sidebar */
    #sidebar { width: 26; background: #161b22; border-right: solid #30363d; }
    #session-list { height: 1fr; background: #161b22; }
    #session-list ListItem {
        padding: 0 1; border-bottom: solid #21262d;
        height: 3; background: #161b22;
    }
    #session-list ListItem:hover { background: #21262d; }
    #session-list ListItem.--highlight { background: #21262d; border-left: solid #58a6ff; }
    #sidebar-btns {
        height: 3; border-top: solid #30363d;
        align: center middle; background: #161b22;
    }

    /* Shared panel title */
    .panel-title {
        height: 3; border-bottom: solid #30363d;
        background: #161b22; padding: 0 1;
        content-align: left middle; color: #8b949e; text-style: bold;
    }

    /* Chat */
    #chat-area { border-right: solid #30363d; }
    #chat-log  { height: 1fr; padding: 0 1; background: #0d1117; }
    #thinking  { height: 2; padding: 0 1; background: #0d1117; color: #8b949e; }
    #input-row {
        height: 3; border-top: solid #30363d;
        background: #161b22; padding: 0 1; align: left middle;
    }
    #msg-input {
        background: #21262d; color: #c9d1d9;
        border: solid #30363d; width: 1fr;
    }
    #msg-input:focus { border: solid #58a6ff; }

    /* Tasks */
    #tasks-panel { width: 26; background: #161b22; }
    #tasks-view  { height: 1fr; padding: 0 1; color: #c9d1d9; overflow-y: auto; }
    #tasks-editor { height: 1fr; display: none; }
    #tasks-footer {
        height: 3; border-top: solid #30363d;
        background: #161b22; align: center middle;
    }

    /* Buttons */
    Button { height: 1; min-width: 8; background: #21262d; color: #8b949e; border: solid #30363d; margin: 0 1; }
    Button:hover { background: #30363d; color: #c9d1d9; }
    Button.-success { color: #3fb950; border: solid #3fb950; }
    Button.-error   { color: #f85149; border: solid #f85149; }
    Button.-warning { color: #d29922; border: solid #d29922; }
    """

    BINDINGS = [
        Binding("n",      "new_session",    "New"),
        Binding("s",      "stop_session",   "Stop"),
        Binding("r",      "resume_session", "Resume"),
        Binding("e",      "edit_tasks",     "Edit Tasks"),
        Binding("ctrl+s", "save_tasks",     "Save", show=False),
        Binding("escape", "cancel_edit",    "Cancel", show=False),
        Binding("q",      "quit",           "Quit"),
    ]

    def __init__(self, sessions_dir: Path, system_sessions_dir: Path) -> None:
        super().__init__()
        self._sdir = sessions_dir
        self._sys = system_sessions_dir
        self._sessions: list[dict] = []
        self._current: str | None = None
        self._ctx_off: int = 0
        self._evt_off: int = 0
        self._thinking = False
        self._editing = False

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("Sessions", classes="panel-title")
                yield ListView(id="session-list")
                with Horizontal(id="sidebar-btns"):
                    yield Button("+ New", id="btn-new",   classes="-success")
                    yield Button("⏸",    id="btn-stop",  classes="-error")
                    yield Button("▶",    id="btn-start", classes="-warning")

            with Vertical(id="chat-area"):
                yield Static("[bright_black]no session[/]", id="chat-header", classes="panel-title")
                yield RichLog(id="chat-log", markup=True, wrap=True, highlight=False)
                yield Static("", id="thinking")
                with Horizontal(id="input-row"):
                    yield Input(placeholder="Type a message… (Enter to send)", id="msg-input")

            with Vertical(id="tasks-panel"):
                yield Static("Tasks", classes="panel-title")
                yield Static("[bright_black](no session)[/]", id="tasks-view")
                yield TextArea("", id="tasks-editor")
                with Horizontal(id="tasks-footer"):
                    yield Button("e Edit",   id="btn-edit-tasks")
                    yield Button("✓ Save",   id="btn-save-tasks",   classes="-success")
                    yield Button("✕ Cancel", id="btn-cancel-tasks", classes="-error")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#btn-save-tasks").display   = False
        self.query_one("#btn-cancel-tasks").display = False
        self.query_one("#tasks-editor").display     = False
        self._do_refresh_sessions()
        self.set_interval(3.0, self._do_refresh_sessions)
        self.set_interval(0.5, self._poll_events)
        self.set_interval(2.0, self._refresh_tasks)

    # ── Session list ──────────────────────────────────────────────────────────

    def _do_refresh_sessions(self) -> None:
        self._sessions = _read_sessions(self._sys, self._sdir)
        lv = self.query_one("#session-list", ListView)
        current_idx: int | None = None
        lv.clear()
        for i, info in enumerate(self._sessions):
            lv.append(SessionItem(info))
            if info["id"] == self._current:
                current_idx = i
        if current_idx is not None:
            lv.index = current_idx
        self._update_header()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionItem):
            self._attach(event.item.session_info["id"])

    # ── Attach session ────────────────────────────────────────────────────────

    def _attach(self, sid: str) -> None:
        if sid == self._current:
            return
        self._current = sid
        self._thinking = False
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        log.write(f"[bright_black]── {sid} ──[/bright_black]\n")

        from nutshell.runtime.ipc import FileIPC
        ipc = FileIPC(self._sys / sid)
        # Replay full history via IPC (handles turn → display event conversion)
        self._ctx_off = 0
        for event, off in ipc.tail_history(0):
            self._render(event)
            self._ctx_off = off
        # Set events.jsonl offset to current end (don't replay old runtime events)
        self._evt_off = ipc.events_size()

        self._update_header()
        self._refresh_tasks()
        self.query_one("#msg-input", Input).focus()

    def _update_header(self) -> None:
        hdr = self.query_one("#chat-header", Static)
        if not self._current:
            hdr.update("[bright_black]no session[/bright_black]")
            return
        info = next((s for s in self._sessions if s["id"] == self._current), None)
        tone_str = ""
        if info:
            tone = _session_tone(info)
            tone_str = f"  [{_TONE_COLOR[tone]}]{_TONE_ICON[tone]}[/] {tone}"
        dot = "[green]●[/green]" if self._thinking else "[bright_black]·[/bright_black]"
        hdr.update(f"[#58a6ff]{self._current}[/]{tone_str}  {dot}")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_events(self) -> None:
        if not self._current:
            return
        from nutshell.runtime.ipc import FileIPC
        ipc = FileIPC(self._sys / self._current)
        for event, off in ipc.tail_context(self._ctx_off):
            self._render(event)
            self._ctx_off = off
        for event, off in ipc.tail_runtime_events(self._evt_off):
            self._handle_runtime(event)
            self._evt_off = off

    # ── Event rendering ───────────────────────────────────────────────────────

    def _render(self, event: dict) -> None:
        """Render a display event (already converted by FileIPC) to the chat log."""
        log = self.query_one("#chat-log", RichLog)
        etype = event.get("type")
        ts = _fmt_ago(event.get("ts"))
        ts_s = f" [bright_black]{ts}[/bright_black]" if ts else ""

        if etype == "user":
            log.write(f"\n[green]you[/green]{ts_s}\n{event.get('content', '')}")

        elif etype == "agent":
            is_hb = event.get("triggered_by") == "heartbeat"
            color  = "#6b9fd4" if is_hb else "#58a6ff"
            prefix = "⏱ "     if is_hb else ""
            log.write(f"\n[{color}]{prefix}agent[/{color}]{ts_s}")
            content = event.get("content", "")
            if content.strip():
                try:
                    log.write(Markdown(content))
                except Exception:
                    log.write(content)
            self._set_thinking(False)

        elif etype == "tool":
            inp = json.dumps(event.get("input", {}), ensure_ascii=False)
            if len(inp) > 80:
                inp = inp[:77] + "…"
            log.write(f"[yellow]  [{event.get('name', '?')}] {inp}[/yellow]")

        elif etype == "heartbeat_trigger":
            log.write("\n[#6b9fd4]⏱ heartbeat — checking tasks[/#6b9fd4]")

        elif etype == "heartbeat_finished":
            self._set_thinking(False)
            log.write("[bright_black]── session finished ──[/bright_black]")

        elif etype == "status":
            log.write(f"[bright_black]── {event.get('value', '')} ──[/bright_black]")

        elif etype == "error":
            self._set_thinking(False)
            log.write(f"[red]⚠ {event.get('content', 'error')}[/red]")

    def _handle_runtime(self, event: dict) -> None:
        """Handle runtime events (from events.jsonl)."""
        etype = event.get("type")

        if etype == "model_status":
            self._set_thinking(event.get("state") == "running")
            self._update_header()

        elif etype == "partial_text":
            snippet = (event.get("content") or "")[-80:].replace("\n", " ")
            self.query_one("#thinking", Static).update(
                f"[#58a6ff]● ● ●[/] [bright_black]{snippet}…[/bright_black]"
            )
        else:
            # status, error, heartbeat_trigger, heartbeat_finished: reuse _render
            self._render(event)

    def _set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking
        ta = self.query_one("#thinking", Static)
        ta.update("[#58a6ff]● ● ●[/] [bright_black]agent is thinking…[/bright_black]" if thinking else "")
        self._update_header()

    # ── Message input ─────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "msg-input":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text or not self._current:
            return
        try:
            from nutshell.runtime.ipc import FileIPC
            FileIPC(self._sys / self._current).send_message(text)
        except Exception as exc:
            self.query_one("#chat-log", RichLog).write(f"[red]Send failed: {exc}[/red]")

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def _refresh_tasks(self) -> None:
        if not self._current or self._editing:
            return
        tasks_path = self._sdir / self._current / "core" / "tasks.md"
        content = tasks_path.read_text(encoding="utf-8").strip() if tasks_path.exists() else ""
        self.query_one("#tasks-view", Static).update(
            content or "[bright_black](empty)[/bright_black]"
        )

    def action_edit_tasks(self) -> None:
        if not self._current:
            return
        self._editing = True
        tasks_path = self._sdir / self._current / "core" / "tasks.md"
        content = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""
        editor = self.query_one("#tasks-editor", TextArea)
        editor.load_text(content)
        self.query_one("#tasks-view").display   = False
        editor.display                          = True
        self.query_one("#btn-edit-tasks").display   = False
        self.query_one("#btn-save-tasks").display   = True
        self.query_one("#btn-cancel-tasks").display = True
        editor.focus()

    def action_save_tasks(self) -> None:
        if not self._current or not self._editing:
            return
        content = self.query_one("#tasks-editor", TextArea).text
        tasks_path = self._sdir / self._current / "core" / "tasks.md"
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        tasks_path.write_text(content, encoding="utf-8")
        self._close_tasks_edit()
        self._refresh_tasks()

    def action_cancel_edit(self) -> None:
        if self._editing:
            self._close_tasks_edit()

    def _close_tasks_edit(self) -> None:
        self._editing = False
        self.query_one("#tasks-view").display       = True
        self.query_one("#tasks-editor").display     = False
        self.query_one("#btn-edit-tasks").display   = True
        self.query_one("#btn-save-tasks").display   = False
        self.query_one("#btn-cancel-tasks").display = False
        self.query_one("#msg-input", Input).focus()

    # ── Stop / Start ──────────────────────────────────────────────────────────

    def action_stop_session(self) -> None:
        if not self._current:
            return
        try:
            from nutshell.runtime.status import write_session_status
            from nutshell.runtime.ipc import FileIPC
            system_dir = self._sys / self._current
            write_session_status(system_dir, status="stopped", stopped_at=datetime.now().isoformat())
            FileIPC(system_dir).append_event({"type": "status", "value": "heartbeat paused"})
        except Exception as exc:
            self.query_one("#chat-log", RichLog).write(f"[red]Stop failed: {exc}[/red]")
        self._do_refresh_sessions()

    def action_resume_session(self) -> None:
        if not self._current:
            return
        try:
            from nutshell.runtime.status import write_session_status
            from nutshell.runtime.ipc import FileIPC
            system_dir = self._sys / self._current
            write_session_status(system_dir, status="active", stopped_at=None)
            FileIPC(system_dir).append_event({"type": "status", "value": "heartbeat resumed"})
        except Exception as exc:
            self.query_one("#chat-log", RichLog).write(f"[red]Resume failed: {exc}[/red]")
        self._do_refresh_sessions()

    # ── New session ───────────────────────────────────────────────────────────

    def action_new_session(self) -> None:
        def _on_result(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            sid, entity = result
            try:
                from ui.web.sessions import _init_session
                _init_session(self._sdir, self._sys, sid, entity, 600.0)
            except Exception as exc:
                self.query_one("#chat-log", RichLog).write(f"[red]Create failed: {exc}[/red]")
                return
            self._do_refresh_sessions()
            self._attach(sid)

        self.push_screen(NewSessionModal(), _on_result)

    # ── Button dispatcher ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {
            "btn-new":          self.action_new_session,
            "btn-stop":         self.action_stop_session,
            "btn-start":        self.action_resume_session,
            "btn-edit-tasks":   self.action_edit_tasks,
            "btn-save-tasks":   self.action_save_tasks,
            "btn-cancel-tasks": self.action_cancel_edit,
        }.get(event.button.id, lambda: None)()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Nutshell TUI")
    parser.add_argument("--sessions-dir",        default=str(SESSIONS_DIR),         metavar="DIR")
    parser.add_argument("--system-sessions-dir", default=str(_SYSTEM_SESSIONS_DIR), metavar="DIR")
    args = parser.parse_args()
    NutshellTUI(Path(args.sessions_dir), Path(args.system_sessions_dir)).run()


if __name__ == "__main__":
    main()
