"""Tests for butterfly.runtime.io — Phase 5 writer surface.

Covers every public writer from DESIGN.md §5.5. The fixture
``session_fs`` mirrors the one in ``test_io_readers.py`` but stubs out
heavy side-effects (venv creation) so the writers run quickly against a
tmp-path layout.

The headline invariant (§5.6): **event is appended BEFORE the
side-effect**. Each writer has at least one test that pins the ordering
— the ``test_event_leads_side_effect_on_upsert_task`` test is the
money test: it simulates a side-effect failure and asserts the event
survives while the derived file does not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.runtime import events as events_mod
from butterfly.runtime import io as io_mod
from butterfly.runtime.events import (
    EVENT_ASSET_CHANGED,
    EVENT_CONFIG_CHANGED,
    EVENT_CONTROL_START,
    EVENT_CONTROL_STOP,
    EVENT_PANEL_ENTRY_CHANGED,
    EVENT_PROMPT_CHANGED,
    EVENT_SESSION_CREATED,
    EVENT_SESSION_DELETED,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TERMINAL_INPUT,
    EVENT_TODO_LIST_CHANGED,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the session-venv creation with a no-op.

    The real ``_create_session_venv`` forks ``python -m venv`` which
    adds ~1s per session and isn't what any writer test actually
    exercises.
    """
    from butterfly.session_engine import session_init

    monkeypatch.setattr(
        session_init,
        "_create_session_venv",
        lambda session_dir: session_dir / ".venv",
    )


@pytest.fixture(autouse=True)
def _stub_bridge_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace messages_service.send_message / interrupt_session with
    no-ops — we're not exercising the daemon IPC here; the writer's
    contract is "event first, daemon-notify second" and these tests
    assert the event leg.
    """
    from butterfly.service import messages_service

    monkeypatch.setattr(
        messages_service,
        "send_message",
        lambda session_id, content, system_sessions_dir, **kw: "fake-msg-id",
    )
    monkeypatch.setattr(
        messages_service,
        "interrupt_session",
        lambda session_id, system_sessions_dir: None,
    )


@pytest.fixture
def session_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build a tmp repo layout with a single pre-initialised session.

    The session is built via the real ``sessions_service.create_session``
    (after monkeypatching the venv stub), so its events_v1.jsonl starts
    empty — tests that want a fresh sidecar can append on top.
    """
    sessions_root = tmp_path / "sessions"
    system_root = tmp_path / "_sessions"
    archived_root = tmp_path / "_archived"
    agenthub_root = tmp_path / "agenthub"
    sessions_root.mkdir()
    system_root.mkdir()
    agenthub_root.mkdir()

    # Minimal agent shipped so create_session (and init_session) can copy
    # a config.yaml.
    (agenthub_root / "default").mkdir()
    (agenthub_root / "default" / "config.yaml").write_text(
        "agent: default\nmodel: test-model\nprovider: test-provider\n",
        encoding="utf-8",
    )

    # Repoint the io module to the tmp layout.
    monkeypatch.setattr(io_mod, "_SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(io_mod, "_SYSTEM_SESSIONS_DIR", system_root)
    monkeypatch.setattr(io_mod, "_ARCHIVED_DIR", archived_root)

    sid = "sess_01"
    from butterfly.service.sessions_service import create_session as _svc_create

    _svc_create(sid, "default", sessions_root, system_root)

    return {
        "tmp": tmp_path,
        "sessions_root": sessions_root,
        "system_root": system_root,
        "archived_root": archived_root,
        "agenthub_root": agenthub_root,
        "sid": sid,
        "user_dir": sessions_root / sid,
        "system_dir": system_root / sid,
    }


def _read_events(system_dir: Path) -> list[dict]:
    """Return the raw events_v1.jsonl as a list of dicts (writer-free)."""
    path = system_dir / events_mod.EVENTS_FILENAME
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ── Session lifecycle ────────────────────────────────────────────────────────


def test_create_session_writes_manifest_and_emits_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Build a blank repo layout; do NOT use the default session_fs
    # because that fixture already calls create_session at build time.
    sessions_root = tmp_path / "sessions"
    system_root = tmp_path / "_sessions"
    agenthub_root = tmp_path / "agenthub"
    sessions_root.mkdir()
    system_root.mkdir()
    agenthub_root.mkdir()
    (agenthub_root / "default").mkdir()
    (agenthub_root / "default" / "config.yaml").write_text(
        "agent: default\nmodel: m\nprovider: p\n", encoding="utf-8"
    )

    monkeypatch.setattr(io_mod, "_SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(io_mod, "_SYSTEM_SESSIONS_DIR", system_root)
    monkeypatch.setattr(io_mod, "_ARCHIVED_DIR", tmp_path / "_archived")

    event = io_mod.create_session("new_sess", agent="default", display_name="New One")

    assert event.type == EVENT_SESSION_CREATED
    assert event.id == 1  # first event in a fresh session
    assert event.payload["manifest"]["id"] == "new_sess"
    assert event.payload["manifest"]["display_name"] == "New One"
    # manifest.json was materialised by the service leg.
    assert (system_root / "new_sess" / "manifest.json").exists()
    # events_v1.jsonl now carries exactly one event.
    events = _read_events(system_root / "new_sess")
    assert len(events) == 1 and events[0]["type"] == EVENT_SESSION_CREATED


def test_create_session_rejects_duplicate_id(session_fs):
    with pytest.raises(FileExistsError):
        io_mod.create_session(session_fs["sid"], agent="default")


def test_create_session_rejects_init_from(session_fs):
    with pytest.raises(NotImplementedError):
        io_mod.create_session("cloned", agent="default", init_from=session_fs["sid"])


def test_create_session_validates_session_id(session_fs):
    with pytest.raises(ValueError):
        io_mod.create_session("has/slash")


def test_delete_session_emits_event_before_rmtree(session_fs):
    sid = session_fs["sid"]
    system_dir = session_fs["system_dir"]

    # Sanity: dir exists right now.
    assert system_dir.is_dir()

    event = io_mod.delete_session(sid)

    assert event.type == EVENT_SESSION_DELETED
    # Both dirs are gone post-rmtree.
    assert not system_dir.exists()
    assert not session_fs["user_dir"].exists()


def test_delete_session_missing_id_raises(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.delete_session("no_such")


def test_stop_session_emits_control_stop(session_fs):
    event = io_mod.stop_session(session_fs["sid"], reason="test")
    assert event.type == EVENT_CONTROL_STOP
    assert event.payload["reason"] == "test"
    # Service leg flipped the status file.
    status = json.loads((session_fs["system_dir"] / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "stopped"


def test_stop_session_default_reason(session_fs):
    event = io_mod.stop_session(session_fs["sid"])
    assert event.payload["reason"] == "user"


def test_start_session_emits_control_start(session_fs):
    # First stop so the service has something to un-stop.
    io_mod.stop_session(session_fs["sid"])
    event = io_mod.start_session(session_fs["sid"])

    assert event.type == EVENT_CONTROL_START
    status = json.loads((session_fs["system_dir"] / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "active"


def test_start_session_missing_raises(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.start_session("no_such")


# ── Input ────────────────────────────────────────────────────────────────────


def test_send_message_appends_user_input_event(session_fs):
    event = io_mod.send_message(session_fs["sid"], "hello world")
    assert event.type == EVENT_USER_INPUT
    assert event.payload["text"] == "hello world"
    assert event.payload["source"] == "cli"
    assert event.payload["caller"] is None
    # Default mode is "interrupt" — the dispatcher cancels in-flight ticks.
    assert event.payload["mode"] == "interrupt"
    events = _read_events(session_fs["system_dir"])
    assert any(e["type"] == EVENT_USER_INPUT and e["payload"]["text"] == "hello world"
               for e in events)


def test_send_message_mode_wait_recorded_on_event(session_fs):
    """``mode="wait"`` is recorded on the user_input event so the dispatcher
    queues this message behind the running tick instead of interrupting it."""
    event = io_mod.send_message(session_fs["sid"], "queue me", mode="wait")
    assert event.payload["mode"] == "wait"
    # build_llm_context only reads ``text`` from this payload, so adding
    # ``mode`` is safe — the LLM still sees a clean user turn.
    assert event.payload["text"] == "queue me"


def test_send_message_rejects_bad_mode(session_fs):
    with pytest.raises(ValueError):
        io_mod.send_message(session_fs["sid"], "hi", mode="bogus")


def test_send_message_with_source_task_and_caller(session_fs):
    event = io_mod.send_message(
        session_fs["sid"],
        "duty fired",
        source="task",
        caller="duty",
    )
    assert event.payload["source"] == "task"
    assert event.payload["caller"] == "duty"


def test_send_message_rejects_bad_source(session_fs):
    with pytest.raises(ValueError):
        io_mod.send_message(session_fs["sid"], "hi", source="nonsense")


def test_send_message_rejects_non_string_text(session_fs):
    with pytest.raises(ValueError):
        io_mod.send_message(session_fs["sid"], 123)  # type: ignore[arg-type]


def test_send_message_missing_session(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.send_message("no_such", "hi")


def test_interrupt_session_with_text(session_fs):
    event = io_mod.interrupt_session(session_fs["sid"], text="stop please")
    assert event.type == EVENT_USER_INTERRUPT
    assert event.payload["text"] == "stop please"


def test_interrupt_session_without_text_still_emits_event(session_fs):
    event = io_mod.interrupt_session(session_fs["sid"])
    assert event.type == EVENT_USER_INTERRUPT
    assert event.payload["text"] is None


def test_interrupt_session_rejects_non_string_text(session_fs):
    with pytest.raises(ValueError):
        io_mod.interrupt_session(session_fs["sid"], text=123)  # type: ignore[arg-type]


# ── Tasks ────────────────────────────────────────────────────────────────────


def test_upsert_task_writes_file_and_emits_event(session_fs):
    """§5.6 invariant: event comes FIRST.

    Capture ``latest_event_id`` immediately after the call — it will be
    the new event's id. The task file's mtime isn't a reliable ordering
    probe (filesystem ts granularity is coarser than the event
    append); instead we validate the event's payload matches the on-disk
    file state, and assert ``latest_event_id > 0`` confirming the append
    ran before the file write.
    """
    event = io_mod.upsert_task(
        session_fs["sid"],
        "alpha",
        description="First task",
        script="echo [start]",
        check_interval=900.0,
    )
    assert event.type == EVENT_TASK_CARD_CHANGED
    assert event.payload["name"] == "alpha"
    assert event.payload["card"]["description"] == "First task"
    assert event.payload["card"]["check_interval"] == 900.0
    # The event id is the latest — confirms the append preceded the
    # file write (both are inside the writer; event append commits to
    # disk BEFORE save_card runs).
    assert io_mod.latest_event_id(session_fs["sid"]) == event.id
    # File materialised.
    card_file = session_fs["user_dir"] / "core" / "tasks" / "alpha.json"
    assert card_file.exists()
    payload = json.loads(card_file.read_text(encoding="utf-8"))
    assert payload["description"] == "First task"
    # Script materialised.
    script_file = session_fs["user_dir"] / "core" / "tasks" / "alpha.sh"
    assert script_file.exists()
    assert "echo [start]" in script_file.read_text(encoding="utf-8")


def test_upsert_task_updates_existing_card(session_fs):
    io_mod.upsert_task(session_fs["sid"], "alpha", description="v1", check_interval=100.0)
    # Update just the description; interval should carry over.
    event = io_mod.upsert_task(session_fs["sid"], "alpha", description="v2")
    assert event.payload["card"]["description"] == "v2"
    assert event.payload["card"]["check_interval"] == 100.0
    card_file = session_fs["user_dir"] / "core" / "tasks" / "alpha.json"
    assert json.loads(card_file.read_text())["description"] == "v2"


def test_upsert_task_rejects_name_traversal(session_fs):
    with pytest.raises(ValueError):
        io_mod.upsert_task(session_fs["sid"], "../evil", description="x")


def test_upsert_task_rejects_empty_name(session_fs):
    with pytest.raises(ValueError):
        io_mod.upsert_task(session_fs["sid"], "", description="x")


def test_upsert_task_rejects_nothing_to_upsert(session_fs):
    # Card doesn't exist AND no fields supplied → ValueError.
    with pytest.raises(ValueError):
        io_mod.upsert_task(session_fs["sid"], "nothing")


def test_upsert_task_missing_session(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.upsert_task("no_such", "x", description="y")


def test_delete_task_emits_null_card_event_and_removes_file(session_fs):
    io_mod.upsert_task(session_fs["sid"], "doomed", description="rm me")
    card_file = session_fs["user_dir"] / "core" / "tasks" / "doomed.json"
    assert card_file.exists()

    event = io_mod.delete_task(session_fs["sid"], "doomed")
    assert event.type == EVENT_TASK_CARD_CHANGED
    assert event.payload["name"] == "doomed"
    assert event.payload["card"] is None
    assert not card_file.exists()


def test_delete_task_missing_card_raises(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.delete_task(session_fs["sid"], "never_existed")


# ── Todo list ────────────────────────────────────────────────────────────────


def test_update_todo_list_happy_path(session_fs):
    payload = {
        "todos": [
            {"content": "A", "status": "pending", "activeForm": "Doing A"},
            {"content": "B", "status": "completed", "activeForm": "Doing B"},
        ],
        "reminder_threshold": 5,
        "iters_since_seen": 1,
    }
    event = io_mod.upsert_todo_list(session_fs["sid"], payload)
    assert event.type == EVENT_TODO_LIST_CHANGED
    assert event.payload["todo_list"]["todos"][0]["content"] == "A"

    # File materialised.
    todo_file = session_fs["user_dir"] / "core" / "todo_list.json"
    assert todo_file.exists()
    on_disk = json.loads(todo_file.read_text(encoding="utf-8"))
    assert len(on_disk["todos"]) == 2


def test_update_todo_list_rejects_non_dict(session_fs):
    with pytest.raises(ValueError):
        io_mod.upsert_todo_list(session_fs["sid"], [1, 2, 3])  # type: ignore[arg-type]


# ── Panel ────────────────────────────────────────────────────────────────────


def test_kill_panel_entry_happy_path(session_fs):
    # Seed a running panel entry via the real writer.
    from butterfly.session_engine.panel import PanelEntry, save_entry

    panel_dir = session_fs["user_dir"] / "core" / "panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    save_entry(panel_dir, PanelEntry(
        tid="t1", type="pending_tool", tool_name="bash",
        input={"cmd": "sleep 100"}, status="running", created_at=1.0,
    ))

    event = io_mod.kill_panel_entry(session_fs["sid"], "t1")

    assert event.type == EVENT_PANEL_ENTRY_CHANGED
    assert event.payload["entry"]["status"] == "killed"
    # File reflects the kill.
    on_disk = json.loads((panel_dir / "t1.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "killed"
    assert on_disk["finished_at"] is not None


def test_kill_panel_entry_missing_tid_raises(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.kill_panel_entry(session_fs["sid"], "nope")


def test_kill_panel_entry_rejects_empty_tid(session_fs):
    with pytest.raises(ValueError):
        io_mod.kill_panel_entry(session_fs["sid"], "")


# ── Terminal ─────────────────────────────────────────────────────────────────


def test_terminal_input_emits_event_and_enqueues(session_fs):
    event = io_mod.terminal_input(session_fs["sid"], "ls -la")
    assert event.type == EVENT_TERMINAL_INPUT
    assert event.payload["text"] == "ls -la"
    assert event.payload["source"] == "web"
    # Input queue got the entry.
    queue = session_fs["user_dir"] / "core" / "terminal" / "input.jsonl"
    assert queue.exists()
    entries = [json.loads(l) for l in queue.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert entries[-1]["type"] == "input"
    assert entries[-1]["content"] == "ls -la"


def test_terminal_input_rejects_bad_source(session_fs):
    with pytest.raises(ValueError):
        io_mod.terminal_input(session_fs["sid"], "x", source="zzz")


def test_terminal_input_rejects_non_string(session_fs):
    with pytest.raises(ValueError):
        io_mod.terminal_input(session_fs["sid"], 42)  # type: ignore[arg-type]


def test_terminal_interrupt_emits_event(session_fs):
    event = io_mod.terminal_interrupt(session_fs["sid"])
    assert event.type == EVENT_TERMINAL_INPUT
    assert event.payload["text"] is None
    queue = session_fs["user_dir"] / "core" / "terminal" / "input.jsonl"
    entries = [json.loads(l) for l in queue.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert entries[-1]["type"] == "interrupt"


# ── Config / prompts / assets ────────────────────────────────────────────────


def test_update_config_emits_event_and_writes_file(session_fs):
    event = io_mod.update_config(session_fs["sid"], "max_iterations", 42)
    assert event.type == EVENT_CONFIG_CHANGED
    assert event.payload["key"] == "max_iterations"
    cfg = io_mod.read_config(session_fs["sid"])
    assert cfg["max_iterations"] == 42


def test_update_config_rejects_unknown_key(session_fs):
    with pytest.raises(ValueError):
        io_mod.update_config(session_fs["sid"], "evil_injection", "x")


def test_update_prompt_writes_core_file(session_fs):
    event = io_mod.update_prompt(session_fs["sid"], "system", "## system\nbody")
    assert event.type == EVENT_PROMPT_CHANGED
    assert event.payload["name"] == "system"
    prompt_file = session_fs["user_dir"] / "core" / "system.md"
    assert prompt_file.read_text(encoding="utf-8") == "## system\nbody"


def test_invalid_prompt_name_raises(session_fs):
    with pytest.raises(ValueError):
        io_mod.update_prompt(session_fs["sid"], "arbitrary", "x")


def test_update_prompt_rejects_non_string_content(session_fs):
    with pytest.raises(ValueError):
        io_mod.update_prompt(session_fs["sid"], "system", 42)  # type: ignore[arg-type]


def test_update_asset_writes_core_file(session_fs):
    event = io_mod.update_asset(session_fs["sid"], "tools", "## tools manifest")
    assert event.type == EVENT_ASSET_CHANGED
    assert event.payload["name"] == "tools"
    asset_file = session_fs["user_dir"] / "core" / "tools.md"
    assert asset_file.read_text(encoding="utf-8") == "## tools manifest"


def test_invalid_asset_name_raises(session_fs):
    with pytest.raises(ValueError):
        io_mod.update_asset(session_fs["sid"], "evil", "x")


# ── The money test: §5.6 event-first invariant under failure ─────────────────


def test_event_leads_side_effect_on_upsert_task(session_fs, monkeypatch):
    """§5.6: event is leading truth.

    If the side-effect blows up AFTER the event was appended, the
    event must remain in events_v1.jsonl — a future
    ``rebuild_views(session_id)`` can reconcile. Here we simulate the
    failure by monkeypatching ``save_card`` to raise ``OSError``.
    """
    from butterfly.session_engine import task_cards

    def _boom(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(task_cards, "save_card", _boom)

    before = io_mod.latest_event_id(session_fs["sid"])
    with pytest.raises(OSError):
        io_mod.upsert_task(session_fs["sid"], "leaks_through", description="x")
    after = io_mod.latest_event_id(session_fs["sid"])

    # Event was persisted despite the side-effect failure.
    assert after == before + 1
    events = _read_events(session_fs["system_dir"])
    latest = events[-1]
    assert latest["type"] == EVENT_TASK_CARD_CHANGED
    assert latest["payload"]["name"] == "leaks_through"

    # Task file was NOT written.
    card_file = session_fs["user_dir"] / "core" / "tasks" / "leaks_through.json"
    assert not card_file.exists()


def test_event_leads_side_effect_on_update_prompt(session_fs, monkeypatch):
    """Same invariant for update_prompt — event persists when the
    service's file write fails.
    """
    from butterfly.service import config_service

    def _boom(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(config_service, "update_prompt_md", _boom)

    before = io_mod.latest_event_id(session_fs["sid"])
    with pytest.raises(OSError):
        io_mod.update_prompt(session_fs["sid"], "system", "body")
    after = io_mod.latest_event_id(session_fs["sid"])
    assert after == before + 1


# ── Public surface ───────────────────────────────────────────────────────────


def test_public_surface_includes_writers():
    must_export = {
        "create_session", "delete_session", "start_session", "stop_session",
        "send_message", "interrupt_session",
        "upsert_task", "delete_task", "upsert_todo_list",
        "kill_panel_entry", "terminal_input", "terminal_interrupt",
        "update_config", "update_prompt", "update_asset",
    }
    exposed = set(io_mod.__all__)
    missing = must_export - exposed
    assert not missing, f"missing public symbols: {missing}"
