from __future__ import annotations

import json

import pytest

from nutshell.tool_engine.providers.archive_session import archive_session


@pytest.mark.asyncio
async def test_archive_session_copies_audit_to_meta_dir(tmp_path):
    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"
    archived_base = tmp_path / "_archived"

    session_id = "s1"
    entity = "agent"

    (sessions_base / session_id / "core").mkdir(parents=True)
    (system_base / session_id).mkdir(parents=True)
    (system_base / f"{entity}_meta" / "core").mkdir(parents=True)

    audit_content = '{"tool":"bash","usage":{"input_tokens":10,"output_tokens":5}}\\n'
    (sessions_base / session_id / "core" / "audit.jsonl").write_text(audit_content, encoding="utf-8")
    (system_base / session_id / "manifest.json").write_text(json.dumps({"entity": entity}), encoding="utf-8")

    result = await archive_session(
        session_id,
        _sessions_base=sessions_base,
        _system_base=system_base,
        _archived_base=archived_base,
    )

    assert result == "archived s1"
    copied = system_base / f"{entity}_meta" / "core" / "audit" / f"{session_id}.jsonl"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == audit_content
