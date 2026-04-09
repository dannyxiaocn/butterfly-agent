"""Tests for nutshell.session_engine.task_cards module."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from pathlib import Path

from nutshell.session_engine.task_cards import (
    TaskCard,
    _parse_card_file,
    _serialize_card,
    clear_all_cards,
    migrate_legacy_default_task,
    migrate_legacy_task_sources,
    ensure_heartbeat_card,
    has_pending_cards,
    load_all_cards,
    load_due_cards,
    load_card,
    migrate_tasks_md,
    save_card,
)
from nutshell.session_engine.session_params import write_session_params


# ── TaskCard.is_due() ────────────────────────────────────────────────────────


def test_card_is_due_when_never_run():
    card = TaskCard(name="test", content="do it", interval=600)
    assert card.is_due()


def test_card_not_due_when_completed():
    card = TaskCard(name="test", content="do it", status="completed")
    assert not card.is_due()


def test_card_not_due_when_paused():
    card = TaskCard(name="test", content="do it", status="paused")
    assert not card.is_due()


def test_card_due_when_interval_elapsed():
    past = (datetime.now() - timedelta(seconds=700)).isoformat()
    card = TaskCard(name="test", content="do it", interval=600, last_run_at=past)
    assert card.is_due()


def test_card_due_when_interval_elapsed_with_timezone_aware_timestamp():
    past = (datetime.now().astimezone() - timedelta(seconds=700)).isoformat()
    card = TaskCard(name="test", content="do it", interval=600, last_run_at=past)
    assert card.is_due()


def test_card_not_due_when_interval_not_elapsed():
    recent = (datetime.now() - timedelta(seconds=100)).isoformat()
    card = TaskCard(name="test", content="do it", interval=600, last_run_at=recent)
    assert not card.is_due()


def test_card_not_due_before_starts_at():
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    card = TaskCard(name="test", content="do it", interval=600, starts_at=future)
    assert not card.is_due()


def test_card_not_due_after_ends_at():
    past_end = (datetime.now() - timedelta(minutes=1)).isoformat()
    card = TaskCard(name="test", content="do it", interval=600, ends_at=past_end)
    assert not card.is_due()


def test_oneshot_card_not_due_after_run():
    """One-shot cards (interval=None) should not be due after first run."""
    card = TaskCard(name="test", content="do it", interval=None, last_run_at=datetime.now().isoformat())
    assert not card.is_due()


def test_oneshot_card_due_when_never_run():
    card = TaskCard(name="test", content="do it", interval=None)
    assert card.is_due()


# ── mark_running / mark_done ──────────────────────────────────────────────────


def test_mark_running():
    card = TaskCard(name="test", content="x")
    card.mark_running()
    assert card.status == "running"


def test_mark_done_oneshot():
    card = TaskCard(name="test", content="x", interval=None)
    card.mark_done()
    assert card.status == "completed"
    assert card.last_run_at is not None


def test_mark_done_recurring():
    card = TaskCard(name="test", content="x", interval=600)
    card.mark_done()
    assert card.status == "pending"
    assert card.last_run_at is not None


def test_mark_done_clear():
    card = TaskCard(name="test", content="x", interval=600)
    card.mark_done(clear=True)
    assert card.status == "completed"


# ── Serialization round-trip ────────────────────────────────────────���─────────


def test_serialize_and_parse_roundtrip(tmp_path):
    card = TaskCard(
        name="my_task",
        content="Do something important",
        interval=3600,
        status="pending",
        created_at="2026-04-08T12:00:00",
    )
    path = save_card(tmp_path, card)
    assert path == tmp_path / "my_task.md"
    assert path.exists()

    loaded = _parse_card_file(path)
    assert loaded.name == "my_task"
    assert loaded.content == "Do something important"
    assert loaded.interval == 3600
    assert loaded.starts_at is None
    assert loaded.ends_at is None
    assert loaded.status == "pending"
    assert loaded.created_at == "2026-04-08T12:00:00"


def test_serialize_and_parse_with_schedule_window(tmp_path):
    card = TaskCard(
        name="windowed",
        content="Do something later",
        interval=1800,
        starts_at="2026-04-10T09:00:00",
        ends_at="2026-04-10T18:00:00",
        status="pending",
    )
    loaded = _parse_card_file(save_card(tmp_path, card))
    assert loaded.starts_at == "2026-04-10T09:00:00"
    assert loaded.ends_at == "2026-04-10T18:00:00"


def test_parse_card_without_frontmatter(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("Just a plain task\n", encoding="utf-8")
    card = _parse_card_file(path)
    assert card.name == "plain"
    assert card.content == "Just a plain task"
    assert card.interval is None
    assert card.status == "pending"


def test_parse_card_with_non_mapping_frontmatter(tmp_path):
    path = tmp_path / "scalar.md"
    path.write_text("---\n- a\n- b\n---\n\nTask body\n", encoding="utf-8")
    card = _parse_card_file(path)
    assert card.name == "scalar"
    assert card.content == "Task body"
    assert card.status == "pending"


# ── Directory operations ──────────────────────────────────────────────────────


def test_load_all_cards(tmp_path):
    save_card(tmp_path, TaskCard(name="a", content="task a"))
    save_card(tmp_path, TaskCard(name="b", content="task b", status="completed"))
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
    save_card(tmp_path, TaskCard(name="due", content="x", interval=600))
    save_card(tmp_path, TaskCard(name="done", content="y", status="completed"))
    save_card(tmp_path, TaskCard(name="paused", content="z", status="paused"))
    due = load_due_cards(tmp_path)
    assert len(due) == 1
    assert due[0].name == "due"


def test_has_pending_cards(tmp_path):
    assert not has_pending_cards(tmp_path)
    save_card(tmp_path, TaskCard(name="t", content="x"))
    assert has_pending_cards(tmp_path)


def test_clear_all_cards(tmp_path):
    save_card(tmp_path, TaskCard(name="a", content="x"))
    save_card(tmp_path, TaskCard(name="b", content="y", interval=600))
    clear_all_cards(tmp_path)
    cards = load_all_cards(tmp_path)
    assert all(c.status == "completed" for c in cards)


# ── Migration ─────────────────────────────────────────────────────────────────


def test_migrate_tasks_md_with_content(tmp_path):
    core_dir = tmp_path
    tasks_md = core_dir / "tasks.md"
    tasks_md.write_text("- [ ] do something\n- [ ] do another thing\n", encoding="utf-8")

    migrate_tasks_md(core_dir)

    # tasks.md should be removed
    assert not tasks_md.exists()
    # tasks/ dir should have one card
    tasks_dir = core_dir / "tasks"
    cards = load_all_cards(tasks_dir)
    assert len(cards) == 1
    assert cards[0].name == "migrated_task"
    assert "do something" in cards[0].content
    assert cards[0].interval is None  # one-shot


def test_migrate_tasks_md_empty(tmp_path):
    core_dir = tmp_path
    tasks_md = core_dir / "tasks.md"
    tasks_md.write_text("", encoding="utf-8")

    migrate_tasks_md(core_dir)

    assert not tasks_md.exists()
    tasks_dir = core_dir / "tasks"
    assert tasks_dir.is_dir()
    assert load_all_cards(tasks_dir) == []


def test_migrate_noop_when_no_tasks_md(tmp_path):
    """migrate_tasks_md is a no-op if tasks.md doesn't exist."""
    migrate_tasks_md(tmp_path)
    # Should not create tasks/ dir either
    assert not (tmp_path / "tasks").exists()


def test_migrate_skips_when_cards_exist(tmp_path):
    """If tasks/ already has cards, don't re-migrate tasks.md content."""
    core_dir = tmp_path
    tasks_md = core_dir / "tasks.md"
    tasks_md.write_text("old content", encoding="utf-8")
    tasks_dir = core_dir / "tasks"
    save_card(tasks_dir, TaskCard(name="existing", content="already here"))

    migrate_tasks_md(core_dir)

    cards = load_all_cards(tasks_dir)
    names = {c.name for c in cards}
    assert "existing" in names
    # Should NOT have created migrated_task since cards already existed
    assert "migrated_task" not in names


def test_migrate_legacy_default_task_creates_heartbeat_card(tmp_path):
    session_dir = tmp_path / "session"
    (session_dir / "core").mkdir(parents=True)
    write_session_params(session_dir, session_type="persistent", default_task="Check mail", heartbeat_interval=300)

    migrate_legacy_default_task(session_dir)

    params = (session_dir / "core" / "params.json").read_text(encoding="utf-8")
    assert '"default_task": null' in params
    heartbeat = load_card(session_dir / "core" / "tasks", "heartbeat")
    assert heartbeat is not None
    assert heartbeat.content == "Check mail"
    assert heartbeat.interval == 300


def test_migrate_legacy_task_sources_combines_tasks_md_and_default_task(tmp_path):
    session_dir = tmp_path / "session"
    core_dir = session_dir / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "tasks.md").write_text("legacy board", encoding="utf-8")
    write_session_params(session_dir, session_type="persistent", default_task="Check mail", heartbeat_interval=600)

    migrate_legacy_task_sources(session_dir)

    cards = load_all_cards(core_dir / "tasks")
    assert {card.name for card in cards} == {"heartbeat", "migrated_task"}


# ── Heartbeat card ────────────────────────────────────────────────────────────


def test_ensure_heartbeat_card_creates(tmp_path):
    card = ensure_heartbeat_card(tmp_path, interval=3600)
    assert card.name == "heartbeat"
    assert card.interval == 3600
    assert card.status == "pending"
    assert "Check for incoming messages" in card.content
    assert (tmp_path / "heartbeat.md").exists()


def test_ensure_heartbeat_card_custom_content(tmp_path):
    card = ensure_heartbeat_card(tmp_path, interval=7200, content="Do custom thing")
    assert card.content == "Do custom thing"
    assert card.interval == 7200


def test_ensure_heartbeat_card_idempotent(tmp_path):
    card1 = ensure_heartbeat_card(tmp_path, interval=3600)
    # Modify the card on disk to verify idempotency
    card1.last_run_at = "2026-04-08T00:00:00"
    save_card(tmp_path, card1)

    card2 = ensure_heartbeat_card(tmp_path, interval=7200)  # different interval
    # Should return existing card, not create new one
    assert card2.last_run_at == "2026-04-08T00:00:00"
    assert card2.interval == 3600  # original interval preserved
