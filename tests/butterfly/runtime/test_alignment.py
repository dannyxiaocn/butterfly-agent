"""Live vs replay alignment — I7 invariant under concurrent append.

DESIGN.md §2 I7: "Live SSE and history replay emit the exact same
event payloads for the same event." The sibling ``test_llm_context.py``
already has a static-sequence pin (``test_alignment_live_equals_replay``)
— this file extends it with a CONCURRENT-APPEND scenario: while a
reader is actively iterating ``read_events``, a writer is appending new
events. The replay done at every interim cursor MUST produce a prefix
of the final sequence, with no reorder, no gap, and no drift in the
materialised LLM context.

The property under test is the composition of three invariants:

  * I1 append-only + I8 monotonic ids (events.py)
  * I3 LLM context = ``build_llm_context(read_events(...))`` (llm_context.py)
  * I4 builder purity (same events in → same messages out)

Any of those three breaking would allow the live tail and the replay
to diverge. One test is enough to catch it.
"""
from __future__ import annotations

import threading
from pathlib import Path

from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_USER_INPUT,
    append_event,
    latest_event_id,
    read_events,
)
from butterfly.runtime.llm_context import build_llm_context


def test_alignment_prefix_property_under_concurrent_append(tmp_path: Path) -> None:
    """Snapshot the LLM context at 10 interim points while a writer is
    appending 40 events concurrently. Every snapshot MUST be a prefix
    of the final LLM context — demonstrating that the reader sees a
    consistent append-only tail regardless of when it runs.

    We enforce the "prefix" check via ``to_dict()`` equality on the
    leading messages, which is the same comparison every frontend SSE
    consumer + history replay runs against each other.
    """
    # Canonical event mix — covers every for_llm role so message
    # grouping + the role-switch flush path are exercised.
    def _write_one(idx: int) -> None:
        if idx % 4 == 0:
            append_event(tmp_path, EVENT_USER_INPUT, {
                "text": f"q{idx}", "source": "cli",
                "caller": None, "display_name": None,
            })
        elif idx % 4 == 1:
            append_event(tmp_path, EVENT_AGENT_TEXT, {
                "text": f"a{idx}", "model": "m",
            })
        elif idx % 4 == 2:
            append_event(tmp_path, EVENT_AGENT_TOOL_CALL, {
                "tool_use_id": f"tu{idx}", "tool_name": "bash",
                "args": {"command": f"echo {idx}"},
            })
        else:
            append_event(tmp_path, EVENT_AGENT_TOOL_RESULT, {
                "tool_use_id": f"tu{idx - 1}", "tool_name": "bash",
                "result": f"ok{idx}", "is_error": False,
                "is_background": False, "duration_ms": 0.1,
            })

    snapshots: list[list[dict]] = []
    stop = threading.Event()

    def reader() -> None:
        # Take a snapshot each time the latest id advances.
        last_seen = 0
        while not stop.is_set() or last_seen < 40:
            curr = latest_event_id(tmp_path)
            if curr > last_seen:
                msgs = build_llm_context(read_events(tmp_path))
                snapshots.append([m.to_dict() for m in msgs])
                last_seen = curr
            if last_seen >= 40:
                break

    def writer() -> None:
        for i in range(40):
            _write_one(i)
        stop.set()

    t_reader = threading.Thread(target=reader)
    t_writer = threading.Thread(target=writer)
    t_reader.start()
    t_writer.start()
    t_writer.join(timeout=10)
    t_reader.join(timeout=10)

    # Final snapshot = ground truth.
    final_msgs = [m.to_dict() for m in build_llm_context(read_events(tmp_path))]

    # Every intermediate snapshot must be a prefix of the final
    # message list (after message-grouping the message count may shrink
    # vs event count; prefix means: first N messages match byte-for-byte,
    # tail is missing).
    for snap in snapshots:
        self_prefix = final_msgs[: len(snap)]
        # Because grouping can merge a new same-role block into the
        # last message of a snapshot, the LAST message of a snapshot
        # may legitimately be extended in the final. So we check:
        #   - every message before the last is byte-equal
        #   - the last message's role matches, and its content is a
        #     prefix of the final's matching message content
        if not snap:
            continue
        assert snap[:-1] == self_prefix[:-1], (
            f"non-prefix divergence before last msg:\n"
            f"snap: {snap[:-1]!r}\nfinal_prefix: {self_prefix[:-1]!r}"
        )
        last_snap = snap[-1]
        last_final = final_msgs[len(snap) - 1]
        assert last_snap["role"] == last_final["role"]
        # Content prefix: first len(last_snap["content"]) blocks equal.
        assert last_snap["content"] == last_final["content"][: len(last_snap["content"])], (
            f"last-msg content diverged:\n"
            f"snap_last: {last_snap!r}\nfinal_at_idx: {last_final!r}"
        )

    # And finally: the snapshot count is > 1 (the reader actually saw
    # interim states). If the writer finished before the reader's first
    # loop, this test wouldn't be testing anything.
    assert len(snapshots) >= 2, (
        f"reader took {len(snapshots)} snapshots; test did not observe "
        "concurrent interleaving"
    )


def test_read_events_and_build_llm_context_are_deterministic(tmp_path: Path) -> None:
    """Call the pair N times in a row on a fixed disk state; every
    invocation must produce byte-identical Messages. Pins I4 (purity)
    against accidental caching / clock / RNG leaks."""
    for i in range(10):
        append_event(tmp_path, EVENT_USER_INPUT, {
            "text": f"q{i}", "source": "cli", "caller": None, "display_name": None,
        })
        append_event(tmp_path, EVENT_AGENT_TEXT, {"text": f"a{i}", "model": "m"})

    reference = [m.to_dict() for m in build_llm_context(read_events(tmp_path))]
    for _ in range(5):
        again = [m.to_dict() for m in build_llm_context(read_events(tmp_path))]
        assert again == reference
