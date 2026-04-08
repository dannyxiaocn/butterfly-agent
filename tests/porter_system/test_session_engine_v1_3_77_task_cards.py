from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.session_engine.task_cards import (
    TaskCard,
    _parse_card_file,
    ensure_heartbeat_card,
    load_all_cards,
    migrate_tasks_md,
    save_card,
)


class TaskCardsUnitTests(unittest.TestCase):
    def test_is_due_handles_timezone_aware_last_run(self) -> None:
        last_run = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        card = TaskCard(name="heartbeat", content="ping", interval=60, last_run_at=last_run)
        self.assertTrue(card.is_due())

    def test_parse_card_file_ignores_non_mapping_frontmatter(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "weird.md"
            path.write_text("---\n- not\n- a\n- mapping\n---\n\nbody", encoding="utf-8")
            card = _parse_card_file(path)

        self.assertEqual(card.name, "weird")
        self.assertEqual(card.status, "pending")
        self.assertEqual(card.content, "body")

    def test_migrate_tasks_md_preserves_existing_cards(self) -> None:
        with TemporaryDirectory() as td:
            core_dir = Path(td)
            save_card(core_dir / "tasks", TaskCard(name="existing", content="already there"))
            (core_dir / "tasks.md").write_text("legacy content", encoding="utf-8")

            migrate_tasks_md(core_dir)
            cards = load_all_cards(core_dir / "tasks")

        self.assertEqual([card.name for card in cards], ["existing"])

    def test_ensure_heartbeat_card_keeps_existing_content(self) -> None:
        with TemporaryDirectory() as td:
            tasks_dir = Path(td)
            created = ensure_heartbeat_card(tasks_dir, interval=60, content="first")
            created.content = "customized"
            save_card(tasks_dir, created)

            loaded = ensure_heartbeat_card(tasks_dir, interval=120, content="second")

        self.assertEqual(loaded.content, "customized")
        self.assertEqual(loaded.interval, 60)

