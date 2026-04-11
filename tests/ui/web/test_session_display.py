import json

from ui.web.sessions import _read_session_info


def test_read_session_info_defaults_session_type(tmp_path):
    session_id = "s1"
    session_dir = tmp_path / "sessions" / session_id
    system_dir = tmp_path / "_sessions" / session_id
    (session_dir / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)

    (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent", "created_at": "2026-04-02T00:00:00"}), encoding="utf-8")
    (system_dir / "status.json").write_text(json.dumps({"status": "active", "model_state": "idle"}), encoding="utf-8")
    (session_dir / "core" / "params.json").write_text("{}", encoding="utf-8")

    info = _read_session_info(session_dir, system_dir)

    assert info is not None
    assert info["session_type"] == "default"


def test_read_session_info_reads_session_type_persistent(tmp_path):
    session_id = "s2"
    session_dir = tmp_path / "sessions" / session_id
    system_dir = tmp_path / "_sessions" / session_id
    (session_dir / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)

    (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent", "created_at": "2026-04-02T00:00:00"}), encoding="utf-8")
    (system_dir / "status.json").write_text(json.dumps({"status": "active", "model_state": "idle"}), encoding="utf-8")
    (session_dir / "core" / "params.json").write_text(json.dumps({"session_type": "persistent"}), encoding="utf-8")

    info = _read_session_info(session_dir, system_dir)

    assert info is not None
    assert info["session_type"] == "persistent"
