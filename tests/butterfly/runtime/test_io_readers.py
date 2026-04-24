"""Tests for butterfly.runtime.io — Phase 4 reader surface.

Covers every public reader from DESIGN.md §5.4. The fixture
``session_fs`` builds a minimal session directory pair (system + user)
under ``tmp_path`` and monkeypatches ``io._SESSIONS_DIR`` /
``io._SYSTEM_SESSIONS_DIR`` so the readers resolve to the tmp layout.

The "display history" tests lock in the events-v1 invariant: no
position-pairing, no re-derivation — the events ARE the UI transcript.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.runtime import events as events_mod
from butterfly.runtime import io as io_mod
from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_THINKING,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_ASSET_CHANGED,
    EVENT_CONFIG_CHANGED,
    EVENT_CONTROL_INTERRUPT,
    EVENT_CONTROL_START,
    EVENT_ERROR,
    EVENT_LLM_CALL_USAGE,
    EVENT_MODEL_STATUS,
    EVENT_PANEL_ENTRY_CHANGED,
    EVENT_PROMPT_CHANGED,
    EVENT_SESSION_CREATED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    EVENT_SUB_AGENT_COUNT,
    EVENT_SYSTEM_NOTICE,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TASK_FINISHED,
    EVENT_TASK_SCRIPT_CHECK,
    EVENT_TERMINAL_LOG,
    EVENT_TERMINAL_STATE,
    EVENT_TODO_LIST_CHANGED,
    EVENT_TOOL_PROGRESS,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
    append_event,
)


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def session_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build a repo-layout fixture with one ready-to-use session.

    Returns a dict carrying the tmp roots and the session id so tests
    can build further state without re-typing paths.
    """
    sessions_root = tmp_path / "sessions"
    system_root = tmp_path / "_sessions"
    archived_root = tmp_path / "_archived"
    sessions_root.mkdir()
    system_root.mkdir()
    # archived is deliberately not created by default — tests that need
    # it create it themselves.

    # Monkeypatch the module-level constants every reader consults.
    monkeypatch.setattr(io_mod, "_SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(io_mod, "_SYSTEM_SESSIONS_DIR", system_root)
    monkeypatch.setattr(io_mod, "_ARCHIVED_DIR", archived_root)

    sid = "sess_01"
    user_dir = sessions_root / sid
    system_dir = system_root / sid
    (user_dir / "core" / "tasks").mkdir(parents=True)
    (user_dir / "core" / "panel").mkdir(parents=True)
    (user_dir / "core" / "terminal").mkdir(parents=True)
    system_dir.mkdir()

    # Minimal manifest so sessions_service.get_session resolves.
    (system_dir / "manifest.json").write_text(
        json.dumps({
            "agent": "agent",
            "created_at": "2026-04-23T00:00:00",
            "display_name": "Sess One",
        }),
        encoding="utf-8",
    )
    # Minimal config so config_service.get_config passes the "both dirs
    # must exist" guard.
    (user_dir / "core" / "config.yaml").write_text("agent: agent\n", encoding="utf-8")

    return {
        "tmp": tmp_path,
        "sessions_root": sessions_root,
        "system_root": system_root,
        "archived_root": archived_root,
        "sid": sid,
        "user_dir": user_dir,
        "system_dir": system_dir,
    }


def _append(system_dir: Path, event_type: str, payload: dict, **kw) -> None:
    append_event(system_dir, event_type, payload, **kw)


# ── list_sessions / get_session / get_status ─────────────────────────────────


def test_list_sessions_returns_live(session_fs):
    out = io_mod.list_sessions()
    assert len(out) == 1
    assert out[0]["id"] == session_fs["sid"]


def test_list_sessions_returns_live_and_archived_when_requested(session_fs):
    # Create a second session in the archive tree with the layout that
    # sessions_service.list_sessions understands (manifest.json at root).
    archived = session_fs["archived_root"]
    archived.mkdir()
    (archived / "old_sess").mkdir()
    (archived / "old_sess" / "manifest.json").write_text(
        json.dumps({"agent": "agent", "created_at": "2026-04-01T00:00:00"}),
        encoding="utf-8",
    )
    # Point _SESSIONS_DIR/_SYSTEM_SESSIONS_DIR stay on live tree (already
    # set by fixture); archive dir now exists.
    live_only = io_mod.list_sessions(include_archived=False)
    with_archived = io_mod.list_sessions(include_archived=True)
    assert len(live_only) == 1
    assert len(with_archived) == 2
    # The archived entry is marked with archived=True.
    archived_infos = [s for s in with_archived if s.get("archived")]
    assert len(archived_infos) == 1
    assert archived_infos[0]["id"] == "old_sess"


def test_get_session_happy_path(session_fs):
    info = io_mod.get_session(session_fs["sid"])
    assert info["id"] == session_fs["sid"]
    assert info["agent"] == "agent"
    assert info["display_name"] == "Sess One"


def test_get_session_raises_on_missing_id(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.get_session("does_not_exist")


def test_get_session_raises_on_invalid_id(session_fs):
    with pytest.raises(ValueError):
        io_mod.get_session("bad/id!")


def test_get_status_returns_defaults_when_no_status_file(session_fs):
    st = io_mod.get_status(session_fs["sid"])
    assert st["status"] == "active"
    assert st["model_state"] == "idle"


def test_get_status_raises_on_missing(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.get_status("no_such")


# ── read_events / latest_event_id ────────────────────────────────────────────


def test_read_events_delegates_to_primitive(session_fs):
    system_dir = session_fs["system_dir"]
    _append(system_dir, EVENT_USER_INPUT, {"text": "hi"})
    _append(system_dir, EVENT_AGENT_TEXT, {"text": "hello", "model": "m"})

    out = list(io_mod.read_events(session_fs["sid"]))
    assert [e.type for e in out] == [EVENT_USER_INPUT, EVENT_AGENT_TEXT]
    # since_id/types forwarding
    only_text = list(io_mod.read_events(session_fs["sid"], types=[EVENT_AGENT_TEXT]))
    assert len(only_text) == 1 and only_text[0].type == EVENT_AGENT_TEXT


def test_latest_event_id_zero_when_empty(session_fs):
    assert io_mod.latest_event_id(session_fs["sid"]) == 0


def test_latest_event_id_after_appends(session_fs):
    _append(session_fs["system_dir"], EVENT_USER_INPUT, {"text": "hi"})
    _append(session_fs["system_dir"], EVENT_AGENT_TEXT, {"text": "y", "model": "m"})
    assert io_mod.latest_event_id(session_fs["sid"]) == 2


def test_read_events_missing_session(session_fs):
    with pytest.raises(FileNotFoundError):
        list(io_mod.read_events("nope"))


# ── read_llm_context ─────────────────────────────────────────────────────────


def test_read_llm_context_filters_for_llm_and_groups_roles(session_fs):
    sd = session_fs["system_dir"]
    # User → user
    _append(sd, EVENT_USER_INPUT, {"text": "hi"})
    # Model status slips in — MUST be skipped (for_llm=False).
    _append(sd, EVENT_MODEL_STATUS, {"status": "running", "model": "m"})
    # Assistant text + thinking → same message, both blocks.
    _append(sd, EVENT_AGENT_THINKING, {"text": "think"})
    _append(sd, EVENT_AGENT_TEXT, {"text": "hello", "model": "m"})
    # Tool call → same assistant message
    _append(sd, EVENT_AGENT_TOOL_CALL, {"tool_use_id": "t1", "tool_name": "bash", "args": {}})
    # Tool result → new user message
    _append(sd, EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "t1", "tool_name": "bash", "result": "ok",
        "is_error": False, "is_background": False, "duration_ms": 1.0,
    })

    msgs = io_mod.read_llm_context(session_fs["sid"])
    # Three messages: user, assistant, user(tool_result).
    assert [m.role for m in msgs] == ["user", "assistant", "user"]
    # Assistant message has thinking + text + tool_use in that order.
    assert [b.type for b in msgs[1].content] == ["thinking", "text", "tool_use"]
    # Tool result message has one tool_result block.
    assert msgs[2].content[0].type == "tool_result"


# ── read_display_history ─────────────────────────────────────────────────────


def test_read_display_history_returns_chronological_ui_events(session_fs):
    sd = session_fs["system_dir"]
    # Mix: user input (for_llm), model_status (UI-visible system),
    # agent_text (for_llm), llm_call_usage (UI-visible system),
    # and a control_start (plumbing) which MUST be dropped.
    _append(sd, EVENT_USER_INPUT, {"text": "hi"})
    _append(sd, EVENT_MODEL_STATUS, {"status": "running", "model": "m"})
    _append(sd, EVENT_AGENT_TEXT, {"text": "hello", "model": "m"})
    _append(sd, EVENT_LLM_CALL_USAGE, {"iteration": 1, "usage": {},
                                        "context_tokens": 100, "toks_per_s": 1.0,
                                        "duration_ms": 100})
    _append(sd, EVENT_CONTROL_START, {})

    hist = io_mod.read_display_history(session_fs["sid"])
    types = [e.type for e in hist]
    assert types == [
        EVENT_USER_INPUT,
        EVENT_MODEL_STATUS,
        EVENT_AGENT_TEXT,
        EVENT_LLM_CALL_USAGE,
    ]
    # Chronological by id — implicit since read_events preserves order.
    assert [e.id for e in hist] == sorted(e.id for e in hist)


def test_read_display_history_ignores_internal_system_events(session_fs):
    sd = session_fs["system_dir"]
    # Every event here is a "plumbing" system event that MUST NOT reach
    # the UI transcript.
    _append(sd, EVENT_SESSION_CREATED, {"manifest": {}})
    _append(sd, EVENT_SESSION_STARTED, {})
    _append(sd, EVENT_SESSION_STOPPED, {"reason": "done"})
    _append(sd, EVENT_CONTROL_INTERRUPT, {"text": None})
    _append(sd, EVENT_CONTROL_START, {})
    # And one UI-visible system event we DO expect to pass through.
    _append(sd, EVENT_TASK_CARD_CHANGED, {"name": "duty", "card": {}})

    hist = io_mod.read_display_history(session_fs["sid"])
    assert [e.type for e in hist] == [EVENT_TASK_CARD_CHANGED]


def test_read_display_history_since_id_filter(session_fs):
    sd = session_fs["system_dir"]
    _append(sd, EVENT_USER_INPUT, {"text": "one"})
    _append(sd, EVENT_USER_INPUT, {"text": "two"})
    _append(sd, EVENT_USER_INPUT, {"text": "three"})
    hist = io_mod.read_display_history(session_fs["sid"], since_id=1)
    assert [e.id for e in hist] == [2, 3]


# ── read_task_cards ──────────────────────────────────────────────────────────


def test_read_task_cards_happy_path(session_fs):
    tasks_dir = session_fs["user_dir"] / "core" / "tasks"
    # Seed two cards. Use the real writer so JSON shape is canonical.
    from butterfly.session_engine.task_cards import TaskCard, save_card, write_script

    save_card(tasks_dir, TaskCard(name="duty", description="Duty card", check_interval=7200.0))
    save_card(tasks_dir, TaskCard(name="alpha", description="Alpha", check_interval=3600.0))
    write_script(tasks_dir, "alpha", "echo [skip]")

    out = io_mod.read_task_cards(session_fs["sid"])
    assert [c["name"] for c in out] == ["duty", "alpha"]  # duty first
    # Alpha carries its script body; duty has None.
    alpha = next(c for c in out if c["name"] == "alpha")
    assert "echo [skip]" in alpha["script"]
    duty = next(c for c in out if c["name"] == "duty")
    assert duty["script"] is None


def test_read_task_cards_empty_when_no_tasks_dir(session_fs):
    # Fresh session — tasks/ exists but empty.
    assert io_mod.read_task_cards(session_fs["sid"]) == []


def test_read_task_cards_missing_session(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.read_task_cards("nope")


# ── read_todo_list ───────────────────────────────────────────────────────────


def test_read_todo_list_happy_path(session_fs):
    from butterfly.session_engine.todo_list import TodoList, save_todo_list

    core_dir = session_fs["user_dir"] / "core"
    save_todo_list(core_dir, TodoList(
        todos=[
            {"content": "Do one", "status": "completed", "activeForm": "Doing one"},
            {"content": "Do two", "status": "in_progress", "activeForm": "Doing two"},
        ],
    ))
    out = io_mod.read_todo_list(session_fs["sid"])
    assert out is not None
    assert out["total"] == 2
    assert out["items"][0]["status"] == "completed"


def test_read_todo_list_none_when_empty(session_fs):
    # No todo_list.json → None.
    assert io_mod.read_todo_list(session_fs["sid"]) is None


# ── read_config / read_prompt / read_asset ───────────────────────────────────


def test_read_config_happy_path(session_fs):
    # The fixture writes ``agent: agent`` into core/config.yaml.
    cfg = io_mod.read_config(session_fs["sid"])
    assert cfg["agent"] == "agent"
    # Merged with DEFAULT_CONFIG.
    assert "max_iterations" in cfg
    # is_meta_session flag surfaced (fixture sid does NOT end with _meta).
    assert cfg["is_meta_session"] is False


def test_read_prompt_happy_path(session_fs):
    # Write a prompt under core/system.md and read it back.
    (session_fs["user_dir"] / "core" / "system.md").write_text(
        "# system prompt\nBody.", encoding="utf-8"
    )
    assert io_mod.read_prompt(session_fs["sid"], "system").startswith("# system prompt")


def test_read_prompt_rejects_unknown_name(session_fs):
    with pytest.raises(ValueError):
        io_mod.read_prompt(session_fs["sid"], "arbitrary")


def test_read_asset_happy_path(session_fs):
    # Write tools.md and read it back.
    (session_fs["user_dir"] / "core" / "tools.md").write_text(
        "## tool manifest", encoding="utf-8"
    )
    assert io_mod.read_asset(session_fs["sid"], "tools") == "## tool manifest"


def test_read_asset_rejects_unknown_name(session_fs):
    with pytest.raises(ValueError):
        io_mod.read_asset(session_fs["sid"], "nope")


# ── read_hud ─────────────────────────────────────────────────────────────────


def test_read_hud_minimal(session_fs):
    # Seed one llm_call_usage via the legacy events.jsonl path since the
    # HUD service still reads from there today (see Phase 7 TODO).
    legacy_events = session_fs["system_dir"] / "events.jsonl"
    legacy_events.write_text(
        json.dumps({
            "type": "llm_call_usage",
            "context_tokens": 1234,
            "toks_per_s": 42.0,
            "ts": "2026-04-23T00:00:00",
        }) + "\n",
        encoding="utf-8",
    )
    hud = io_mod.read_hud(session_fs["sid"])
    # Only check fields every session must populate — the rest depend
    # on git/shell state.
    assert "cwd" in hud
    assert hud["context_tokens"] == 1234
    assert hud["toks_per_s"] == 42.0
    assert hud["sub_agents_running"] == 0
    assert hud["bash_running"] == 0


def test_read_hud_missing_session(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.read_hud("no_such_session")


# ── read_panel / read_panel_entry ────────────────────────────────────────────


def test_read_panel_empty_when_no_entries(session_fs):
    assert io_mod.read_panel(session_fs["sid"]) == []


def test_read_panel_returns_entries(session_fs):
    from butterfly.session_engine.panel import PanelEntry, save_entry

    panel_dir = session_fs["user_dir"] / "core" / "panel"
    save_entry(panel_dir, PanelEntry(
        tid="t1", type="pending_tool", tool_name="bash",
        input={"cmd": "ls"}, status="running", created_at=1.0,
    ))
    out = io_mod.read_panel(session_fs["sid"])
    assert len(out) == 1
    assert out[0]["tid"] == "t1"


def test_read_panel_entry_happy(session_fs):
    from butterfly.session_engine.panel import PanelEntry, save_entry

    panel_dir = session_fs["user_dir"] / "core" / "panel"
    save_entry(panel_dir, PanelEntry(
        tid="t2", type="pending_tool", tool_name="bash",
        input={}, status="completed", created_at=1.0,
    ))
    out = io_mod.read_panel_entry(session_fs["sid"], "t2")
    assert out["tid"] == "t2"


def test_read_panel_entry_missing_raises(session_fs):
    with pytest.raises(FileNotFoundError):
        io_mod.read_panel_entry(session_fs["sid"], "no_tid")


# ── read_terminal_state / read_terminal_log ──────────────────────────────────


def test_read_terminal_state_when_no_terminal(session_fs):
    st = io_mod.read_terminal_state(session_fs["sid"])
    assert st["active"] is False
    assert st["cwd"] is None


def test_read_terminal_state_reads_file(session_fs):
    term = session_fs["user_dir"] / "core" / "terminal"
    (term / "state.json").write_text(
        json.dumps({"active": True, "cwd": "/tmp"}), encoding="utf-8"
    )
    st = io_mod.read_terminal_state(session_fs["sid"])
    assert st["active"] is True
    assert st["cwd"] == "/tmp"


def test_read_terminal_log_empty(session_fs):
    assert io_mod.read_terminal_log(session_fs["sid"]) == []


def test_read_terminal_log_tail(session_fs):
    term = session_fs["user_dir"] / "core" / "terminal"
    with (term / "log.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1, "text": "one"}) + "\n")
        f.write(json.dumps({"ts": 2, "text": "two"}) + "\n")
    entries = io_mod.read_terminal_log(session_fs["sid"])
    assert [e["text"] for e in entries] == ["one", "two"]


# ── list_models / list_agents ────────────────────────────────────────────────


def test_list_models_returns_catalog(session_fs):
    # models.yaml ships with the repo; list_models reads it directly
    # (no monkeypatching needed since the path is relative to the module).
    providers = io_mod.list_models()
    assert isinstance(providers, list)
    assert providers  # at least one provider shipped
    assert all("provider" in p for p in providers)
    assert all("models" in p for p in providers)


def test_list_agents_returns_catalog(session_fs, monkeypatch):
    # io_mod._REPO_ROOT points at the real repo, which has agenthub/.
    agents = io_mod.list_agents()
    assert isinstance(agents, list)
    # agenthub/agent ships a config.yaml so it must appear.
    assert "agent" in agents


# ── Public surface ───────────────────────────────────────────────────────────


def test_public_surface_matches_design(session_fs):
    """Spot-check the exposed names — catches silent drift."""
    must_export = {
        "list_sessions", "get_session", "get_status",
        "read_events", "latest_event_id",
        "read_llm_context", "read_display_history",
        "read_hud", "read_task_cards", "read_todo_list",
        "read_config", "read_prompt", "read_asset",
        "read_panel", "read_panel_entry",
        "read_terminal_state", "read_terminal_log",
        "list_models", "list_agents",
    }
    exposed = set(io_mod.__all__)
    missing = must_export - exposed
    assert not missing, f"missing public symbols: {missing}"
