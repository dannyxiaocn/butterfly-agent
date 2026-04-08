from __future__ import annotations

from pathlib import Path

from nutshell.runtime.cap import CAP


def test_cap_primitives_declared(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    assert cap.primitives() == ("handshake", "lock", "broadcast", "heartbeat-sync")


def test_handshake_roundtrip(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    event = cap.handshake("git", "sess-a", "sess-b", metadata={"repo": "x"})
    assert event["protocol"] == "git"
    stored = cap.get_handshake("git", "sess-a", "sess-b")
    assert stored is not None
    assert stored["metadata"]["repo"] == "x"


def test_lock_acquire_busy_release(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    acquired = cap.acquire_lock("git:repo", "sess-a", metadata={"scope": "push"})
    assert acquired["state"] == "acquired"
    busy = cap.acquire_lock("git:repo", "sess-b")
    assert busy["state"] == "busy"
    assert busy["owner_session"] == "sess-a"
    released = cap.release_lock("git:repo", "sess-a")
    assert released["state"] == "released"
    free = cap.get_lock("git:repo")
    assert free["state"] == "free"


def test_broadcast_append_and_filter(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    cap.broadcast("team", "sess-a", "hello")
    cap.broadcast("alerts", "sess-b", "warn")
    all_events = cap.list_broadcasts()
    assert len(all_events) == 2
    team_events = cap.list_broadcasts("team")
    assert len(team_events) == 1
    assert team_events[0]["content"] == "hello"


def test_heartbeat_sync_roundtrip(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    cap.sync_heartbeat("sess-a", heartbeat_at="2026-04-02T14:00:00", metadata={"state": "idle"})
    hb = cap.get_heartbeat("sess-a")
    assert hb is not None
    assert hb["heartbeat_at"] == "2026-04-02T14:00:00"
    assert hb["metadata"]["state"] == "idle"


def test_git_protocol_adapter_returns_coordinator(tmp_path: Path):
    cap = CAP(system_base=tmp_path / "_sessions")
    coordinator = cap.git_protocol()
    from nutshell.runtime.git_coordinator import GitCoordinator
    assert isinstance(coordinator, GitCoordinator)
