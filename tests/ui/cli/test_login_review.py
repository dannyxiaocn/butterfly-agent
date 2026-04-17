"""Review-round regression tests for ``ui/cli/login.py`` (v2.0.13 interface).

Previous test_login_review.py covered the subprocess + _verify_codex_auth()
interface from v2.0.7 / v2.0.10. That interface was replaced in v2.0.13 with:
  - Built-in OAuth device-code flow (no codex CLI dependency)
  - Butterfly-owned auth store at ~/.butterfly/auth.json
  - Interactive kimi key prompt + .env write

These tests cover the correctness of the new helpers introduced in v2.0.13.
"""
from __future__ import annotations

import json
import time

import pytest

from ui.cli import login as login_mod


# ── 1. _read_butterfly_codex_tokens shape robustness ────────────────────────


def test_read_butterfly_tokens_missing_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", tmp_path / "nope.json")
    assert login_mod._read_butterfly_codex_tokens() is None


def test_read_butterfly_tokens_corrupt_json_returns_none(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{{ not json")
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", auth)
    assert login_mod._read_butterfly_codex_tokens() is None


def test_read_butterfly_tokens_non_dict_tokens_returns_none(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": ["list", "not", "dict"]}))
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", auth)
    assert login_mod._read_butterfly_codex_tokens() is None


def test_read_butterfly_tokens_valid_returns_data(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    data = {"tokens": {"access_token": "a.b.c", "refresh_token": "r"}}
    auth.write_text(json.dumps(data))
    monkeypatch.setattr(login_mod, "_BUTTERFLY_AUTH_PATH", auth)
    result = login_mod._read_butterfly_codex_tokens()
    assert result == data


# ── 2. _is_token_expired ────────────────────────────────────────────────────


def test_is_token_expired_empty_string():
    assert login_mod._is_token_expired("") is True


def test_is_token_expired_non_jwt():
    assert login_mod._is_token_expired("notajwt") is True


def test_is_token_expired_past_exp():
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) - 10}).encode()
    ).rstrip(b"=").decode()
    token = f"h.{payload}.s"
    assert login_mod._is_token_expired(token, buffer_seconds=0) is True


def test_is_token_expired_future_exp():
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    token = f"h.{payload}.s"
    assert login_mod._is_token_expired(token, buffer_seconds=0) is False


# ── 3. _read_codex_cli_tokens ───────────────────────────────────────────────


def test_read_codex_cli_tokens_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", tmp_path / "nope.json")
    assert login_mod._read_codex_cli_tokens() is None


def test_read_codex_cli_tokens_expired_token_returns_none(monkeypatch, tmp_path):
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) - 100}).encode()
    ).rstrip(b"=").decode()
    expired_token = f"h.{payload}.s"
    cli_auth = tmp_path / "auth.json"
    cli_auth.write_text(json.dumps({
        "tokens": {"access_token": expired_token, "refresh_token": "r"}
    }))
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)
    assert login_mod._read_codex_cli_tokens() is None


def test_read_codex_cli_tokens_valid(monkeypatch, tmp_path):
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    fresh_token = f"h.{payload}.s"
    cli_auth = tmp_path / "auth.json"
    cli_auth.write_text(json.dumps({
        "tokens": {"access_token": fresh_token, "refresh_token": "r"}
    }))
    monkeypatch.setattr(login_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)
    result = login_mod._read_codex_cli_tokens()
    assert result is not None
    assert result["access_token"] == fresh_token


# ── 4. device code poll — 403 access_denied fails fast ──────────────────────


def test_device_code_flow_403_access_denied_raises_immediately(monkeypatch):
    """403 with error=access_denied must raise immediately, not loop for 15 min."""
    import httpx as httpx_mod

    class _MockResp:
        status_code = 403
        def json(self):
            return {"error": "access_denied"}

    class _MockDeviceResp:
        status_code = 200
        def json(self):
            return {
                "user_code": "ABCD-1234",
                "device_auth_id": "dev-id-xyz",
                "interval": "1",
            }

    class _MockClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, **kw):
            if "usercode" in url:
                return _MockDeviceResp()
            return _MockResp()

    monkeypatch.setattr(httpx_mod, "Client", _MockClient)
    monkeypatch.setattr(httpx_mod, "Timeout", lambda x: x)

    with pytest.raises(RuntimeError, match="access_denied"):
        login_mod._run_device_code_flow(httpx_mod)


# ── 5. account-id extraction failure is non-fatal ───────────────────────────


def test_codex_success_survives_account_id_failure(monkeypatch, capsys):
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    access_token = f"h.{payload}.s"

    def boom(*_a, **_kw):
        raise RuntimeError("decode failed")

    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        boom,
    )

    rc = login_mod._print_codex_success(access_token)
    assert rc == 0, "account-id failure must NOT block verify success"
    out = capsys.readouterr().out
    assert "verified" in out.lower()
