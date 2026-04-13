"""Tests for nutshell.session_engine.task_cards module."""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from nutshell.session_engine.task_cards import (
    TaskCard,
    clear_all_cards,
    ensure_card,
    has_pending_cards,
    load_all_cards,
    load_due_cards,
    load_card,
    save_card,
)


# ── TaskCard.is_due() ────────────────────────────────────────────────────────


def test_card_is_due_when_never_finished():
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    card = TaskCard(name="test", description="do it", interval=600, start_at=past)
    assert card.is_due()


def test_card_not_due_when_finished():
    card = TaskCard(name="test", description="do it", status="finished")
    assert not card.is_due()


def test_card_not_due_when_working():
    card = TaskCard(name="test", description="do it", status="working")
    assert not card.is_due()


def test_card_due_when_interval_elapsed():
    past_start = (datetime.now() - timedelta(hours=1)).isoformat()
    past = (datetime.now() - timedelta(seconds=700)).isoformat()
    card = TaskCard(name="test", description="do it", interval=600, last_finished_at=past, start_at=past_start)
    assert card.is_due()


def test_card_due_when_interval_elapsed_with_timezone_aware_timestamp():
    past_start = (datetime.now() - timedelta(hours=1)).isoformat()
    past = (datetime.now().astimezone() - timedelta(seconds=700)).isoformat()
    card = TaskCard(name="test", description="do it", interval=600, last_finished_at=past, start_at=past_start)
    assert card.is_due()


def test_card_not_due_when_interval_not_elapsed():
    past_start = (datetime.now() - timedelta(hours=1)).isoformat()
    recent = (datetime.now() - timedelta(seconds=100)).isoformat()
    card = TaskCard(name="test", description="do it", interval=600, last_finished_at=recent, start_at=past_start)
    assert not card.is_due()


def test_oneshot_card_not_due_after_finished():
    """One-shot cards (interval=None) should not be due after first finish."""
    card = TaskCard(
        name="test", description="do it", interval=None,
        status="finished", last_finished_at=datetime.now().isoformat(),
    )
    assert not card.is_due()


def test_oneshot_card_due_when_never_finished():
    card = TaskCard(name="test", description="do it", interval=None)
    assert card.is_due()


# ── mark_working / mark_finished ─────────────────────────────────────────────


def test_mark_working():
    card = TaskCard(name="test", description="x")
    card.mark_working()
    assert card.status == "working"
    assert card.last_started_at is not None


def test_mark_finished_oneshot():
    card = TaskCard(name="test", description="x", interval=None)
    card.mark_finished()
    assert card.status == "finished"
    assert card.last_finished_at is not None


def test_mark_finished_recurring():
    card = TaskCard(name="test", description="x", interval=600)
    card.mark_finished()
    assert card.status == "pending"
    assert card.last_finished_at is not None


def test_mark_paused():
    card = TaskCard(name="test", description="x", status="working")
    card.mark_paused()
    assert card.status == "paused"


# ── Serialization round-trip ─────────────────────────────────────────────────


def test_serialize_and_parse_roundtrip(tmp_path):
    card = TaskCard(
        name="my_task",
        description="Do something important",
        interval=3600,
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
    assert loaded.interval == 3600
    assert loaded.status == "paused"
    assert loaded.created_at == "2026-04-08T12:00:00"


def test_to_dict_from_dict_roundtrip():
    card = TaskCard(
        name="rt",
        description="round-trip test",
        interval=1800,
        status="paused",
        comments="some notes",
        progress="50%",
    )
    d = card.to_dict()
    restored = TaskCard.from_dict(d)
    assert restored.name == "rt"
    assert restored.description == "round-trip test"
    assert restored.interval == 1800
    assert restored.comments == "some notes"
    assert restored.progress == "50%"


# ── Directory operations ──────────────────────────────────────────────────────


def test_load_all_cards(tmp_path):
    save_card(tmp_path, TaskCard(name="a", description="task a"))
    save_card(tmp_path, TaskCard(name="b", description="task b", status="finished"))
    cards = load_all_cards(tmp_path)
    assert len(cards) == 2
    names = {c.name for c in cards}
    assert names == {"a", "b"}


def test_load_all_cards_empty_dir(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    assert load_all_cards(tasks_dir) == []


def test_load_all_cards_nonexistent_dir(tmp_path):
    assert load_all_cards(tmp_path / "nonexistent") == []


def test_load_due_cards(tmp_path):
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    save_card(tmp_path, TaskCard(name="due", description="x", interval=600, start_at=past))
    save_card(tmp_path, TaskCard(name="done", description="y", status="finished"))
    save_card(tmp_path, TaskCard(name="busy", description="z", status="working"))
    due = load_due_cards(tmp_path)
    assert len(due) == 1
    assert due[0].name == "due"


def test_has_pending_cards(tmp_path):
    assert not has_pending_cards(tmp_path)
    save_card(tmp_path, TaskCard(name="t", description="x"))
    assert has_pending_cards(tmp_path)


def test_clear_all_cards(tmp_path):
    save_card(tmp_path, TaskCard(name="a", description="x"))
    save_card(tmp_path, TaskCard(name="b", description="y", interval=600))
    clear_all_cards(tmp_path)
    cards = load_all_cards(tmp_path)
    assert all(c.status == "finished" for c in cards)


# ── ensure_card ──────────────────────────────────────────────────────────────


def test_ensure_card_creates(tmp_path):
    card = ensure_card(tmp_path, name="duty", interval=3600, description="Duty task")
    assert card.name == "duty"
    assert card.interval == 3600
    assert card.status == "pending"
    assert (tmp_path / "duty.json").exists()


def test_ensure_card_idempotent(tmp_path):
    card1 = ensure_card(tmp_path, name="duty", interval=3600)
    card1.last_finished_at = "2026-04-08T00:00:00"
    save_card(tmp_path, card1)

    card2 = ensure_card(tmp_path, name="duty", interval=7200)
    assert card2.last_finished_at == "2026-04-08T00:00:00"
    assert card2.interval == 3600  # original preserved
