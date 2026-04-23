"""Tests for the standalone todo list (v2.0.37).

Covers:

* ``todo_list`` helpers — normalise, progress line (incl. all-done
  ``[N/N] <last name>``), active index, pending count, completed diff,
  reminder text, JSON round-trip.
* ``TodoListExecutor`` tool end-to-end (reads/writes ``core/todo_list.json``).
* ``hud_service.get_hud`` surfaces the todo payload.
* ``todo_list_service.get_todo_list`` shape + hidden-when-empty.
* Session-level reminder enqueue on threshold crossing.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butterfly.session_engine.todo_list import (
    TODO_DEFAULT_THRESHOLD,
    TODO_LIST_FILENAME,
    TodoList,
    delete_todo_list,
    diff_newly_completed,
    format_completed_comment_line,
    format_todo_progress,
    format_todo_system_reminder,
    is_todo_all_done,
    load_todo_list,
    normalise_todo_items,
    save_todo_list,
    todo_active_index,
    todo_pending_count,
)


class TodoHelpersTest(unittest.TestCase):
    def test_normalise_drops_junk_and_trims(self) -> None:
        items = normalise_todo_items(
            [
                {"content": "  Run tests ", "status": "in_progress", "activeForm": "Running tests"},
                {"content": "", "status": "pending", "activeForm": ""},
                {"content": "Write docs", "status": "COMPLETED", "activeForm": "Writing docs"},
                "not a dict",
                {"content": "Investigate", "status": "weird_status", "activeForm": ""},
            ]
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["content"], "Run tests")
        self.assertEqual(items[0]["status"], "in_progress")
        self.assertEqual(items[1]["status"], "completed")
        self.assertEqual(items[2]["activeForm"], "Investigate")  # defaults to content
        self.assertEqual(items[2]["status"], "pending")

    def test_normalise_rejects_non_list(self) -> None:
        self.assertEqual(normalise_todo_items(None), [])
        self.assertEqual(normalise_todo_items("oops"), [])
        self.assertEqual(normalise_todo_items({"todos": []}), [])

    def test_active_index_prefers_in_progress(self) -> None:
        items = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "pending", "activeForm": "B"},
            {"content": "c", "status": "in_progress", "activeForm": "C"},
        ]
        self.assertEqual(todo_active_index(items), 3)

    def test_active_index_falls_back_to_pending(self) -> None:
        items = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "pending", "activeForm": "B"},
        ]
        self.assertEqual(todo_active_index(items), 2)

    def test_active_index_all_done_returns_total(self) -> None:
        items = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "completed", "activeForm": "B"},
        ]
        self.assertEqual(todo_active_index(items), 2)

    def test_progress_line_uses_active_form(self) -> None:
        items = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "in_progress", "activeForm": "Doing B"},
            {"content": "c", "status": "pending", "activeForm": "C"},
        ]
        self.assertEqual(format_todo_progress(items), "[2/3] Doing B")

    def test_progress_line_all_done_includes_last_name(self) -> None:
        # v2.0.37 bug fix: collapsed HUD row needs the last todo's name
        # visible when all done, instead of a bare ``[N/N]``.
        items = [
            {"content": "Investigate", "status": "completed", "activeForm": "Investigating"},
            {"content": "Review PR", "status": "completed", "activeForm": "Reviewing PR"},
        ]
        self.assertEqual(format_todo_progress(items), "[2/2] Review PR")

    def test_progress_line_all_done_single_item(self) -> None:
        items = [{"content": "a", "status": "completed", "activeForm": "A"}]
        self.assertEqual(format_todo_progress(items), "[1/1] a")

    def test_progress_line_empty_list(self) -> None:
        self.assertEqual(format_todo_progress([]), "")

    def test_is_todo_all_done(self) -> None:
        self.assertFalse(is_todo_all_done([]))
        self.assertFalse(is_todo_all_done([
            {"content": "a", "status": "pending", "activeForm": "A"},
        ]))
        self.assertTrue(is_todo_all_done([
            {"content": "a", "status": "completed", "activeForm": "A"},
        ]))

    def test_pending_count(self) -> None:
        items = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "in_progress", "activeForm": "B"},
            {"content": "c", "status": "pending", "activeForm": "C"},
        ]
        self.assertEqual(todo_pending_count(items), 2)

    def test_diff_newly_completed(self) -> None:
        prev = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "pending", "activeForm": "B"},
        ]
        new = [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "completed", "activeForm": "B"},
        ]
        diff = diff_newly_completed(prev, new)
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["content"], "b")

    def test_diff_empty_when_no_flip(self) -> None:
        same = [{"content": "a", "status": "completed", "activeForm": "A"}]
        self.assertEqual(diff_newly_completed(same, same), [])

    def test_completed_comment_line_format(self) -> None:
        line = format_completed_comment_line(
            2, {"content": "Implement Y", "status": "completed", "activeForm": "Implementing Y"}
        )
        self.assertEqual(line, "☑ 2. Implement Y")

    def test_reminder_wraps_in_system_reminder_block(self) -> None:
        items = [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
            {"content": "Review PR", "status": "pending", "activeForm": "Reviewing PR"},
        ]
        text = format_todo_system_reminder(items)
        self.assertIn("<system-reminder>", text)
        self.assertIn("</system-reminder>", text)
        self.assertIn("1. [in_progress] Run tests", text)
        self.assertIn("(active: Running tests)", text)
        self.assertIn("2. [pending] Review PR", text)

    def test_reminder_empty_list_returns_empty(self) -> None:
        self.assertEqual(format_todo_system_reminder([]), "")


class TodoListIOTest(unittest.TestCase):
    def test_round_trip_through_json(self) -> None:
        with TemporaryDirectory() as td:
            core = Path(td)
            tl = TodoList(
                todos=[
                    {"content": "a", "status": "in_progress", "activeForm": "Aing"},
                    {"content": "b", "status": "pending", "activeForm": "Bing"},
                ],
                iters_since_seen=4,
                reminder_threshold=7,
                progress="[1/2] Aing",
                comments="☑ 0. previous",
            )
            save_todo_list(core, tl)
            reloaded = load_todo_list(core)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.todos, tl.todos)
            self.assertEqual(reloaded.iters_since_seen, 4)
            self.assertEqual(reloaded.reminder_threshold, 7)
            self.assertEqual(reloaded.progress, "[1/2] Aing")
            self.assertEqual(reloaded.comments, "☑ 0. previous")
            self.assertIsNotNone(reloaded.updated_at)

    def test_load_returns_none_when_absent(self) -> None:
        with TemporaryDirectory() as td:
            self.assertIsNone(load_todo_list(Path(td)))

    def test_from_dict_clamps_bad_threshold(self) -> None:
        data = {
            "todos": [{"content": "a", "status": "pending", "activeForm": "A"}],
            "reminder_threshold": -3,
            "iters_since_seen": "garbage",
        }
        tl = TodoList.from_dict(data)
        self.assertEqual(tl.reminder_threshold, TODO_DEFAULT_THRESHOLD)
        self.assertEqual(tl.iters_since_seen, 0)

    def test_delete(self) -> None:
        with TemporaryDirectory() as td:
            core = Path(td)
            save_todo_list(core, TodoList(todos=[{"content": "a", "status": "pending", "activeForm": "A"}]))
            self.assertTrue((core / TODO_LIST_FILENAME).exists())
            self.assertTrue(delete_todo_list(core))
            self.assertFalse((core / TODO_LIST_FILENAME).exists())
            self.assertFalse(delete_todo_list(core))  # idempotent

    def test_lives_outside_tasks_directory(self) -> None:
        """Regression guard — the todo list MUST NOT appear as a task card."""
        with TemporaryDirectory() as td:
            core = Path(td)
            tasks_dir = core / "tasks"
            tasks_dir.mkdir()
            save_todo_list(core, TodoList(todos=[{"content": "a", "status": "pending", "activeForm": "A"}]))
            # The file lives at core/todo_list.json — NOT under tasks/.
            self.assertTrue((core / TODO_LIST_FILENAME).exists())
            self.assertFalse((tasks_dir / TODO_LIST_FILENAME).exists())


class TodoListExecutorTest(unittest.TestCase):
    def _exec(self, core_dir: Path, on_change=None):
        from toolhub.todo_list.executor import TodoListExecutor
        return TodoListExecutor(core_dir=core_dir, on_change=on_change)

    def test_first_call_creates_file(self) -> None:
        with TemporaryDirectory() as td:
            core = Path(td)
            calls: list[str] = []
            exec_ = self._exec(core, lambda c: calls.append(c))
            out = asyncio.run(exec_.execute(todos=[
                {"content": "a", "status": "in_progress", "activeForm": "Aing"},
                {"content": "b", "status": "pending", "activeForm": "Bing"},
            ]))
            self.assertIn("Todos have been modified successfully", out)
            tl = load_todo_list(core)
            self.assertIsNotNone(tl)
            self.assertEqual(len(tl.todos), 2)
            self.assertEqual(tl.progress, "[1/2] Aing")
            self.assertEqual(tl.iters_since_seen, 0)
            self.assertEqual(calls, ["updated"])

    def test_subsequent_call_appends_completed_comment_and_resets_iters(self) -> None:
        with TemporaryDirectory() as td:
            core = Path(td)
            exec_ = self._exec(core)
            asyncio.run(exec_.execute(todos=[
                {"content": "Investigate", "status": "completed", "activeForm": "Investigating"},
                {"content": "Implement", "status": "in_progress", "activeForm": "Implementing"},
            ]))
            # Simulate some iters passing
            tl = load_todo_list(core)
            tl.iters_since_seen = 5
            save_todo_list(core, tl)
            asyncio.run(exec_.execute(todos=[
                {"content": "Investigate", "status": "completed", "activeForm": "Investigating"},
                {"content": "Implement", "status": "completed", "activeForm": "Implementing"},
            ]))
            tl = load_todo_list(core)
            self.assertEqual(tl.iters_since_seen, 0)
            self.assertIn("☑ 1. Investigate", tl.comments)
            self.assertIn("☑ 2. Implement", tl.comments)
            # All-done progress shape — with last name visible
            self.assertEqual(tl.progress, "[2/2] Implement")

    def test_empty_list_clears_todos(self) -> None:
        with TemporaryDirectory() as td:
            core = Path(td)
            exec_ = self._exec(core)
            asyncio.run(exec_.execute(todos=[{"content": "a", "status": "pending", "activeForm": "A"}]))
            asyncio.run(exec_.execute(todos=[]))
            tl = load_todo_list(core)
            self.assertEqual(tl.todos, [])
            self.assertEqual(tl.progress, "")

    def test_missing_core_dir_returns_error(self) -> None:
        exec_ = self._exec(None)  # type: ignore[arg-type]
        out = asyncio.run(exec_.execute(todos=[]))
        self.assertIn("Error", out)


class HudServiceTodoTest(unittest.TestCase):
    def test_get_hud_surfaces_todo_payload(self) -> None:
        from butterfly.service.hud_service import get_hud

        with TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            system = Path(root) / "_sessions"
            sid = "test-session"
            (sessions / sid / "core").mkdir(parents=True)
            (system / sid).mkdir(parents=True)
            save_todo_list(sessions / sid / "core", TodoList(
                todos=[
                    {"content": "Investigate X", "status": "completed", "activeForm": "Investigating X"},
                    {"content": "Implement Y", "status": "in_progress", "activeForm": "Implementing Y"},
                    {"content": "Review PR", "status": "pending", "activeForm": "Reviewing PR"},
                ],
                iters_since_seen=4,
                reminder_threshold=10,
            ))
            payload = get_hud(sid, sessions, system)
            self.assertIn("todo", payload)
            todo = payload["todo"]
            self.assertIsNotNone(todo)
            self.assertEqual(todo["progress_line"], "[2/3] Implementing Y")
            self.assertEqual(todo["iters_since_seen"], 4)
            self.assertFalse(todo["all_done"])
            self.assertEqual(todo["items"][1]["activeForm"], "Implementing Y")

    def test_get_hud_none_when_no_todo_file(self) -> None:
        from butterfly.service.hud_service import get_hud

        with TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            system = Path(root) / "_sessions"
            sid = "bare-session"
            (sessions / sid).mkdir(parents=True)
            (system / sid).mkdir(parents=True)
            payload = get_hud(sid, sessions, system)
            self.assertIsNone(payload["todo"])


class TodoListServiceTest(unittest.TestCase):
    def test_get_todo_list_returns_full_snapshot(self) -> None:
        from butterfly.service.todo_list_service import get_todo_list

        with TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sid = "s"
            (sessions / sid / "core").mkdir(parents=True)
            save_todo_list(sessions / sid / "core", TodoList(
                todos=[
                    {"content": "a", "status": "in_progress", "activeForm": "Aing"},
                ],
                progress="[1/1] Aing",
                comments="☑ 0. foo",
            ))
            snap = get_todo_list(sid, sessions)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["progress"], "[1/1] Aing")
            self.assertEqual(snap["comments"], "☑ 0. foo")
            self.assertEqual(snap["total"], 1)

    def test_get_todo_list_none_when_missing(self) -> None:
        from butterfly.service.todo_list_service import get_todo_list

        with TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sid = "s"
            (sessions / sid).mkdir(parents=True)
            self.assertIsNone(get_todo_list(sid, sessions))


class ReminderEnqueueTest(unittest.TestCase):
    """Pins the v2.0.37 runtime reminder injection: on_llm_call_end bumps
    iters_since_seen; when ≥ threshold, a wait-mode ChatItem carrying the
    formatted ``<system-reminder>`` is enqueued and the counter resets.
    Pending-count=0 short-circuits (nothing to remind about).
    """

    def _make_session(self, tmp: Path):
        from butterfly.session_engine.session import Session
        base = tmp / "sessions"
        system = tmp / "_sessions"
        sid = "s"
        (base / sid / "core").mkdir(parents=True)
        (system / sid).mkdir(parents=True)
        sess = object.__new__(Session)
        sess._base_dir = base
        sess._session_id = sid
        sess._system_base = system
        return sess

    def test_no_todo_file_is_noop(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            # Should silently no-op — no file, nothing to track.
            sess._tick_todo_list_reminder()
            self.assertIsNone(load_todo_list(sess.core_dir))

    def test_below_threshold_just_increments(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_todo_list(sess.core_dir, TodoList(
                todos=[{"content": "a", "status": "pending", "activeForm": "A"}],
                iters_since_seen=2,
                reminder_threshold=10,
            ))
            enqueued: list = []
            sess._enqueue_todo_list_reminder = lambda todos: enqueued.append(todos)
            sess._tick_todo_list_reminder()
            tl = load_todo_list(sess.core_dir)
            self.assertEqual(tl.iters_since_seen, 3)
            self.assertEqual(enqueued, [])

    def test_threshold_crossed_enqueues_and_resets(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_todo_list(sess.core_dir, TodoList(
                todos=[{"content": "a", "status": "pending", "activeForm": "A"}],
                iters_since_seen=9,
                reminder_threshold=10,
            ))
            enqueued: list = []
            sess._enqueue_todo_list_reminder = lambda todos: enqueued.append(list(todos))
            sess._tick_todo_list_reminder()
            tl = load_todo_list(sess.core_dir)
            self.assertEqual(tl.iters_since_seen, 0)
            self.assertEqual(len(enqueued), 1)
            self.assertEqual(enqueued[0][0]["content"], "a")

    def test_all_completed_does_not_fire_reminder(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_todo_list(sess.core_dir, TodoList(
                todos=[{"content": "a", "status": "completed", "activeForm": "A"}],
                iters_since_seen=100,
                reminder_threshold=10,
            ))
            enqueued: list = []
            sess._enqueue_todo_list_reminder = lambda todos: enqueued.append(todos)
            sess._tick_todo_list_reminder()
            tl = load_todo_list(sess.core_dir)
            # Still increments so the field stays fresh, but no reminder fires.
            self.assertEqual(tl.iters_since_seen, 101)
            self.assertEqual(enqueued, [])


class ReminderInjectionTest(unittest.IsolatedAsyncioTestCase):
    """Exercise ``_enqueue_todo_list_reminder`` end-to-end: a ChatItem with
    the correct source/mode/content is placed on the session's wait
    queue, and a ``todo_list_changed`` event is written. Complements
    ``ReminderEnqueueTest`` (which stubs out this method) by pinning the
    wiring between the tick and the dispatcher queue.
    """

    def _make_session(self, tmp: Path):
        from butterfly.session_engine.session import Session
        base = tmp / "sessions"
        system = tmp / "_sessions"
        sid = "s"
        (base / sid / "core").mkdir(parents=True)
        (system / sid).mkdir(parents=True)
        sess = object.__new__(Session)
        sess._base_dir = base
        sess._session_id = sid
        sess._system_base = system
        sess._ipc = None
        sess._inbox_lock = None
        sess._interrupt_queue = []
        sess._wait_queue = []
        sess._scheduled_task_names = set()
        sess._run_task = None
        # Stub the consumer so the queued reminder sits on the queue for
        # assertion instead of being dispatched into missing agent state.
        async def _noop_consumer() -> None:
            return None
        sess._consumer_loop = _noop_consumer
        sess._consumer_task = None
        return sess

    async def test_reminder_enqueues_chat_item_with_correct_shape(self) -> None:
        from butterfly.session_engine.pending_inputs import ChatItem

        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            todos = [
                {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
                {"content": "Review PR", "status": "pending", "activeForm": "Reviewing PR"},
            ]
            sess._enqueue_todo_list_reminder(todos)
            # ``_enqueue`` was scheduled via ``loop.create_task``; yield so it runs.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(sess._wait_queue), 1)
            item = sess._wait_queue[0]
            self.assertIsInstance(item, ChatItem)
            self.assertEqual(item.mode, "wait")
            self.assertEqual(item.source, "todo_reminder")
            self.assertEqual(item.caller_type, "system")
            self.assertIn("<system-reminder>", item.content)
            self.assertIn("1. [in_progress] Run tests", item.content)
            self.assertIn("2. [pending] Review PR", item.content)
            # Event was written to events.jsonl so the frontend sees the reset.
            events_path = sess._events_path
            self.assertTrue(events_path.exists())
            lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            injections = [e for e in lines if e.get("type") == "todo_list_changed"
                          and e.get("change") == "reminder_injected"]
            self.assertEqual(len(injections), 1)

    async def test_reminder_empty_todos_is_noop(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            sess._enqueue_todo_list_reminder([])
            await asyncio.sleep(0)
            self.assertEqual(sess._wait_queue, [])


# ── v2.0.36 clobber-fix regression (kept, still applies to real task cards) ──

class PersistCardTransitionTest(unittest.TestCase):
    """Regression tests for the v2.0.36 stale-card clobber fix. These now
    exercise the helper on a plain (non-todo) task card — todo lists live
    outside the task-card system since v2.0.37, but the helper still
    guards agent-written fields (progress / comments / description) on
    regular cards.
    """

    def _make_session(self, tmp: Path):
        from butterfly.session_engine.session import Session
        base = tmp / "sessions"
        system = tmp / "_sessions"
        sid = "s"
        (base / sid / "core" / "tasks").mkdir(parents=True)
        (system / sid).mkdir(parents=True)
        sess = object.__new__(Session)
        sess._base_dir = base
        sess._session_id = sid
        sess._system_base = system
        return sess

    def test_persist_transition_preserves_agent_writes(self) -> None:
        from butterfly.session_engine.task_cards import TaskCard, load_card, save_card

        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            card = TaskCard(
                name="heartbeat",
                check_interval=2.0,
                progress="agent wrote [2/3] Implementing Y",
                comments="observation: X",
                description="hourly review",
            )
            save_card(sess.tasks_dir, card)
            stale = TaskCard(name="heartbeat", check_interval=2.0)
            result = sess._persist_card_transition(stale.name, lambda c: c.mark_finished())
            self.assertIsNotNone(result)
            disk = load_card(sess.tasks_dir, "heartbeat")
            # Agent-written fields must survive.
            self.assertEqual(disk.progress, "agent wrote [2/3] Implementing Y")
            self.assertEqual(disk.comments, "observation: X")
            self.assertEqual(disk.description, "hourly review")
            # Runtime-owned status transition landed.
            self.assertEqual(disk.status, "pending")
            self.assertIsNotNone(disk.last_finished_at)

    def test_persist_transition_returns_none_for_missing_card(self) -> None:
        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            self.assertIsNone(sess._persist_card_transition("nope", lambda c: c.mark_working()))

    def test_persist_transition_swallows_mutate_exception(self) -> None:
        from butterfly.session_engine.task_cards import TaskCard, save_card

        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_card(sess.tasks_dir, TaskCard(name="x", check_interval=2.0))
            def bad(_c):
                raise RuntimeError("boom")
            self.assertIsNone(sess._persist_card_transition("x", bad))

    def test_persist_transition_emits_task_card_changed(self) -> None:
        """``emit_change`` surfaces a ``task_card_changed`` event so the
        frontend refreshes the Tasks tab on status transitions driven
        by ``_do_tick`` (``mark_working`` / ``mark_finished`` /
        ``mark_pending``). Without it the UI stayed stuck on the
        pre-tick status until the next manual refresh.
        """
        from butterfly.session_engine.task_cards import TaskCard, save_card

        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_card(sess.tasks_dir, TaskCard(name="x", check_interval=2.0))
            emitted: list[dict] = []
            sess._append_event = emitted.append  # type: ignore[method-assign]
            sess._persist_card_transition(
                "x", lambda c: c.mark_working(), emit_change="started",
            )
            self.assertEqual(
                emitted,
                [{"type": "task_card_changed", "card": "x", "change": "started"}],
            )

    def test_persist_transition_no_emit_when_change_omitted(self) -> None:
        """Default ``emit_change=None`` stays silent — preserves the
        ``_poll_card_script`` ``mark_checked`` call-site which has its
        own ``task_check`` event and doesn't want to double-fire.
        """
        from butterfly.session_engine.task_cards import TaskCard, save_card

        with TemporaryDirectory() as td:
            sess = self._make_session(Path(td))
            save_card(sess.tasks_dir, TaskCard(name="x", check_interval=2.0))
            emitted: list[dict] = []
            sess._append_event = emitted.append  # type: ignore[method-assign]
            sess._persist_card_transition("x", lambda c: c.mark_checked())
            self.assertEqual(emitted, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
