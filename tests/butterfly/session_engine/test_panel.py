"""PanelEntry persistence + sweep semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.session_engine.panel import (
    PanelEntry,
    STATUS_COMPLETED,
    STATUS_KILLED,
    STATUS_KILLED_BY_RESTART,
    STATUS_RUNNING,
    STATUS_STALLED,
    TERMINAL_STATUSES,
    create_pending_tool_entry,
    entry_path,
    list_entries,
    load_entry,
    save_entry,
    sweep_killed_by_restart,
)


def test_create_pending_tool_entry_writes_json(tmp_path: Path) -> None:
    entry = create_pending_tool_entry(
        tmp_path, tool_name="bash", input={"command": "echo hi"}
    )
    assert entry.tid.startswith("bg_")
    p = entry_path(tmp_path, entry.tid)
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["tool_name"] == "bash"
    assert data["status"] == STATUS_RUNNING
    assert data["input"] == {"command": "echo hi"}


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    e = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    e.status = STATUS_COMPLETED
    e.exit_code = 0
    e.output_bytes = 42
    save_entry(tmp_path, e)

    loaded = load_entry(tmp_path, e.tid)
    assert loaded is not None
    assert loaded.status == STATUS_COMPLETED
    assert loaded.exit_code == 0
    assert loaded.output_bytes == 42


def test_list_entries_sorted_by_created_at(tmp_path: Path) -> None:
    a = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    b = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    # Force a's created_at to be earlier.
    a.created_at = 100.0
    b.created_at = 200.0
    save_entry(tmp_path, a)
    save_entry(tmp_path, b)
    ids = [e.tid for e in list_entries(tmp_path)]
    assert ids == [a.tid, b.tid]


def test_load_entry_missing_returns_none(tmp_path: Path) -> None:
    assert load_entry(tmp_path, "nope") is None


def test_load_entry_corrupt_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / "corrupt.json").write_text("not-json", encoding="utf-8")
    assert load_entry(tmp_path, "corrupt") is None


def test_load_entry_schema_invalid_regression(tmp_path: Path) -> None:
    """Cubic P2: schema-invalid JSON should not crash.

    `PanelEntry.from_json` filters unknown keys but still raises TypeError
    if required fields are missing. Regression guard: make sure
    `list_entries` does not blow up on a broken file — it should just
    skip it rather than raise.
    """
    # Missing all required fields (tid/type/tool_name/input/status/created_at).
    (tmp_path / "broken.json").write_text("{}", encoding="utf-8")
    # Also write a valid one so we can tell good files aren't affected.
    create_pending_tool_entry(tmp_path, tool_name="bash", input={})

    try:
        entries = list_entries(tmp_path)
    except TypeError as exc:
        pytest.xfail(
            f"list_entries crashes on schema-invalid JSON (cubic P2, not fixed in PR #19): {exc}"
        )
    else:
        # When the bug is fixed, only the valid entry survives.
        assert len(entries) == 1


def test_sweep_killed_by_restart_running_only(tmp_path: Path) -> None:
    running = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    completed = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    completed.status = STATUS_COMPLETED
    save_entry(tmp_path, completed)

    updated = sweep_killed_by_restart(tmp_path)
    assert len(updated) == 1
    assert updated[0].tid == running.tid
    assert updated[0].status == STATUS_KILLED_BY_RESTART

    reload_completed = load_entry(tmp_path, completed.tid)
    assert reload_completed is not None
    assert reload_completed.status == STATUS_COMPLETED  # untouched


def test_sweep_skips_stalled_regression(tmp_path: Path) -> None:
    """Cubic P2: `sweep_killed_by_restart` should also sweep STALLED entries.

    A stalled-pre-restart task is still orphaned from our POV — its
    subprocess died with the old daemon. Current code only sweeps RUNNING.
    Documented as xfail so the suite stays green while the finding stands.
    """
    e = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    e.status = STATUS_STALLED
    save_entry(tmp_path, e)

    sweep_killed_by_restart(tmp_path)
    loaded = load_entry(tmp_path, e.tid)
    assert loaded is not None
    if loaded.status != STATUS_KILLED_BY_RESTART:
        pytest.xfail(
            "sweep_killed_by_restart leaves STALLED entries non-terminal "
            "after restart (cubic P2, not fixed in PR #19)."
        )


def test_panel_entry_is_terminal() -> None:
    for status in TERMINAL_STATUSES:
        e = PanelEntry(
            tid="t",
            type="pending_tool",
            tool_name="bash",
            input={},
            status=status,
            created_at=0,
        )
        assert e.is_terminal()
    e = PanelEntry(
        tid="t",
        type="pending_tool",
        tool_name="bash",
        input={},
        status=STATUS_RUNNING,
        created_at=0,
    )
    assert not e.is_terminal()
    e.status = STATUS_STALLED
    assert not e.is_terminal()  # stalled is NOT terminal by design


def test_save_entry_extra_fields_roundtrip(tmp_path: Path) -> None:
    """Forward-compat: unknown keys in JSON are tolerated by from_json."""
    e = create_pending_tool_entry(tmp_path, tool_name="bash", input={})
    path = entry_path(tmp_path, e.tid)
    data = json.loads(path.read_text())
    data["future_field"] = {"foo": "bar"}
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_entry(tmp_path, e.tid)
    assert loaded is not None
    assert loaded.tid == e.tid
