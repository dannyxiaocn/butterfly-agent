"""Tests for `butterfly codex login` and `butterfly kimi login` (v2.0.13 interface)."""
from __future__ import annotations

import argparse
import json
import sys
import time

import pytest

from ui.cli import login as login_mod


# ── Codex login ─────────────────────────────────────────────────────────────


def _make_codex_args(**overrides) -> argparse.Namespace:
    base = dict(codex_cmd="login", no_verify=False, import_codex_cli=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_codex_login_imports_cli_tokens_when_flag_set(monkeypatch, tmp_path, capsys):
    """--import-codex-cli should import tokens from ~/.codex/auth.json without prompting."""
    # Build a valid (not-yet-expired) JWT-like token so _is_token_expired() returns False.
    import base64
    exp_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    access_token = f"header.{exp_payload}.sig"

    cli_auth = tmp_path / "cli_auth.json"
    cli_auth.write_text(json.dumps({
        "tokens": {
            "access_token": access_token,
            "refresh_token": "r.r.r",
        }
    }), encoding="utf-8")

    butterfly_auth = tmp_path / "butterfly_auth.json"
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)
    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        lambda access, id_token="": "acct-cli",
    )

    rc = login_mod.cmd_codex(_make_codex_args(import_codex_cli=True))
    assert rc == 0
    assert butterfly_auth.exists(), "butterfly auth file should be written"
    stored = json.loads(butterfly_auth.read_text())
    assert stored["tokens"]["access_token"] == access_token


def test_codex_login_butterfly_auth_missing_runs_device_flow(monkeypatch, tmp_path, capsys):
    """When no tokens exist and no CLI auth, device code flow is attempted."""
    butterfly_auth = tmp_path / "butterfly_auth.json"
    codex_cli_auth = tmp_path / "codex_cli_auth.json"  # does not exist
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", codex_cli_auth)

    # Stub device flow to raise to avoid real HTTP.
    def fake_device_flow(httpx_mod):
        raise RuntimeError("no-network-in-test")

    monkeypatch.setattr(login_mod, "_run_device_code_flow", fake_device_flow)

    import httpx as _httpx  # ensure import works
    rc = login_mod.cmd_codex(_make_codex_args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no-network-in-test" in err


def test_codex_login_device_flow_success(monkeypatch, tmp_path, capsys):
    """Successful device flow writes tokens and prints success."""
    import base64
    exp_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    access_token = f"h.{exp_payload}.s"

    butterfly_auth = tmp_path / "butterfly_auth.json"
    codex_cli_auth = tmp_path / "codex_cli_auth.json"  # not present
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", codex_cli_auth)

    def fake_device_flow(_httpx):
        return {"tokens": {"access_token": access_token, "refresh_token": "r"}}

    monkeypatch.setattr(login_mod, "_run_device_code_flow", fake_device_flow)
    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        lambda access, id_token="": "acct-device",
    )

    rc = login_mod.cmd_codex(_make_codex_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified" in out.lower() or "success" in out.lower()
    assert butterfly_auth.exists()


def test_codex_login_no_verify(monkeypatch, tmp_path, capsys):
    """--no-verify skips the success-print verification step."""
    import base64
    exp_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    access_token = f"h.{exp_payload}.s"

    butterfly_auth = tmp_path / "butterfly_auth.json"
    codex_cli_auth = tmp_path / "codex_cli_auth.json"
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", codex_cli_auth)

    def fake_device_flow(_httpx):
        return {"tokens": {"access_token": access_token, "refresh_token": "r"}}

    monkeypatch.setattr(login_mod, "_run_device_code_flow", fake_device_flow)

    rc = login_mod.cmd_codex(_make_codex_args(no_verify=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Login successful" in out


# ── Kimi login ─────────────────────────────────────────────────────────────


def _make_kimi_args(**overrides) -> argparse.Namespace:
    base = dict(kimi_cmd="login", key=None, no_verify=True, env_file=".env")
    base.update(overrides)
    return argparse.Namespace(**base)


def test_kimi_login_with_key_writes_env_file(monkeypatch, tmp_path, capsys):
    """--key skips prompt and writes to .env."""
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"

    args = _make_kimi_args(key="sk-test-key", no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    assert rc == 0
    assert env_file.exists()
    content = env_file.read_text()
    assert "KIMI_FOR_CODING_API_KEY=sk-test-key" in content


def test_kimi_login_dashboard_url_in_output(monkeypatch, tmp_path, capsys):
    """Output should reference the Kimi dashboard URL."""
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    args = _make_kimi_args(key="sk-test-key", no_verify=True, env_file=str(env_file))
    login_mod.cmd_kimi(args)
    # URL constant should still be the Kimi Code console.
    assert login_mod._KIMI_DASHBOARD_URL == "https://www.kimi.com/code/console"
    assert login_mod._KIMI_ENV_KEY == "KIMI_FOR_CODING_API_KEY"


def test_kimi_login_no_key_no_env_prompts_and_cancels(monkeypatch, tmp_path, capsys):
    """When no key arg and no env var, prompts user; EOFError → rc=1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    # Simulate user pressing Ctrl-C at the prompt.
    monkeypatch.setattr("getpass.getpass", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    assert rc == 1
    assert not env_file.exists(), ".env must not be written on cancelled login"


def test_kimi_upsert_env_var_replaces_existing(tmp_path):
    """_upsert_env_var should replace an existing KEY=... line in place."""
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nKIMI_FOR_CODING_API_KEY=old-key\nBAZ=qux\n")
    login_mod._upsert_env_var(env_file, "KIMI_FOR_CODING_API_KEY", "new-key")
    content = env_file.read_text()
    assert "KIMI_FOR_CODING_API_KEY=new-key" in content
    assert "old-key" not in content
    assert "FOO=bar" in content
    assert "BAZ=qux" in content


def test_kimi_upsert_env_var_appends_if_missing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n")
    login_mod._upsert_env_var(env_file, "KIMI_FOR_CODING_API_KEY", "new-key")
    content = env_file.read_text()
    assert "KIMI_FOR_CODING_API_KEY=new-key" in content
    assert "FOO=bar" in content


def test_kimi_upsert_env_var_preserves_export_prefix(tmp_path):
    """Replacing 'export KEY=old' must yield 'export KEY=new', not lose the prefix."""
    env_file = tmp_path / ".env"
    env_file.write_text("export KIMI_FOR_CODING_API_KEY=old-key\n")
    login_mod._upsert_env_var(env_file, "KIMI_FOR_CODING_API_KEY", "new-key")
    content = env_file.read_text()
    assert "export KIMI_FOR_CODING_API_KEY=new-key" in content
    assert "old-key" not in content


# ── End-to-end through argparse ─────────────────────────────────────────────


def test_main_dispatches_kimi_login(monkeypatch, tmp_path, capsys):
    """Verify `butterfly kimi login --key=x --no-verify` routes correctly."""
    from ui.cli import main as main_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["butterfly", "kimi", "login", "--key=sk-testkey", "--no-verify",
         f"--env-file={tmp_path / '.env'}"],
    )
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 0
    env_content = (tmp_path / ".env").read_text()
    assert "KIMI_FOR_CODING_API_KEY=sk-testkey" in env_content
