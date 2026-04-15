"""Tests for the `butterfly codex login` and `butterfly kimi login` helpers."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ui.cli import login as login_mod


# ── Codex login ─────────────────────────────────────────────────────────────


def _make_codex_args(**overrides) -> argparse.Namespace:
    base = dict(codex_cmd="login", skip_cli=False, no_verify=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_codex_login_missing_cli_prints_install_hint(monkeypatch, capsys):
    """With no codex CLI on PATH, we should print install + login instructions."""
    monkeypatch.setattr(login_mod.shutil, "which", lambda _cmd: None)
    rc = login_mod.cmd_codex(_make_codex_args())
    assert rc == 1
    out = capsys.readouterr().out
    assert "codex CLI not found" in out
    assert login_mod._CODEX_INSTALL_HINT in out
    assert "codex login" in out


def test_codex_login_skip_cli_verifies_existing_auth(monkeypatch, tmp_path, capsys):
    """--skip-cli should bypass subprocess and go straight to verification."""
    fake_auth = tmp_path / "auth.json"
    fake_auth.write_text(json.dumps({
        "tokens": {
            "access_token": "a.b.c",
            "refresh_token": "r.r.r",
            "id_token": "",
        }
    }), encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", fake_auth)
    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        lambda access, id_token="": "acct-123",
    )

    rc = login_mod.cmd_codex(_make_codex_args(skip_cli=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Codex login verified" in out
    assert "acct-123" in out


def test_codex_login_skip_cli_missing_auth_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", tmp_path / "nope.json")
    rc = login_mod.cmd_codex(_make_codex_args(skip_cli=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_codex_login_skip_cli_corrupt_auth_fails(monkeypatch, tmp_path, capsys):
    auth = tmp_path / "auth.json"
    auth.write_text("{{ not json", encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)
    rc = login_mod.cmd_codex(_make_codex_args(skip_cli=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not parse" in err


def test_codex_login_skip_cli_missing_tokens_fails(monkeypatch, tmp_path, capsys):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)
    rc = login_mod.cmd_codex(_make_codex_args(skip_cli=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "missing" in err.lower()


def test_codex_login_runs_subprocess_and_verifies(monkeypatch, tmp_path, capsys):
    """Full happy path: codex CLI present, subprocess returns 0, auth verified."""
    monkeypatch.setattr(login_mod.shutil, "which", lambda _cmd: "/usr/local/bin/codex")

    called: dict[str, object] = {}

    def fake_call(argv):
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(login_mod.subprocess, "call", fake_call)

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "tokens": {"access_token": "a.b.c", "refresh_token": "r", "id_token": ""}
    }), encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)
    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        lambda access, id_token="": "acct-OK",
    )

    rc = login_mod.cmd_codex(_make_codex_args())
    assert rc == 0
    assert called["argv"] == ["/usr/local/bin/codex", "login"]
    assert "Codex login verified" in capsys.readouterr().out


def test_codex_login_no_verify(monkeypatch, capsys):
    monkeypatch.setattr(login_mod.shutil, "which", lambda _cmd: "/usr/local/bin/codex")
    monkeypatch.setattr(login_mod.subprocess, "call", lambda argv: 0)
    rc = login_mod.cmd_codex(_make_codex_args(no_verify=True))
    assert rc == 0
    assert "Skipping verification" in capsys.readouterr().out


def test_codex_login_subprocess_fail_propagates_rc(monkeypatch, capsys):
    monkeypatch.setattr(login_mod.shutil, "which", lambda _cmd: "/usr/local/bin/codex")
    monkeypatch.setattr(login_mod.subprocess, "call", lambda argv: 7)
    rc = login_mod.cmd_codex(_make_codex_args())
    assert rc == 7


# ── Kimi login ──────────────────────────────────────────────────────────────


def _make_kimi_args(**overrides) -> argparse.Namespace:
    base = dict(kimi_cmd="login", env_file=None, key=None, no_verify=True)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_kimi_login_writes_key_to_env(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    # Ensure no pre-existing key so reuse prompt is skipped.
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    rc = login_mod.cmd_kimi(_make_kimi_args(
        env_file=env_file,
        key="sk-kimi-test-123",
        no_verify=True,
    ))
    assert rc == 0
    content = env_file.read_text(encoding="utf-8")
    assert "KIMI_FOR_CODING_API_KEY=sk-kimi-test-123" in content
    # chmod 0600 (owner rw only) — skip on Windows-ish fs.
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode & 0o077 == 0, f"expected 0600 permissions, got {oct(mode)}"
    assert os.environ.get(login_mod._KIMI_ENV_KEY) == "sk-kimi-test-123"


def test_kimi_login_upserts_existing_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OTHER=preserved\nKIMI_FOR_CODING_API_KEY=old-key\nLAST=still-here\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    rc = login_mod.cmd_kimi(_make_kimi_args(
        env_file=env_file,
        key="new-key",
        no_verify=True,
    ))
    assert rc == 0
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert "OTHER=preserved" in lines
    assert "LAST=still-here" in lines
    assert "KIMI_FOR_CODING_API_KEY=new-key" in lines
    # Only one line for the key (upsert, not append).
    assert sum("KIMI_FOR_CODING_API_KEY=" in ln for ln in lines) == 1


def test_kimi_login_empty_key_fails(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(login_mod, "_prompt_secret", lambda _msg: "   ")

    rc = login_mod.cmd_kimi(_make_kimi_args(env_file=env_file, no_verify=True))
    assert rc == 1
    assert not env_file.exists()
    assert "empty key" in capsys.readouterr().err


def test_kimi_login_reuses_env_on_yes(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setenv(login_mod._KIMI_ENV_KEY, "env-existing-key")
    responses = iter([""])  # blank → "yes"
    monkeypatch.setattr(login_mod, "_prompt", lambda _msg: next(responses))

    rc = login_mod.cmd_kimi(_make_kimi_args(env_file=env_file, no_verify=True))
    assert rc == 0
    assert "KIMI_FOR_CODING_API_KEY=env-existing-key" in env_file.read_text(encoding="utf-8")


def test_kimi_login_verify_path_invokes_ping(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    called: dict[str, object] = {}

    def fake_verify(key: str) -> tuple[bool, str]:
        called["key"] = key
        return True, "(4→2 tokens)"

    monkeypatch.setattr(login_mod, "_verify_kimi_key", fake_verify)

    rc = login_mod.cmd_kimi(_make_kimi_args(
        env_file=env_file,
        key="verify-me",
        no_verify=False,
    ))
    assert rc == 0
    assert called["key"] == "verify-me"
    out = capsys.readouterr().out
    assert "verified" in out.lower()


def test_kimi_login_verify_failure_returns_1_but_keeps_key(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    monkeypatch.setattr(
        login_mod,
        "_verify_kimi_key",
        lambda _k: (False, "AuthError: 401"),
    )

    rc = login_mod.cmd_kimi(_make_kimi_args(
        env_file=env_file,
        key="bad-key",
        no_verify=False,
    ))
    assert rc == 1
    # The key is still written (user can retry).
    assert "KIMI_FOR_CODING_API_KEY=bad-key" in env_file.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert "validation failed" in err


# ── Quoting helper ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("simple", "simple"),
        ("with space", '"with space"'),
        ('with"quote', '"with\\"quote"'),
        ("with$var", '"with$var"'),
        ("with#hash", '"with#hash"'),
    ],
)
def test_quote_env_value(value, expected):
    assert login_mod._quote_env_value(value) == expected


# ── End-to-end through argparse ─────────────────────────────────────────────


def test_main_dispatches_codex_login(monkeypatch, capsys):
    """Verify `butterfly codex login --skip-cli --no-verify` wires up correctly."""
    from ui.cli import main as main_mod

    # Avoid real verification — --no-verify short-circuits after confirming
    # skip-cli means no subprocess call.
    monkeypatch.setattr(sys, "argv", ["butterfly", "codex", "login", "--skip-cli", "--no-verify"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 0
    assert "Skipping verification" in capsys.readouterr().out


def test_main_dispatches_kimi_login(monkeypatch, tmp_path, capsys):
    from ui.cli import main as main_mod

    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "butterfly", "kimi", "login",
        "--env-file", str(env_file),
        "--key", "end-to-end-key",
        "--no-verify",
    ])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 0
    assert "KIMI_FOR_CODING_API_KEY=end-to-end-key" in env_file.read_text(encoding="utf-8")
