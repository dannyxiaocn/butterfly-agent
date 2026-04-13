from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.session_engine.task_cards import (
    TaskCard,
    ensure_card,
    load_all_cards,
    save_card,
)


class TaskCardsUnitTests(unittest.TestCase):
    def test_is_due_handles_timezone_aware_last_finished(self) -> None:
        past = (datetime.now() - timedelta(hours=3)).isoformat()
        last = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        card = TaskCard(name="duty", description="ping", interval=60, last_finished_at=last, start_at=past)
        self.assertTrue(card.is_due())

    def test_ensure_card_keeps_existing(self) -> None:
        with TemporaryDirectory() as td:
            tasks_dir = Path(td)
            created = ensure_card(tasks_dir, name="duty", interval=60, description="first")
            # Modify and save
            created.description = "customized"
            save_card(tasks_dir, created)

            loaded = ensure_card(tasks_dir, name="duty", interval=120, description="second")

        self.assertEqual(loaded.description, "customized")
        self.assertEqual(loaded.interval, 60)
