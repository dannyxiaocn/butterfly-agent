"""Tests for Moonshot KLIP-14 OAuth device-authorization flow in
``butterfly kimi login`` (v2.0.31).

The OAuth path is exercised via mocked httpx.Client responses — no real
network. Every exit path must print the agent-hint so this file also grep-
checks stdout for that line.
"""
from __future__ import annotations

import argparse

import pytest

from ui.cli import login as login_mod


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_kimi_args(**overrides) -> argparse.Namespace:
    base = dict(
        kimi_cmd="login",
        key=None,
        no_verify=True,
        env_file=".env",
        oauth=False,
        api_key=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _Resp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _MockClient:
    """httpx.Client stand-in whose responses are driven by a queue of _Resp."""

    def __init__(self, device_resp: _Resp, poll_resps: list[_Resp]):
        self._device_resp = device_resp
        self._poll_resps = list(poll_resps)

    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *a):
        return False

    def post(self, url: str, **_kw) -> _Resp:
        if "device_authorization" in url:
            return self._device_resp
        if "oauth/token" in url:
            if not self._poll_resps:
                raise RuntimeError("poll queue exhausted in mock")
            return self._poll_resps.pop(0)
        raise RuntimeError(f"unexpected mock URL: {url}")


def _install_httpx_mock(monkeypatch, device_resp: _Resp, poll_resps: list[_Resp]):
    """Replace ``httpx.Client`` + ``httpx.Timeout`` inside login_mod's call site."""
    import httpx as real_httpx

    mock = _MockClient(device_resp, poll_resps)

    def _factory(*_a, **_kw):
        return mock

    monkeypatch.setattr(real_httpx, "Client", _factory)
    monkeypatch.setattr(real_httpx, "Timeout", lambda x: x)
    # Our _sleep indirection lets us bypass real time.sleep during polling.
    monkeypatch.setattr(login_mod, "_sleep", lambda _s: None)


# ── Fast-path: key already present ──────────────────────────────────────────


def test_fastpath_env_var_set_verifies_and_exits(monkeypatch, tmp_path, capsys):
    """Pre-existing env var → verify and exit 0, no OAuth."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(login_mod._KIMI_ENV_KEY, "sk-preset")
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))

    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already set" in out or "already set — verified" in out
    # Agent-hint MUST appear on every path.
    assert "If you are an agent" in out
    assert "KIMI_FOR_CODING_API_KEY from environment" in out


def test_fastpath_env_file_set_verifies_and_exits(monkeypatch, tmp_path, capsys):
    """Pre-existing .env key → verify and exit 0, no OAuth."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("KIMI_FOR_CODING_API_KEY=sk-in-envfile\n")

    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "already set" in out
    assert "If you are an agent" in out


def test_fastpath_no_verify_skips_ping(monkeypatch, tmp_path, capsys):
    """Existing key + --no-verify → skip ping entirely."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(login_mod._KIMI_ENV_KEY, "sk-preset")

    def _fail(_k):
        raise AssertionError("ping must not be called with --no-verify")

    monkeypatch.setattr(login_mod, "_kimi_ping", _fail)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "If you are an agent" in out


def test_fastpath_verify_fail_returns_nonzero(monkeypatch, tmp_path, capsys):
    """Existing key fails verification → rc != 0 + hint printed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(login_mod._KIMI_ENV_KEY, "sk-bad")
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (False, "HTTP 401"))

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc != 0
    assert "If you are an agent" in out


# ── OAuth happy path ────────────────────────────────────────────────────────


def test_oauth_happy_path_pending_then_success(monkeypatch, tmp_path, capsys):
    """pending → success: persists access_token + refresh_token to .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    device_resp = _Resp(
        200,
        {
            "device_code": "dc-abc",
            "user_code": "WXYZ-1234",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=WXYZ-1234",
            "expires_in": 600,
            "interval": 2,
        },
    )
    poll_resps = [
        _Resp(400, {"error": "authorization_pending"}),
        _Resp(200, {
            "access_token": "sk-oauth-abc",
            "refresh_token": "r-xyz",
            "token_type": "Bearer",
            "expires_in": 3600,
        }),
    ]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out

    assert rc == 0
    content = env_file.read_text()
    assert "KIMI_FOR_CODING_API_KEY=sk-oauth-abc" in content
    assert "KIMI_REFRESH_TOKEN=r-xyz" in content
    # Prompt must have shown the verification URL / user code.
    assert "WXYZ-1234" in out or "kimi.com/device" in out
    # Agent-hint MUST appear.
    assert "If you are an agent" in out


def test_oauth_force_oauth_bypasses_existing_key(monkeypatch, tmp_path, capsys):
    """--oauth forces OAuth even when a key already exists."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(login_mod._KIMI_ENV_KEY, "sk-old")
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "AAAA",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=AAAA",
            "expires_in": 600,
            "interval": 1,
        },
    )
    poll_resps = [_Resp(200, {"access_token": "sk-fresh", "refresh_token": "r"})]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(
        key=None, no_verify=False, env_file=str(env_file), oauth=True
    )
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sk-fresh" in env_file.read_text()
    assert "If you are an agent" in out


# ── OAuth slow_down doubles interval ────────────────────────────────────────


def test_oauth_slow_down_doubles_interval(monkeypatch, tmp_path, capsys):
    """slow_down response doubles the poll interval (capped at 15 s)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 600,
            "interval": 2,  # initial
        },
    )
    poll_resps = [
        _Resp(400, {"error": "slow_down"}),
        _Resp(400, {"error": "slow_down"}),
        _Resp(200, {"access_token": "sk-ok", "refresh_token": ""}),
    ]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    recorded_sleeps: list[float] = []
    monkeypatch.setattr(login_mod, "_sleep", lambda s: recorded_sleeps.append(s))

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out

    assert rc == 0
    # Sequence: initial 2s (before 1st poll), then double to 4 (before 2nd), then 8 (before 3rd).
    assert recorded_sleeps == [2, 4, 8], recorded_sleeps
    assert "If you are an agent" in out


def test_oauth_slow_down_caps_at_15s(monkeypatch, tmp_path, capsys):
    """Repeated slow_down must cap the interval at 15 s (not grow unbounded)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.setattr(login_mod, "_kimi_ping", lambda _k: (True, ""))

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 600,
            "interval": 5,
        },
    )
    # 5 slow_downs followed by success. Expected sleeps: 5, 10, 15, 15, 15, 15.
    poll_resps = [
        _Resp(400, {"error": "slow_down"}),
        _Resp(400, {"error": "slow_down"}),
        _Resp(400, {"error": "slow_down"}),
        _Resp(400, {"error": "slow_down"}),
        _Resp(400, {"error": "slow_down"}),
        _Resp(200, {"access_token": "sk-ok"}),
    ]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    recorded_sleeps: list[float] = []
    monkeypatch.setattr(login_mod, "_sleep", lambda s: recorded_sleeps.append(s))

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=False, env_file=str(env_file))
    login_mod.cmd_kimi(args)

    assert recorded_sleeps == [5, 10, 15, 15, 15, 15], recorded_sleeps


# ── OAuth terminal errors ───────────────────────────────────────────────────


def test_oauth_access_denied_exits_nonzero(monkeypatch, tmp_path, capsys):
    """access_denied must abort fast with rc != 0."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 600,
            "interval": 1,
        },
    )
    poll_resps = [_Resp(403, {"error": "access_denied"})]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    captured = capsys.readouterr()

    assert rc != 0
    assert not env_file.exists() or "KIMI_FOR_CODING_API_KEY" not in env_file.read_text()
    assert "access_denied" in captured.err or "access_denied" in captured.out
    # Agent-hint MUST still appear.
    assert "If you are an agent" in captured.out


def test_oauth_expired_token_exits_nonzero(monkeypatch, tmp_path, capsys):
    """expired_token (user took too long on the approval page) must abort."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 600,
            "interval": 1,
        },
    )
    poll_resps = [_Resp(403, {"error": "expired_token"})]
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    captured = capsys.readouterr()

    assert rc != 0
    assert "expired_token" in captured.err or "expired_token" in captured.out
    assert "If you are an agent" in captured.out


def test_oauth_flow_expires_by_deadline(monkeypatch, tmp_path, capsys):
    """If ``expires_in`` elapses without success → print 'expired' and rc=1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 1,  # 1 s window
            "interval": 1,
        },
    )
    # Unlimited pending — but the expires-in deadline should trip first.
    poll_resps = [_Resp(400, {"error": "authorization_pending"})] * 50
    _install_httpx_mock(monkeypatch, device_resp, poll_resps)

    # Simulate time passing: monotonic advances by 2s per sleep call.
    t = [0.0]

    def _mono():
        return t[0]

    def _advance(_s):
        t[0] += 2.0

    monkeypatch.setattr(login_mod.time, "monotonic", _mono)
    monkeypatch.setattr(login_mod, "_sleep", _advance)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "expired" in captured.err.lower()
    assert "If you are an agent" in captured.out


# ── Agent-hint is ALWAYS printed ────────────────────────────────────────────


def test_agent_hint_printed_on_key_arg_success(monkeypatch, tmp_path, capsys):
    """Legacy --key success path must still print the agent-hint."""
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    args = _make_kimi_args(key="sk-scripted", no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "If you are an agent" in out


def test_agent_hint_printed_on_api_key_cancel(monkeypatch, tmp_path, capsys):
    """--api-key + KeyboardInterrupt in getpass → rc=1, hint still printed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    env_file = tmp_path / ".env"
    args = _make_kimi_args(
        key=None, no_verify=True, env_file=str(env_file), api_key=True
    )
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "If you are an agent" in out


def test_agent_hint_printed_on_ctrl_c_during_oauth(monkeypatch, tmp_path, capsys):
    """Ctrl-C during OAuth polling → rc=130, hint still printed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    device_resp = _Resp(
        200,
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://kimi.com/device",
            "verification_uri_complete": "https://kimi.com/device?code=UC",
            "expires_in": 600,
            "interval": 1,
        },
    )
    _install_httpx_mock(monkeypatch, device_resp, [])

    def _raise_kbd(_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(login_mod, "_sleep", _raise_kbd)

    env_file = tmp_path / ".env"
    args = _make_kimi_args(key=None, no_verify=True, env_file=str(env_file))
    rc = login_mod.cmd_kimi(args)
    out = capsys.readouterr().out
    assert rc == 130
    assert "If you are an agent" in out
    assert "OAuth cancelled" in out


# ── Device-id / header stability ────────────────────────────────────────────


def test_stable_device_id_is_16_hex_chars():
    did = login_mod._stable_device_id()
    assert len(did) == 16
    assert all(c in "0123456789abcdef" for c in did)


def test_stable_device_id_deterministic():
    a = login_mod._stable_device_id()
    b = login_mod._stable_device_id()
    assert a == b


def test_kimi_oauth_headers_contain_required_fields():
    headers = login_mod._kimi_oauth_headers()
    for required in (
        "X-Msh-Platform",
        "X-Msh-Version",
        "X-Msh-Device-Name",
        "X-Msh-Device-Model",
        "X-Msh-Device-Id",
        "X-Msh-Os-Version",
    ):
        assert required in headers, f"header {required} missing"
    assert headers["X-Msh-Platform"] == "butterfly"
    assert headers["X-Msh-Device-Model"] == "cli"
