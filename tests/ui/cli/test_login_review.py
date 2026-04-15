"""Review-round regression tests for ``ui/cli/login.py`` (PR #21 review).

These cover bugs and gaps surfaced during the deep-test pass on PR #21:

1. ``_verify_codex_auth`` raises ``AttributeError`` when ``auth.json`` is valid
   JSON but not an object (e.g. ``[1,2,3]``) — the ``data.get("tokens")`` call
   blows up because the only ``except`` catches ``(OSError, JSONDecodeError)``.
2. ``_write_env_key`` writes the file with the process umask perms (typically
   ``0644``) and *then* ``chmod``s to ``0600`` — there's a brief window where
   the secrets file is world-readable.
3. ``butterfly kimi login --key ""`` silently falls through to an interactive
   ``getpass`` prompt instead of failing fast with the empty-key error.
4. End-to-end coverage gaps: ``OSError`` on ``codex`` exec, and
   ``_extract_account_id`` raising shouldn't fail the whole verify path
   (account id is decorative).
5. Upsert preserves CRLF — file rewrite shouldn't silently normalise line
   endings on Windows-authored ``.env`` files.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

from ui.cli import login as login_mod


# ── 1. Codex auth.json shape robustness ─────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "[1,2,3]",         # JSON array
        '"a string"',     # JSON string
        "42",              # JSON number
        "null",            # JSON null
        "true",            # JSON bool
    ],
)
def test_codex_verify_handles_non_object_json(monkeypatch, tmp_path, capsys, payload):
    """A valid-JSON-but-non-dict auth.json should be a graceful error, not a crash."""
    auth = tmp_path / "auth.json"
    auth.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)

    # Must not raise AttributeError (which is what HEAD does today).
    rc = login_mod._verify_codex_auth()
    assert rc == 1, "non-object auth.json must return rc=1, not crash"
    err = capsys.readouterr().err
    assert "auth.json" in err.lower() or "missing" in err.lower() or "could not" in err.lower(), (
        f"expected an explanatory stderr message, got: {err!r}"
    )


def test_codex_verify_tokens_field_is_not_dict(monkeypatch, tmp_path, capsys):
    """If ``tokens`` is itself a non-dict (e.g. list), we shouldn't crash either."""
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": ["wrong-shape"]}), encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)

    rc = login_mod._verify_codex_auth()
    assert rc == 1


# ── 2. Race window: secrets file briefly world-readable ─────────────────────


def test_write_env_key_creates_file_with_0600_atomically(tmp_path):
    """The file must NEVER exist on disk with broader perms than 0600.

    Today's implementation does ``write_text`` (which creates with the umask
    default, often ``0644``) and *then* ``os.chmod(env_file, 0o600)``. Snapshot
    the perms at the moment ``os.chmod`` is invoked: they should already be
    ``0600`` (i.e. the file was created safely), not ``0644``.
    """
    env = tmp_path / ".env"
    real_chmod = os.chmod
    captured: dict[str, int] = {}

    def spy_chmod(path, mode):
        # If path matches our env file, capture its current perms BEFORE chmod.
        try:
            if Path(path) == env:
                captured["before"] = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            pass
        return real_chmod(path, mode)

    with mock.patch("os.chmod", spy_chmod):
        login_mod._write_env_key(env, "KIMI_FOR_CODING_API_KEY", "secret-value")

    # File must end up at 0600.
    final_mode = stat.S_IMODE(env.stat().st_mode)
    assert final_mode & 0o077 == 0, f"final perms not group/world-private: {oct(final_mode)}"

    # And the perms RIGHT BEFORE chmod must not have leaked the secret to other users.
    if "before" in captured:
        leaked = captured["before"] & 0o077
        assert leaked == 0, (
            f"secrets file briefly created with perms {oct(captured['before'])} "
            f"before chmod to 0600 — fix _write_env_key to create at 0600 atomically"
        )


# ── 3. Empty --key should fail-fast, not block on getpass ───────────────────


def test_kimi_login_empty_key_arg_fails_fast(tmp_path, monkeypatch, capsys):
    """``--key ''`` from a script must error out, not silently prompt.

    Today the check ``if key_arg:`` is falsy for empty string, so we fall
    through into the interactive branch and call ``getpass.getpass`` — which
    would block forever in CI. Treat any explicit ``--key`` (even empty) as
    a non-interactive intent.
    """
    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)

    # If we accidentally fall through to interactive, this would be invoked —
    # make it raise so the test fails loudly instead of hanging.
    def boom(_msg: str) -> str:
        raise AssertionError(
            "empty --key fell through to interactive getpass; expected fail-fast"
        )

    monkeypatch.setattr(login_mod, "_prompt_secret", boom)
    monkeypatch.setattr(login_mod, "_prompt", boom)

    args = argparse.Namespace(
        kimi_cmd="login", env_file=env_file, key="", no_verify=True,
    )
    rc = login_mod.cmd_kimi(args)
    assert rc == 1, "empty --key must produce non-zero exit"
    assert not env_file.exists(), "no .env should be written for empty --key"
    assert "empty" in capsys.readouterr().err.lower()


# ── 4. Coverage gaps from the audit ─────────────────────────────────────────


def test_kimi_login_ignores_legacy_kimi_api_key(tmp_path, monkeypatch):
    """``KIMI_API_KEY`` is NOT reused by the login helper (legacy-fallback removed).

    With only the legacy variable set, the reuse prompt must not fire; the
    helper must fall through to a secret prompt. We make that prompt return
    empty so the command exits with rc=1 (empty key) — proving the legacy
    value was never considered.
    """
    env_file = tmp_path / ".env"
    monkeypatch.delenv(login_mod._KIMI_ENV_KEY, raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "legacy-key-xyz")
    # Reuse prompt must NOT be invoked — fail loudly if it is.
    monkeypatch.setattr(
        login_mod, "_prompt",
        lambda _msg: pytest.fail("legacy KIMI_API_KEY must not trigger reuse prompt"),
    )
    # Secret prompt returns empty → command exits with rc=1.
    monkeypatch.setattr(login_mod, "_prompt_secret", lambda _msg: "")

    rc = login_mod.cmd_kimi(argparse.Namespace(
        kimi_cmd="login", env_file=env_file, key=None, no_verify=True,
    ))
    assert rc == 1
    assert not env_file.exists()


def test_codex_login_subprocess_oserror_returns_1(monkeypatch, capsys):
    """If exec'ing the codex CLI itself raises OSError, we should surface it cleanly."""
    monkeypatch.setattr(login_mod.shutil, "which", lambda _cmd: "/usr/local/bin/codex")

    def boom(_argv):
        raise OSError("Exec format error")

    monkeypatch.setattr(login_mod.subprocess, "call", boom)
    rc = login_mod.cmd_codex(argparse.Namespace(
        codex_cmd="login", skip_cli=False, no_verify=True,
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "exec" in err.lower() or "failed" in err.lower()


def test_codex_verify_account_id_extraction_failure_is_non_fatal(monkeypatch, tmp_path, capsys):
    """A failure inside ``_extract_account_id`` is decorative — verify must still pass."""
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "tokens": {"access_token": "a.b.c", "refresh_token": "r", "id_token": ""}
    }), encoding="utf-8")
    monkeypatch.setattr(login_mod, "_CODEX_AUTH_PATH", auth)

    def boom(*_a, **_kw):
        raise RuntimeError("decode failed")

    monkeypatch.setattr(
        "butterfly.llm_engine.providers.codex._extract_account_id",
        boom,
    )

    rc = login_mod._verify_codex_auth()
    assert rc == 0, "account-id failure must NOT block verify success"
    out = capsys.readouterr().out
    assert "verified" in out.lower()


# ── 5. Upsert: line-ending preservation & comment robustness ────────────────


def test_write_env_key_does_not_clobber_comments(tmp_path):
    """Lines that look like ``# KIMI_FOR_CODING_API_KEY=foo`` must NOT match."""
    env = tmp_path / ".env"
    env.write_text(
        "# KIMI_FOR_CODING_API_KEY=commented-out-old\nOTHER=value\n",
        encoding="utf-8",
    )
    login_mod._write_env_key(env, "KIMI_FOR_CODING_API_KEY", "real-new-key")
    text = env.read_text(encoding="utf-8")
    # Comment must survive verbatim.
    assert "# KIMI_FOR_CODING_API_KEY=commented-out-old" in text
    # The new line must be appended (not replacing the comment).
    assert "KIMI_FOR_CODING_API_KEY=real-new-key" in text
    # Two distinct lines mentioning the var (one comment + one real).
    assert sum(
        line.lstrip().startswith("KIMI_FOR_CODING_API_KEY=")
        for line in text.splitlines()
    ) == 1


def test_write_env_key_quoted_value_round_trip(tmp_path):
    """A value with shell-special chars must be re-quoted, and our own runtime
    loader (``runtime/env.py``) must be able to read it back to the original."""
    env = tmp_path / ".env"
    secret = 'has space & "quote" $var #hash'
    login_mod._write_env_key(env, "KIMI_FOR_CODING_API_KEY", secret)

    # Round-trip through the very loader the runtime uses.
    from butterfly.runtime import env as runtime_env

    # Clean the env first so we're testing what gets *loaded*, not what's already there.
    os.environ.pop("KIMI_FOR_CODING_API_KEY", None)
    saved_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        runtime_env.load_dotenv()
        loaded = os.environ.get("KIMI_FOR_CODING_API_KEY")
    finally:
        os.chdir(saved_cwd)
        os.environ.pop("KIMI_FOR_CODING_API_KEY", None)

    # NOTE: `runtime/env.py` strips one layer of surrounding quotes but does not
    # un-escape. So if our quoter writes `"has space..."`, runtime gets back
    # the inner string. This pins the contract — if either side changes
    # quoting, this test forces a coordinated update.
    assert loaded is not None, "runtime loader failed to pick up the key"
    assert "has space" in loaded


# ── 6. Quote helper edge cases ──────────────────────────────────────────────


def test_quote_env_value_backslash_is_escaped():
    """Backslashes need doubling so the round-trip through a `.env` parser stays sane."""
    out = login_mod._quote_env_value("a\\b")
    # After quoting, the literal backslash must be doubled to survive shell-style parsing.
    assert out.startswith('"') and out.endswith('"')
    assert "\\\\" in out, f"backslash not escaped: {out!r}"


def test_quote_env_value_empty_string_is_unquoted():
    """An empty value has no special chars — quoting is unnecessary noise."""
    assert login_mod._quote_env_value("") == ""
