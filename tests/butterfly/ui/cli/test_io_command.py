"""Phase 6 — tests for ``ui.cli.io_command``.

Covers the ``butterfly io <fn>`` reflection command plus every
net-new alias (interrupt / delete / task-upsert / task-delete /
shell / config-set / prompt-edit). Existing cmd_chat / cmd_new /
cmd_stop / cmd_start / cmd_sessions / cmd_log / cmd_tasks tests
live in ``tests/ui/cli/test_main_commands.py`` and still assert the
legacy UX contract — we don't re-test those here.

Fixtures monkeypatch ``butterfly.runtime.io._SESSIONS_DIR`` /
``_SYSTEM_SESSIONS_DIR`` onto a tmp_path, then build one session via
the real ``sessions_service.create_session`` (which stubs out the
heavy venv build via a conftest fixture). Each alias test invokes
its handler directly with a crafted Namespace — faster than
subprocess + keeps the coverage signal on the handler logic rather
than argparse plumbing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from butterfly.runtime import io as io_mod
from ui.cli import io_command as ioc


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the per-session venv build. Real venv=1s/test — wasteful."""
    from butterfly.session_engine import session_init

    monkeypatch.setattr(
        session_init,
        "_create_session_venv",
        lambda session_dir: session_dir / ".venv",
    )


@pytest.fixture(autouse=True)
def _stub_bridge_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip daemon IPC side-effects; we only exercise the io-write leg."""
    from butterfly.service import messages_service
    from butterfly.service import terminal_service

    monkeypatch.setattr(
        messages_service,
        "send_message",
        lambda session_id, content, system_sessions_dir, **kw: "fake-id",
    )
    monkeypatch.setattr(
        messages_service,
        "interrupt_session",
        lambda session_id, system_sessions_dir: None,
    )
    monkeypatch.setattr(
        terminal_service,
        "enqueue_input",
        lambda *a, **kw: None,
    )


@pytest.fixture
def fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Tmp-path repo layout with one ready-to-use session."""
    sessions = tmp_path / "sessions"
    system = tmp_path / "_sessions"
    archived = tmp_path / "_archived"
    agenthub = tmp_path / "agenthub"
    sessions.mkdir()
    system.mkdir()
    agenthub.mkdir()
    (agenthub / "default").mkdir()
    (agenthub / "default" / "config.yaml").write_text(
        "agent: default\nmodel: m\nprovider: p\n", encoding="utf-8"
    )

    monkeypatch.setattr(io_mod, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(io_mod, "_SYSTEM_SESSIONS_DIR", system)
    monkeypatch.setattr(io_mod, "_ARCHIVED_DIR", archived)

    sid = "sess_01"
    from butterfly.service.sessions_service import create_session as _svc_create

    _svc_create(sid, "default", sessions, system)

    return {"tmp": tmp_path, "sid": sid, "sessions": sessions, "system": system}


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ── Reflection (butterfly io ...) ────────────────────────────────────────────


def test_io_reflect_calls_function(fs, capsys):
    args = _ns(io_function="list_sessions", io_extras=[])
    rc = ioc.cmd_io(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert out and out[0]["id"] == fs["sid"]


def test_io_reflect_list_subcommand_prints_catalog(fs, capsys):
    args = _ns(io_function="list", io_extras=[])
    rc = ioc.cmd_io(args)
    assert rc == 0
    out = capsys.readouterr().out
    # Every public io fn name appears somewhere in the output.
    for name in ("list_sessions", "send_message", "upsert_task", "read_hud"):
        assert name in out


def test_io_reflect_list_when_fn_name_omitted(fs, capsys):
    args = _ns(io_function=None, io_extras=[])
    rc = ioc.cmd_io(args)
    assert rc == 0
    assert "list_sessions" in capsys.readouterr().out


def test_io_reflect_unknown_function_errors(fs, capsys):
    args = _ns(io_function="nope_nope", io_extras=[])
    rc = ioc.cmd_io(args)
    assert rc == 2
    assert "unknown io function" in capsys.readouterr().err


def test_io_reflect_rejects_private_name(fs, capsys):
    # Underscore-prefixed names must not be reachable via reflection.
    args = _ns(io_function="_resolve_session_dir", io_extras=[])
    rc = ioc.cmd_io(args)
    assert rc == 2
    assert "unknown io function" in capsys.readouterr().err


def test_io_reflect_passes_session_id_positional(fs, capsys):
    args = _ns(io_function="get_session", io_extras=[fs["sid"]])
    rc = ioc.cmd_io(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == fs["sid"]


def test_io_reflect_parses_json_kwargs(fs, capsys, monkeypatch):
    captured = {}

    def fake_read_events(session_id, *, since_id=None, until_id=None, types=None):
        captured["since_id"] = since_id
        return iter([])

    monkeypatch.setattr(io_mod, "read_events", fake_read_events)
    args = _ns(io_function="read_events", io_extras=[fs["sid"], "--since_id=10"])
    rc = ioc.cmd_io(args)
    assert rc == 0
    assert captured["since_id"] == 10
    assert isinstance(captured["since_id"], int)


def test_io_reflect_coerces_event_result(fs, capsys):
    # send_message returns an Event — reflection should .to_dict() it.
    args = _ns(
        io_function="send_message",
        io_extras=[fs["sid"], "--text=hello"],
    )
    rc = ioc.cmd_io(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["type"] == "user_input"
    assert out["payload"]["text"] == "hello"


def test_io_reflect_materializes_generator(fs, capsys):
    # read_events returns Iterator[Event] — must be materialised to a
    # list in JSON output.
    # First send a message so there's something to read.
    io_mod.send_message(fs["sid"], "ping", source="cli")
    args = _ns(io_function="read_events", io_extras=[fs["sid"]])
    rc = ioc.cmd_io(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert any(ev["type"] == "user_input" for ev in out)


def test_io_reflect_bad_arity_errors(fs, capsys):
    # Missing required arg (name) for upsert_task.
    args = _ns(io_function="upsert_task", io_extras=[fs["sid"]])
    rc = ioc.cmd_io(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad arguments" in err


def test_io_reflect_converts_bool_flag(fs, capsys):
    """Bare --archived becomes bool True; passed to list_sessions."""
    args = _ns(io_function="list_sessions", io_extras=["--include_archived"])
    rc = ioc.cmd_io(args)
    assert rc == 0  # no archived dir → empty + live only, but no crash
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)


# ── parse_cli_value primitives ────────────────────────────────────────────


def test_parse_cli_value_json_int():
    assert ioc._parse_cli_value("10") == 10


def test_parse_cli_value_json_bool():
    assert ioc._parse_cli_value("true") is True
    assert ioc._parse_cli_value("false") is False


def test_parse_cli_value_json_null():
    assert ioc._parse_cli_value("null") is None


def test_parse_cli_value_fallback_string():
    assert ioc._parse_cli_value("hello") == "hello"


def test_parse_cli_value_empty_stays_empty():
    assert ioc._parse_cli_value("") == ""


# ── _coerce_result ──────────────────────────────────────────────────────────


def test_coerce_primitives_pass_through():
    assert ioc._coerce_result(None) is None
    assert ioc._coerce_result(42) == 42
    assert ioc._coerce_result("hi") == "hi"
    assert ioc._coerce_result(True) is True


def test_coerce_dict_recurses():
    ev = io_mod.Event(id=1, ts=1.0, type="x", for_llm=False, payload={"a": 1})
    out = ioc._coerce_result({"event": ev})
    assert out == {"event": {"id": 1, "ts": 1.0, "type": "x", "for_llm": False, "payload": {"a": 1}}}


def test_coerce_generator():
    def gen():
        yield 1
        yield 2
    assert ioc._coerce_result(gen()) == [1, 2]


# ── interrupt alias ─────────────────────────────────────────────────────────


def test_interrupt_alias_without_text(fs, capsys):
    args = _ns(session_id=fs["sid"], text=None)
    rc = ioc.cmd_alias_interrupt(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"interrupted {fs['sid']}" in out
    events = list(io_mod.read_events(fs["sid"]))
    assert any(ev.type == "user_interrupt" for ev in events)


def test_interrupt_alias_with_text(fs, capsys):
    args = _ns(session_id=fs["sid"], text="stop and do X")
    rc = ioc.cmd_alias_interrupt(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "+text" in out
    events = list(io_mod.read_events(fs["sid"]))
    matching = [ev for ev in events if ev.type == "user_interrupt"]
    assert matching and matching[0].payload["text"] == "stop and do X"


def test_interrupt_alias_unknown_session_errors(fs, capsys):
    args = _ns(session_id="not_a_session", text=None)
    rc = ioc.cmd_alias_interrupt(args)
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ── delete alias ────────────────────────────────────────────────────────────


def test_delete_requires_yes_flag(fs, capsys):
    args = _ns(session_id=fs["sid"], yes=False)
    rc = ioc.cmd_alias_delete(args)
    assert rc == 2
    assert "--yes" in capsys.readouterr().err
    # Session still exists.
    assert (fs["system"] / fs["sid"]).is_dir()


def test_delete_with_yes_removes_session(fs, capsys):
    args = _ns(session_id=fs["sid"], yes=True)
    rc = ioc.cmd_alias_delete(args)
    assert rc == 0
    assert f"deleted {fs['sid']}" in capsys.readouterr().out
    assert not (fs["system"] / fs["sid"]).is_dir()


# ── task-upsert / task-delete ──────────────────────────────────────────────


def test_task_upsert_calls_io_upsert_task(fs, capsys):
    args = _ns(
        session_id=fs["sid"], name="my_task",
        description="hi", script="echo [start]",
        check_interval=60.0, notes="note-body", progress="p1",
    )
    rc = ioc.cmd_alias_task_upsert(args)
    assert rc == 0
    cards = io_mod.read_task_cards(fs["sid"])
    names = [c["name"] for c in cards]
    assert "my_task" in names
    card = next(c for c in cards if c["name"] == "my_task")
    assert card["description"] == "hi"
    # task_cards.write_script prepends the shebang + trailing newline.
    # We assert the body we supplied is PRESENT, not equality.
    assert "echo [start]" in (card["script"] or "")
    assert card["check_interval"] == 60.0


def test_task_upsert_nothing_to_do_errors(fs, capsys):
    args = _ns(
        session_id=fs["sid"], name="empty",
        description=None, script=None,
        check_interval=None, notes=None, progress=None,
    )
    rc = ioc.cmd_alias_task_upsert(args)
    assert rc == 2


def test_task_delete_calls_io_delete_task(fs, capsys):
    # Seed a task first.
    io_mod.upsert_task(fs["sid"], "goodbye", description="to remove")
    args = _ns(session_id=fs["sid"], name="goodbye", yes=True)
    rc = ioc.cmd_alias_task_delete(args)
    assert rc == 0
    cards = io_mod.read_task_cards(fs["sid"])
    assert all(c["name"] != "goodbye" for c in cards)


def test_task_delete_requires_yes(fs, capsys):
    io_mod.upsert_task(fs["sid"], "keeper", description="d")
    args = _ns(session_id=fs["sid"], name="keeper", yes=False)
    rc = ioc.cmd_alias_task_delete(args)
    assert rc == 2
    assert "--yes" in capsys.readouterr().err
    cards = io_mod.read_task_cards(fs["sid"])
    assert any(c["name"] == "keeper" for c in cards)


# ── shell alias ─────────────────────────────────────────────────────────────


def test_shell_alias_calls_terminal_input(fs, capsys):
    args = _ns(session_id=fs["sid"], text="ls -la")
    rc = ioc.cmd_alias_shell(args)
    assert rc == 0
    assert "ok" in capsys.readouterr().out
    events = list(io_mod.read_events(fs["sid"]))
    match = [ev for ev in events if ev.type == "terminal_input"]
    assert match and match[0].payload["text"] == "ls -la"
    assert match[0].payload["source"] == "cli"


# ── config-set alias ────────────────────────────────────────────────────────


def test_config_set_parses_json_value(fs, capsys):
    # "gpt-5.4" is not valid JSON → stays string.
    args = _ns(session_id=fs["sid"], key="model", value="gpt-5.4")
    rc = ioc.cmd_alias_config_set(args)
    assert rc == 0
    conf = io_mod.read_config(fs["sid"])
    assert conf["model"] == "gpt-5.4"


def test_config_set_unknown_key_errors(fs, capsys):
    args = _ns(session_id=fs["sid"], key="definitely_not_a_key", value="x")
    rc = ioc.cmd_alias_config_set(args)
    assert rc == 2
    assert "unknown key" in capsys.readouterr().err


# ── prompt-edit alias ───────────────────────────────────────────────────────


def test_prompt_edit_roundtrips(fs, monkeypatch, capsys):
    # Seed a prompt file.
    io_mod.update_prompt(fs["sid"], "system", "original body\n")
    # Mock the editor to append a marker.
    captured = {"path": None}

    def fake_run(cmd, **kw):
        path = cmd[-1]
        captured["path"] = path
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("APPENDED\n")
        class _R: returncode = 0
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("EDITOR", "fake-editor")

    args = _ns(session_id=fs["sid"], name="system")
    rc = ioc.cmd_alias_prompt_edit(args)
    assert rc == 0
    assert "updated" in capsys.readouterr().out
    assert io_mod.read_prompt(fs["sid"], "system") == "original body\nAPPENDED\n"


def test_prompt_edit_no_changes(fs, monkeypatch, capsys):
    io_mod.update_prompt(fs["sid"], "task", "seed\n")

    def fake_run(cmd, **kw):
        class _R: returncode = 0
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    args = _ns(session_id=fs["sid"], name="task")
    rc = ioc.cmd_alias_prompt_edit(args)
    assert rc == 0
    assert "no changes" in capsys.readouterr().out


def test_prompt_edit_bad_name_errors(fs, monkeypatch, capsys):
    args = _ns(session_id=fs["sid"], name="definitely_invalid")
    rc = ioc.cmd_alias_prompt_edit(args)
    assert rc == 2
    assert "unknown prompt" in capsys.readouterr().err


# ── sessions / tasks pretty output ──────────────────────────────────────────


def test_sessions_alias_pretty_output(fs, capsys):
    args = _ns(archived=False, as_json=False)
    rc = ioc.cmd_alias_sessions(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ID" in out and "AGENT" in out and "STATUS" in out
    assert fs["sid"] in out


def test_sessions_alias_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(io_mod, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(io_mod, "_SYSTEM_SESSIONS_DIR", tmp_path / "_sessions")
    monkeypatch.setattr(io_mod, "_ARCHIVED_DIR", tmp_path / "_archived")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "_sessions").mkdir()

    args = _ns(archived=False, as_json=False)
    rc = ioc.cmd_alias_sessions(args)
    assert rc == 0
    assert "No sessions found" in capsys.readouterr().out


def test_tasks_alias_pretty_output(fs, capsys):
    io_mod.upsert_task(fs["sid"], "foo", description="the foo task\nwith detail")
    args = _ns(session_id=fs["sid"], as_json=False)
    rc = ioc.cmd_alias_tasks(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "foo" in out
    assert "the foo task" in out


# ── log alias ───────────────────────────────────────────────────────────────


def test_log_alias_basic(fs, capsys):
    io_mod.send_message(fs["sid"], "hello from log test", source="cli")
    args = _ns(session_id=fs["sid"], num=10, since=None, watch=False)
    rc = ioc.cmd_alias_log(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "user_input" in out
    assert "hello from log test" in out


def test_log_alias_n_filter(fs, capsys):
    for i in range(5):
        io_mod.send_message(fs["sid"], f"msg-{i}", source="cli")
    args = _ns(session_id=fs["sid"], num=2, since=None, watch=False)
    rc = ioc.cmd_alias_log(args)
    assert rc == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    # At most 2 lines because of -n=2.
    assert len(lines) <= 2


def test_log_alias_since_filters(fs, capsys):
    io_mod.send_message(fs["sid"], "first", source="cli")
    after = io_mod.latest_event_id(fs["sid"])
    io_mod.send_message(fs["sid"], "second", source="cli")

    args = _ns(session_id=fs["sid"], num=None, since=after, watch=False)
    rc = ioc.cmd_alias_log(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "second" in out
    assert "first" not in out


def test_log_alias_no_session_fallback(fs, capsys):
    io_mod.send_message(fs["sid"], "x", source="cli")
    args = _ns(session_id=None, num=1, since=None, watch=False)
    rc = ioc.cmd_alias_log(args)
    assert rc == 0


def test_log_alias_watch_mode(fs, capsys, monkeypatch):
    """Watch mode: poll once then Ctrl+C via a KeyboardInterrupt sleep."""
    io_mod.send_message(fs["sid"], "initial", source="cli")

    call_count = {"n": 0}

    def fake_sleep(_secs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Between polls: simulate a new event from another process.
            io_mod.send_message(fs["sid"], "while-watching", source="cli")
            return None
        raise KeyboardInterrupt()

    monkeypatch.setattr(ioc.time, "sleep", fake_sleep)
    args = _ns(session_id=fs["sid"], num=None, since=None, watch=True)
    rc = ioc.cmd_alias_log(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "initial" in out
    assert "while-watching" in out


# ── register_io_commands wiring ─────────────────────────────────────────────


def test_register_io_commands_wires_all_new_aliases():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    ioc.register_io_commands(sub)
    # Every new alias must be reachable by name.
    names = set(sub.choices.keys())
    for alias in (
        "io", "interrupt", "delete", "task-upsert",
        "task-delete", "shell", "config-set", "prompt-edit",
    ):
        assert alias in names


def test_new_alias_session_id_validation(fs, capsys):
    """Bad session id → sibling io function raises ValueError → exit 2."""
    args = _ns(session_id="has/slash", text=None)
    rc = ioc.cmd_alias_interrupt(args)
    assert rc == 2
    assert "Invalid session_id" in capsys.readouterr().err
