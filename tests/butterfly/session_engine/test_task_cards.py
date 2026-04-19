"""Tests for butterfly.session_engine.task_cards (v2.0.27 bash-driven)."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.session_engine.task_cards import (
    TaskCard,
    cards_needing_check,
    clear_all_cards,
    ensure_card,
    end_script_path,
    has_pending_cards,
    load_all_cards,
    load_card,
    parse_end_output,
    parse_trigger_output,
    read_end_script,
    read_trigger_script,
    save_card,
    trigger_script_path,
    write_end_script,
    write_trigger_script,
)


class TaskCardsUnitTests(unittest.TestCase):
    def test_ensure_card_keeps_existing(self) -> None:
        with TemporaryDirectory() as td:
            tasks_dir = Path(td)
            created = ensure_card(
                tasks_dir,
                name="duty",
                check_interval=60,
                description="first",
            )
            created.description = "customized"
            save_card(tasks_dir, created)

            loaded = ensure_card(
                tasks_dir,
                name="duty",
                check_interval=120,
                description="second",
            )

        self.assertEqual(loaded.description, "customized")
        self.assertEqual(loaded.check_interval, 60)

    def test_ensure_card_writes_default_trigger(self) -> None:
        with TemporaryDirectory() as td:
            tasks_dir = Path(td)
            ensure_card(tasks_dir, name="duty", check_interval=60)
            body = read_trigger_script(tasks_dir, "duty") or ""
        self.assertIn("echo [start]", body)


# ── needs_check cadence ─────────────────────────────────────────────────


def test_needs_check_first_call(tmp_path):
    card = TaskCard(name="t", description="x", check_interval=60)
    assert card.needs_check() is True


def test_needs_check_throttles(tmp_path):
    card = TaskCard(name="t", description="x", check_interval=60)
    card.mark_checked(datetime.now())
    assert card.needs_check() is False


def test_needs_check_fires_after_interval(tmp_path):
    card = TaskCard(
        name="t",
        description="x",
        check_interval=30,
        last_checked_at=(datetime.now() - timedelta(seconds=60)).isoformat(),
    )
    assert card.needs_check() is True


def test_needs_check_skips_non_pending(tmp_path):
    for status in ("working", "finished", "paused"):
        card = TaskCard(name="t", description="x", status=status)
        assert card.needs_check() is False


def test_end_check_only_while_working(tmp_path):
    pending = TaskCard(name="t", status="pending")
    assert pending.end_check_due() is False
    working = TaskCard(name="t", status="working")
    assert working.end_check_due() is True


# ── mark_* transitions ──────────────────────────────────────────────────


def test_mark_working():
    card = TaskCard(name="test", description="x")
    card.mark_working()
    assert card.status == "working"
    assert card.last_started_at is not None


def test_mark_finished_returns_pending():
    """v2.0.27: mark_finished → pending (trigger script decides re-fire)."""
    card = TaskCard(name="test", description="x", check_interval=600)
    card.mark_finished()
    assert card.status == "pending"
    assert card.last_finished_at is not None


def test_mark_terminal_is_sticky():
    card = TaskCard(name="test")
    card.mark_terminal()
    assert card.status == "finished"


def test_mark_paused():
    card = TaskCard(name="test", description="x", status="working")
    card.mark_paused()
    assert card.status == "paused"


# ── Serialization round-trip ────────────────────────────────────────────


def test_serialize_and_parse_roundtrip(tmp_path):
    card = TaskCard(
        name="my_task",
        description="Do something important",
        check_interval=3600,
        status="paused",
        created_at="2026-04-08T12:00:00",
    )
    path = save_card(tmp_path, card)
    assert path == tmp_path / "my_task.json"
    assert path.exists()

    loaded = load_card(tmp_path, "my_task")
    assert loaded is not None
    assert loaded.name == "my_task"
    assert loaded.description == "Do something important"
    assert loaded.check_interval == 3600
    assert loaded.status == "paused"
    assert loaded.created_at == "2026-04-08T12:00:00"


def test_legacy_interval_field_still_loads(tmp_path):
    """Sessions created before v2.0.27 stored `interval`; we fall back to it."""
    (tmp_path / "old.json").write_text(
        json.dumps({
            "name": "old",
            "description": "x",
            "status": "pending",
            "interval": 120.0,
        }),
        encoding="utf-8",
    )
    card = load_card(tmp_path, "old")
    assert card is not None
    assert card.check_interval == 120.0


def test_to_dict_drops_legacy_fields():
    card = TaskCard(name="rt", description="x", check_interval=1800)
    d = card.to_dict()
    assert "start_at" not in d
    assert "end_at" not in d
    assert "interval" not in d
    assert d["check_interval"] == 1800


# ── Directory-level helpers ─────────────────────────────────────────────


def test_cards_needing_check(tmp_path):
    save_card(tmp_path, TaskCard(name="fresh", description="x"))  # needs_check → True
    save_card(tmp_path, TaskCard(name="working", description="x", status="working"))
    save_card(tmp_path, TaskCard(name="finished", description="x", status="finished"))
    names = {c.name for c in cards_needing_check(tmp_path)}
    assert names == {"fresh"}


def test_has_pending_cards(tmp_path):
    assert not has_pending_cards(tmp_path)
    save_card(tmp_path, TaskCard(name="t", description="x"))
    assert has_pending_cards(tmp_path)


def test_clear_all_cards_marks_finished(tmp_path):
    save_card(tmp_path, TaskCard(name="a", description="x"))
    save_card(tmp_path, TaskCard(name="b", description="y", check_interval=600))
    clear_all_cards(tmp_path)
    cards = load_all_cards(tmp_path)
    assert all(c.status == "finished" for c in cards)


# ── Script IO ───────────────────────────────────────────────────────────


def test_write_trigger_script_prepends_shebang(tmp_path):
    path = write_trigger_script(tmp_path, "t", "echo [start]")
    body = path.read_text()
    assert body.startswith("#!/bin/bash")
    assert "echo [start]" in body


def test_write_trigger_script_empty_body_defaults_to_fire(tmp_path):
    write_trigger_script(tmp_path, "t", "")
    body = read_trigger_script(tmp_path, "t") or ""
    assert "echo [start]" in body


def test_write_end_script_none_removes_file(tmp_path):
    write_end_script(tmp_path, "t", "echo [done]")
    assert end_script_path(tmp_path, "t").exists()
    write_end_script(tmp_path, "t", None)
    assert not end_script_path(tmp_path, "t").exists()


# ── Parse helpers ───────────────────────────────────────────────────────


def test_parse_trigger_start(tmp_path):
    assert parse_trigger_output("[start]\n", 0) == ("[start]", "")


def test_parse_trigger_start_with_message(tmp_path):
    assert parse_trigger_output("debug info\n[start] build ready\n", 0) == (
        "[start]",
        "build ready",
    )


def test_parse_trigger_skip(tmp_path):
    assert parse_trigger_output("[skip]\n", 0) == ("[skip]", "")


def test_parse_trigger_unknown_line_returns_none():
    assert parse_trigger_output("hello\n", 0) is None


def test_parse_trigger_non_zero_exit_is_fail_closed():
    assert parse_trigger_output("[start]\n", 1) is None


def test_parse_end_done(tmp_path):
    assert parse_end_output("[done]\n", 0) == ("[done]", "")


def test_parse_end_not_done():
    assert parse_end_output("[not_done]\n", 0) == ("[not_done]", "")


def test_parse_end_doesnt_accept_trigger_tags():
    """end script should not interpret [start] as done."""
    assert parse_end_output("[start]\n", 0) is None
