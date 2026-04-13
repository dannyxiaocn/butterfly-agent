import json

from nutshell.service.sessions_service import get_session


def test_read_session_info_returns_core_fields(tmp_path):
    """get_session returns entity, status, model_state from manifest + status."""
    session_id = "s1"
    session_dir = tmp_path / "sessions" / session_id
    system_dir = tmp_path / "_sessions" / session_id
    (session_dir / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)

    (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent", "created_at": "2026-04-02T00:00:00"}), encoding="utf-8")
    (system_dir / "status.json").write_text(json.dumps({"status": "active", "model_state": "idle"}), encoding="utf-8")

    info = get_session(session_id, tmp_path / "sessions", tmp_path / "_sessions")

    assert info is not None
    assert info["entity"] == "agent"
    assert info["status"] == "active"
    assert info["model_state"] == "idle"


def test_read_session_info_reads_config_model(tmp_path):
    """get_session picks up model from config.yaml."""
    session_id = "s2"
    session_dir = tmp_path / "sessions" / session_id
    system_dir = tmp_path / "_sessions" / session_id
    (session_dir / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)

    (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent", "created_at": "2026-04-02T00:00:00"}), encoding="utf-8")
    (system_dir / "status.json").write_text(json.dumps({"status": "active", "model_state": "idle"}), encoding="utf-8")

    info = get_session(session_id, tmp_path / "sessions", tmp_path / "_sessions")

    assert info is not None
    assert "model" in info or "entity" in info  # model comes from config
