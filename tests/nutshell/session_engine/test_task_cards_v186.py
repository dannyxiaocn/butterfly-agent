"""Tests for v1.3.86 task card changes: pending status, start_at/end_at scheduling, mark_pending."""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from nutshell.session_engine.task_cards import (
    TaskCard,
    _default_start_at,
    _default_end_at,
    _ceil_to_hour,
    _floor_to_hour,
    _LEGACY_STATUS_MAP,
    ensure_card,
    has_pending_cards,
    load_all_cards,
    load_due_cards,
    load_card,
    save_card,
)


# ── _floor_to_hour / _ceil_to_hour ──────────────────────────────────────────


def test_floor_to_hour():
    dt = datetime(2026, 4, 12, 14, 37, 59, 123456)
    assert _floor_to_hour(dt) == datetime(2026, 4, 12, 14, 0, 0, 0)


def test_floor_to_hour_already_on_hour():
    dt = datetime(2026, 4, 12, 14, 0, 0, 0)
    assert _floor_to_hour(dt) == dt


def test_ceil_to_hour():
    dt = datetime(2026, 4, 12, 14, 37, 59, 123456)
    assert _ceil_to_hour(dt) == datetime(2026, 4, 12, 15, 0, 0, 0)


def test_ceil_to_hour_already_on_hour():
    """Exact hour should NOT round up."""
    dt = datetime(2026, 4, 12, 14, 0, 0, 0)
    assert _ceil_to_hour(dt) == dt


def test_ceil_to_hour_one_second_past():
    dt = datetime(2026, 4, 12, 14, 0, 1)
    assert _ceil_to_hour(dt) == datetime(2026, 4, 12, 15, 0, 0)


# ── _default_start_at ────────────────────────────────────────────────────────


def test_default_start_at_oneshot():
    """One-shot tasks: start_at = floor(created_at)."""
    created = datetime(2026, 4, 12, 10, 30, 0)
    result = _default_start_at(created, interval=None)
    assert result == "2026-04-12T10:00:00"  # floor


def test_default_start_at_recurring():
    """Recurring: start_at = ceil(created + interval)."""
    created = datetime(2026, 4, 12, 10, 0, 0)
    result = _default_start_at(created, interval=3600)
    assert result == "2026-04-12T11:00:00"  # exact hour, no rounding needed


def test_default_start_at_recurring_non_exact():
    """Recurring with non-exact result rounds UP to next hour."""
    created = datetime(2026, 4, 12, 10, 30, 0)
    result = _default_start_at(created, interval=3600)
    # 10:30 + 1h = 11:30 → ceil → 12:00
    assert result == "2026-04-12T12:00:00"


def test_default_start_at_large_interval():
    created = datetime(2026, 4, 12, 10, 0, 0)
    result = _default_start_at(created, interval=86400)  # 1 day
    assert result == "2026-04-13T10:00:00"


# ── _default_end_at ──────────────────────────────────────────────────────────


def test_default_end_at_default_7_days():
    """Default end_at is ceil(7 days from created)."""
    created = datetime(2026, 4, 12, 10, 30, 0)
    result = _default_end_at(created, interval=3600)
    # 10:30 + 7d = 2026-04-19T10:30 → ceil → 11:00
    assert result == "2026-04-19T11:00:00"


def test_default_end_at_no_interval():
    """One-shot: end_at = ceil(7 days from created)."""
    created = datetime(2026, 4, 12, 10, 0, 0)
    result = _default_end_at(created, interval=None)
    # exact hour, no rounding
    assert result == "2026-04-19T10:00:00"


def test_default_end_at_large_interval():
    """If interval > 7 days, end_at = ceil(10 * interval)."""
    created = datetime(2026, 4, 12, 10, 0, 0)
    eight_days = 8 * 24 * 3600
    result = _default_end_at(created, interval=eight_days)
    expected = _ceil_to_hour(created + timedelta(seconds=eight_days * 10))
    assert result == expected.isoformat()


# ── TaskCard defaults via __post_init__ ──────────────────────────────────────


def test_post_init_fills_start_at_end_at():
    """__post_init__ fills start_at/end_at when None."""
    card = TaskCard(name="t", interval=3600, created_at="2026-04-12T10:00:00")
    assert card.start_at == "2026-04-12T11:00:00"
    assert card.end_at == "2026-04-19T10:00:00"


def test_post_init_preserves_explicit_values():
    """__post_init__ does NOT overwrite explicit start_at/end_at."""
    card = TaskCard(
        name="t", interval=3600,
        created_at="2026-04-12T10:00:00",
        start_at="2026-05-01T00:00:00",
        end_at="2026-06-01T00:00:00",
    )
    assert card.start_at == "2026-05-01T00:00:00"
    assert card.end_at == "2026-06-01T00:00:00"


def test_post_init_oneshot_start_at_is_created():
    """One-shot tasks: start_at = floor(created_at)."""
    card = TaskCard(name="t", interval=None, created_at="2026-04-12T10:30:00")
    assert card.start_at == "2026-04-12T10:00:00"  # floor


def test_post_init_invalid_created_at():
    """Invalid created_at falls back to now() for defaults."""
    card = TaskCard(name="t", interval=3600, created_at="not-a-date")
    # Should not raise; start_at/end_at should be filled
    assert card.start_at is not None
    assert card.end_at is not None


# ── Default status = pending ─────────────────────────────────────────────────


def test_default_status_is_pending():
    card = TaskCard(name="t")
    assert card.status == "pending"


# ── is_due with start_at / end_at ────────────────────────────────────────────


def test_is_due_before_start_at():
    """Task is not due before start_at."""
    future = (datetime.now() + timedelta(hours=2)).isoformat()
    card = TaskCard(
        name="t", interval=600, status="pending",
        start_at=future,
        end_at=(datetime.now() + timedelta(days=7)).isoformat(),
    )
    assert not card.is_due()


def test_is_due_after_start_at():
    """Task is due after start_at (never finished)."""
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    card = TaskCard(
        name="t", interval=600, status="pending",
        start_at=past,
        end_at=(datetime.now() + timedelta(days=7)).isoformat(),
    )
    assert card.is_due()


def test_is_due_auto_expire_after_end_at():
    """Task auto-expires when current >= end_at."""
    past_end = (datetime.now() - timedelta(hours=1)).isoformat()
    past_start = (datetime.now() - timedelta(days=2)).isoformat()
    card = TaskCard(
        name="t", interval=600, status="pending",
        start_at=past_start,
        end_at=past_end,
    )
    assert not card.is_due()
    assert card.status == "finished"  # auto-expired


def test_is_due_paused_never_fires():
    """Paused tasks never fire."""
    card = TaskCard(
        name="t", interval=600, status="paused",
        start_at=(datetime.now() - timedelta(hours=1)).isoformat(),
        end_at=(datetime.now() + timedelta(days=7)).isoformat(),
    )
    assert not card.is_due()


def test_is_due_invalid_start_at_ignored():
    """Invalid start_at is ignored (treated as no constraint)."""
    card = TaskCard(
        name="t", interval=600, status="pending",
        start_at="bad-date",
        end_at=(datetime.now() + timedelta(days=7)).isoformat(),
    )
    assert card.is_due()


def test_is_due_invalid_end_at_ignored():
    """Invalid end_at is ignored (no auto-expire)."""
    card = TaskCard(
        name="t", interval=600, status="pending",
        start_at=(datetime.now() - timedelta(hours=1)).isoformat(),
        end_at="bad-date",
    )
    assert card.is_due()


# ── mark_pending / mark_paused ───────────────────────────────────────────────


def test_mark_pending():
    card = TaskCard(name="t", status="working")
    card.mark_pending()
    assert card.status == "pending"


def test_mark_paused():
    card = TaskCard(name="t", status="pending")
    card.mark_paused()
    assert card.status == "paused"


# ── mark_finished recurring → pending ────────────────────────────────────────


def test_mark_finished_recurring_goes_to_pending():
    """Recurring tasks go to pending (not paused) after completion."""
    card = TaskCard(name="t", interval=600, status="working")
    card.mark_finished()
    assert card.status == "pending"
    assert card.last_finished_at is not None


def test_mark_finished_oneshot_stays_finished():
    card = TaskCard(name="t", interval=None, status="working")
    card.mark_finished()
    assert card.status == "finished"


# ── to_dict / from_dict with start_at/end_at ────────────────────────────────


def test_to_dict_includes_start_end():
    card = TaskCard(
        name="t", interval=3600,
        created_at="2026-04-12T10:00:00",
        start_at="2026-04-12T11:00:00",
        end_at="2026-04-19T10:00:00",
    )
    d = card.to_dict()
    assert d["start_at"] == "2026-04-12T11:00:00"
    assert d["end_at"] == "2026-04-19T10:00:00"
    assert d["status"] == "pending"


def test_from_dict_with_start_end():
    d = {
        "name": "t", "interval": 3600,
        "start_at": "2026-04-12T11:00:00",
        "end_at": "2026-04-19T10:00:00",
        "created_at": "2026-04-12T10:00:00",
    }
    card = TaskCard.from_dict(d)
    assert card.start_at == "2026-04-12T11:00:00"
    assert card.end_at == "2026-04-19T10:00:00"


def test_from_dict_without_start_end_gets_defaults():
    """from_dict with no start_at/end_at triggers __post_init__ defaults."""
    d = {"name": "t", "interval": 3600, "created_at": "2026-04-12T10:00:00"}
    card = TaskCard.from_dict(d)
    assert card.start_at == "2026-04-12T11:00:00"
    assert card.end_at == "2026-04-19T10:00:00"


def test_roundtrip_preserves_start_end(tmp_path):
    """Save + load roundtrip preserves start_at/end_at."""
    card = TaskCard(
        name="rt", interval=3600,
        created_at="2026-04-12T10:00:00",
        start_at="2026-04-12T11:00:00",
        end_at="2026-04-19T10:00:00",
    )
    save_card(tmp_path, card)
    loaded = load_card(tmp_path, "rt")
    assert loaded is not None
    assert loaded.start_at == "2026-04-12T11:00:00"
    assert loaded.end_at == "2026-04-19T10:00:00"


# ── Legacy status mapping ───────────────────────────────────────────────────


def test_legacy_status_map_running():
    assert _LEGACY_STATUS_MAP["running"] == "working"


def test_legacy_status_map_completed():
    assert _LEGACY_STATUS_MAP["completed"] == "finished"


def test_legacy_pending_not_mapped():
    """'pending' is a valid status now, NOT mapped to 'paused'."""
    assert "pending" not in _LEGACY_STATUS_MAP


def test_from_dict_maps_legacy_running():
    d = {"name": "old", "status": "running"}
    card = TaskCard.from_dict(d)
    assert card.status == "working"


def test_from_dict_maps_legacy_completed():
    d = {"name": "old", "status": "completed"}
    card = TaskCard.from_dict(d)
    assert card.status == "finished"


def test_from_dict_keeps_paused():
    """Paused cards stay paused (user-initiated pause is preserved)."""
    d = {"name": "old", "status": "paused"}
    card = TaskCard.from_dict(d)
    assert card.status == "paused"


# ── load_due_cards auto-expire persistence ───────────────────────────────────


def test_load_due_cards_persists_expired(tmp_path):
    """load_due_cards saves auto-expired cards to disk."""
    past_end = (datetime.now() - timedelta(hours=1)).isoformat()
    past_start = (datetime.now() - timedelta(days=2)).isoformat()
    card = TaskCard(
        name="expired", interval=600, status="pending",
        start_at=past_start, end_at=past_end,
        created_at=(datetime.now() - timedelta(days=3)).isoformat(),
    )
    save_card(tmp_path, card)

    due = load_due_cards(tmp_path)
    assert len(due) == 0

    # Reload from disk — status should be persisted as finished
    reloaded = load_card(tmp_path, "expired")
    assert reloaded is not None
    assert reloaded.status == "finished"


def test_load_due_cards_returns_due_cards(tmp_path):
    """Due cards are returned and non-due cards are not."""
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    future_end = (datetime.now() + timedelta(days=7)).isoformat()

    due_card = TaskCard(
        name="due", interval=600, status="pending",
        start_at=past, end_at=future_end,
        created_at=(datetime.now() - timedelta(days=1)).isoformat(),
    )
    paused_card = TaskCard(
        name="paused", interval=600, status="paused",
        start_at=past, end_at=future_end,
        created_at=(datetime.now() - timedelta(days=1)).isoformat(),
    )
    save_card(tmp_path, due_card)
    save_card(tmp_path, paused_card)

    due = load_due_cards(tmp_path)
    assert len(due) == 1
    assert due[0].name == "due"


# ── has_pending_cards with new status ────────────────────────────────────────


def test_has_pending_cards_true(tmp_path):
    card = TaskCard(name="t", status="pending")
    save_card(tmp_path, card)
    assert has_pending_cards(tmp_path)


def test_has_pending_cards_false_paused(tmp_path):
    """Paused cards are NOT 'pending'."""
    card = TaskCard(name="t", status="paused")
    save_card(tmp_path, card)
    assert not has_pending_cards(tmp_path)


# ── ensure_card with start_at/end_at ─────────────────────────────────────────


def test_ensure_card_creates_with_pending(tmp_path):
    card = ensure_card(tmp_path, name="duty", interval=3600)
    assert card.status == "pending"
    assert card.start_at is not None
    assert card.end_at is not None


def test_ensure_card_with_explicit_start_end(tmp_path):
    card = ensure_card(
        tmp_path, name="duty", interval=3600,
        start_at="2026-05-01T00:00:00",
        end_at="2026-06-01T00:00:00",
    )
    assert card.start_at == "2026-05-01T00:00:00"
    assert card.end_at == "2026-06-01T00:00:00"
