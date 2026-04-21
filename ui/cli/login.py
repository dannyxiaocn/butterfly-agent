"""One-command login helpers for Codex and Kimi providers.

Exposes two subcommands:

* ``butterfly codex login`` — runs a built-in OpenAI device-code OAuth flow
  (no dependency on the ``codex`` CLI) and stores tokens in butterfly's own
  ``~/.butterfly/auth.json``.  On first run it can also import existing tokens
  from ``~/.codex/auth.json`` to avoid requiring a fresh login.  This mirrors
  the approach used by hermes-agent: butterfly owns its own OAuth session so
  refresh-token rotation by Codex CLI / VS Code never invalidates butterfly's
  credentials.

* ``butterfly kimi login`` — auto-selects between two auth paths:

    1. If a ``KIMI_FOR_CODING_API_KEY`` is already set (env or ``.env``),
       fast-path: ping-verify and exit 0.
    2. Otherwise, run Moonshot's KLIP-14 OAuth device-authorization flow
       and store the resulting access_token as ``KIMI_FOR_CODING_API_KEY``
       in ``.env`` (plus ``KIMI_REFRESH_TOKEN`` if Moonshot ships one).

  Legacy interactive getpass key-paste is still available via ``--api-key``.
  Every exit path prints a final agent-hint line so sub-agents reading stdout
  know how to ask a human for a key instead of retrying OAuth in a loop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

_BUTTERFLY_AUTH_PATH = Path.home() / ".butterfly" / "auth.json"
_CODEX_CLI_AUTH_PATH = Path.home() / ".codex" / "auth.json"

# OpenAI device-code OAuth endpoints (same as hermes-agent)
_CODEX_DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
_CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_DEVICE_AUTH_URL = "https://auth.openai.com/codex/device"

_KIMI_DASHBOARD_URL = "https://www.kimi.com/code/console"
_KIMI_ENV_KEY = "KIMI_FOR_CODING_API_KEY"
_KIMI_REFRESH_ENV_KEY = "KIMI_REFRESH_TOKEN"
_KIMI_VERIFY_URL = "https://api.kimi.com/coding/v1/chat/completions"
# Ping-model: the live model name served at `/coding/v1/`. Passing a bad model
# name would produce a 400 which _kimi_ping still treats as "key accepted", but
# using the real name keeps logs tidy and matches Phase 1B work.
_KIMI_VERIFY_MODEL = "kimi-for-coding"

# Moonshot KLIP-14 OAuth device-authorization endpoints. Client-id is the public
# kimi-cli client; we reuse it since butterfly is a CLI tool of the same shape.
_KIMI_OAUTH_DEVICE_URL = "https://auth.kimi.com/api/oauth/device_authorization"
_KIMI_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
_KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_OAUTH_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_KIMI_OAUTH_POLL_INTERVAL_CAP = 15  # seconds (cap on slow_down doubling)

# Final line printed by every `butterfly kimi login` exit path (success,
# failure, skip). Agents reading stdout must see this hint so they know how to
# request a key from a human instead of retrying OAuth in a loop.
_KIMI_AGENT_HINT_LINE_1 = (
    "If you are an agent: please ask your human for a Kimi Code API key "
    "or have them run this command to OAuth-login."
)
_KIMI_AGENT_HINT_LINE_2 = (
    "   If you already have the key: set it in the .env file as:  "
    "KIMI_FOR_CODING_API_KEY=sk-..."
)
_KIMI_AGENT_HINT_LINE_3 = (
    "   The provider reads KIMI_FOR_CODING_API_KEY from environment "
    "or from .env in the current directory."
)


def _print_kimi_agent_hint() -> None:
    """Print the final agent-hint line block (stable, grep-testable)."""
    print()
    print("Tip: " + _KIMI_AGENT_HINT_LINE_1)
    print(_KIMI_AGENT_HINT_LINE_2)
    print(_KIMI_AGENT_HINT_LINE_3)


# ── `butterfly codex login` ──────────────────────────────────────────────────


def _add_codex_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "codex",
        allow_abbrev=False,
        help="Codex (ChatGPT-OAuth) provider helpers.",
        description=(
            "Codex provider helpers.\n\n"
            "Subcommands:\n"
            "  butterfly codex login      OAuth login via device code flow\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    csub = p.add_subparsers(dest="codex_cmd", metavar="COMMAND")
    csub.required = True

    login = csub.add_parser(
        "login",
        allow_abbrev=False,
        help="Authenticate with OpenAI Codex via device code OAuth.",
        description=(
            "One-command Codex OAuth login (no Codex CLI required).\n\n"
            "Butterfly runs its own OAuth device-code flow and stores tokens in\n"
            f"  {_BUTTERFLY_AUTH_PATH}\n\n"
            "This keeps butterfly's session independent from the Codex CLI and\n"
            "VS Code extension — refresh-token rotation in those tools will no\n"
            "longer invalidate butterfly's credentials.\n\n"
            "If an existing ~/.codex/auth.json is found you will be offered the\n"
            "option to import it instead of running a fresh login.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip token verification after login.",
    )
    login.add_argument(
        "--import-codex-cli",
        action="store_true",
        help="Import tokens from ~/.codex/auth.json without prompting.",
    )

    p.set_defaults(func=cmd_codex)


def cmd_codex(args) -> int:
    if args.codex_cmd == "login":
        return _codex_login(
            verify=not args.no_verify,
            import_codex_cli=args.import_codex_cli,
        )
    return 2


def _codex_login(*, verify: bool, import_codex_cli: bool) -> int:
    """Run the full Codex login flow, storing tokens in ~/.butterfly/auth.json."""
    import httpx

    # Check for already-valid butterfly tokens.
    existing = _read_butterfly_codex_tokens()
    if existing:
        access = existing.get("tokens", {}).get("access_token", "")
        if access and not _is_token_expired(access):
            print("Existing Codex credentials found in butterfly auth store.")
            try:
                reuse = input("Use existing credentials? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                reuse = "y"
            if reuse in ("", "y", "yes"):
                return _print_codex_success(access)

    # Offer to import from ~/.codex/auth.json (Codex CLI's file).
    if import_codex_cli or _CODEX_CLI_AUTH_PATH.exists():
        cli_tokens = _read_codex_cli_tokens()
        if cli_tokens:
            if import_codex_cli:
                do_import = True
            else:
                print(f"Found existing Codex CLI credentials at {_CODEX_CLI_AUTH_PATH}")
                print("Butterfly will create its own session to avoid refresh-token conflicts.")
                try:
                    ans = input("Import these credentials now? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "n"
                do_import = ans in ("y", "yes")
            if do_import:
                _write_butterfly_codex_tokens({"tokens": cli_tokens})
                access = cli_tokens.get("access_token", "")
                print("Credentials imported.")
                if verify:
                    return _print_codex_success(access)
                return 0

    # Run a fresh device-code OAuth flow.
    print()
    print("Signing in to OpenAI Codex (device code flow)...")
    print("Butterfly creates its own session — won't affect Codex CLI or VS Code.")
    print()

    try:
        creds = _run_device_code_flow(httpx)
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    tokens = creds["tokens"]
    _write_butterfly_codex_tokens({"tokens": tokens})

    access = tokens.get("access_token", "")
    if verify:
        return _print_codex_success(access)
    print()
    print("Login successful!")
    print(f"  auth file: {_BUTTERFLY_AUTH_PATH}")
    return 0


def _run_device_code_flow(httpx_module) -> dict:
    """Perform OpenAI device-code OAuth and return a tokens dict."""
    # Step 1: request a device code.
    try:
        with httpx_module.Client(timeout=httpx_module.Timeout(15.0)) as client:
            resp = client.post(
                _CODEX_DEVICE_USERCODE_URL,
                json={"client_id": _CODEX_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        raise RuntimeError(f"Failed to request device code: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"Device code request returned status {resp.status_code}: {resp.text[:200]}"
        )

    device_data = resp.json()
    user_code = device_data.get("user_code", "")
    device_auth_id = device_data.get("device_auth_id", "")
    poll_interval = max(3, int(device_data.get("interval", "5")))

    if not user_code or not device_auth_id:
        raise RuntimeError("Device code response is missing required fields.")

    # Step 2: show the user the code.
    print("To continue, follow these steps:")
    print()
    print(f"  1. Open: \033[94m{_CODEX_DEVICE_AUTH_URL}\033[0m")
    print(f"  2. Enter code: \033[94m{user_code}\033[0m")
    print()
    print("Waiting for sign-in... (Ctrl+C to cancel)")

    # Step 3: poll until authorized.
    max_wait = 15 * 60
    start = time.monotonic()
    code_resp = None

    with httpx_module.Client(timeout=httpx_module.Timeout(15.0)) as client:
        while time.monotonic() - start < max_wait:
            time.sleep(poll_interval)
            poll = client.post(
                _CODEX_DEVICE_TOKEN_URL,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if poll.status_code == 200:
                code_resp = poll.json()
                break
            elif poll.status_code == 404:
                continue  # authorization still pending
            elif poll.status_code == 403:
                # 403 can mean either "still pending" or a terminal denial.
                # Check the response body for known terminal error codes before
                # continuing — if the user clicked "Deny" we should fail fast
                # rather than poll for 15 minutes.
                try:
                    err_data = poll.json()
                    err_code = err_data.get("error", "")
                except Exception:
                    err_code = ""
                if err_code in ("access_denied", "expired_token"):
                    raise RuntimeError(
                        f"Device auth rejected by server: {err_code}. "
                        "Run `butterfly codex login` again to start a new flow."
                    )
                continue  # still pending (no terminal error in body)
            else:
                raise RuntimeError(
                    f"Device auth polling returned status {poll.status_code}."
                )

    if code_resp is None:
        raise RuntimeError("Login timed out after 15 minutes.")

    # Step 4: exchange authorization code for tokens.
    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    redirect_uri = "https://auth.openai.com/deviceauth/callback"

    if not authorization_code or not code_verifier:
        raise RuntimeError(
            "Device auth response is missing authorization_code or code_verifier."
        )

    with httpx_module.Client(timeout=httpx_module.Timeout(15.0)) as client:
        token_resp = client.post(
            _CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": redirect_uri,
                "client_id": _CODEX_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({token_resp.status_code}): {token_resp.text[:200]}"
        )

    result = token_resp.json()
    if "access_token" not in result or "refresh_token" not in result:
        raise RuntimeError("Token exchange response is missing access_token or refresh_token.")

    tokens = {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
    }
    if "id_token" in result:
        tokens["id_token"] = result["id_token"]

    return {"tokens": tokens}


def _read_butterfly_codex_tokens() -> dict | None:
    if not _BUTTERFLY_AUTH_PATH.exists():
        return None
    try:
        data = json.loads(_BUTTERFLY_AUTH_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            return data
    except Exception:
        pass
    return None


def _write_butterfly_codex_tokens(data: dict) -> None:
    _BUTTERFLY_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BUTTERFLY_AUTH_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(_BUTTERFLY_AUTH_PATH, 0o600)
    except OSError:
        pass


def _read_codex_cli_tokens() -> dict | None:
    """Read tokens from ~/.codex/auth.json (Codex CLI). Returns None if unavailable."""
    if not _CODEX_CLI_AUTH_PATH.exists():
        return None
    try:
        data = json.loads(_CODEX_CLI_AUTH_PATH.read_text(encoding="utf-8"))
        tokens = data.get("tokens", {})
        if isinstance(tokens, dict) and tokens.get("access_token") and tokens.get("refresh_token"):
            if not _is_token_expired(tokens["access_token"]):
                return dict(tokens)
    except Exception:
        pass
    return None


def _is_token_expired(token: str, buffer_seconds: int = 300) -> bool:
    import base64
    if not token:
        return True
    try:
        parts = token.split(".")
        pad = 4 - len(parts[1]) % 4
        padded = parts[1] + ("=" * pad if pad != 4 else "")
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return time.time() + buffer_seconds >= payload.get("exp", 0)
    except Exception:
        return True


def _print_codex_success(access_token: str) -> int:
    account_id = ""
    try:
        from butterfly.llm_engine.providers.codex import _extract_account_id
        account_id = _extract_account_id(access_token, "")
    except Exception:
        pass

    print()
    print("Codex login verified.")
    print(f"  auth file:  {_BUTTERFLY_AUTH_PATH}")
    if account_id:
        print(f"  account id: {account_id}")
    print()
    print("You can now run:")
    print("    butterfly chat 'hello'")
    return 0


# ── `butterfly kimi login` ───────────────────────────────────────────────────


def _add_kimi_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "kimi",
        allow_abbrev=False,
        help="Kimi For Coding (Moonshot) provider helpers.",
        description=(
            "Kimi provider helpers.\n\n"
            "Subcommands:\n"
            "  butterfly kimi login       Set up Kimi For Coding API key\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ksub = p.add_subparsers(dest="kimi_cmd", metavar="COMMAND")
    ksub.required = True

    login = ksub.add_parser(
        "login",
        allow_abbrev=False,
        help="Set up and verify a Kimi For Coding API key.",
        description=(
            "Kimi For Coding auth — auto-selects between two paths:\n\n"
            "  1. If KIMI_FOR_CODING_API_KEY is already in env or .env,\n"
            "     fast-path: ping-verify and exit.\n"
            "  2. Otherwise, run Moonshot's OAuth device-authorization\n"
            "     flow (KLIP-14) and persist the access_token to .env.\n\n"
            "Use --oauth to force OAuth, --api-key to force the legacy\n"
            "getpass paste path.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--key",
        metavar="KEY",
        help=(
            "API key to use (non-interactive; writes to .env then ping-"
            "verifies)."
        ),
    )
    login.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the API ping that confirms the key is valid.",
    )
    login.add_argument(
        "--env-file",
        metavar="PATH",
        default=".env",
        help="Path to the .env file to write (default: .env).",
    )
    mode = login.add_mutually_exclusive_group()
    mode.add_argument(
        "--oauth",
        action="store_true",
        help=(
            "Force the OAuth device-authorization path, even if a key is "
            "already set."
        ),
    )
    mode.add_argument(
        "--api-key",
        action="store_true",
        help=(
            "Force the legacy getpass-paste path (for scripted provisioning)."
        ),
    )

    p.set_defaults(func=cmd_kimi)


def cmd_kimi(args) -> int:
    if args.kimi_cmd == "login":
        return _kimi_login(
            key=getattr(args, "key", None),
            verify=not args.no_verify,
            env_file=getattr(args, "env_file", ".env"),
            force_oauth=getattr(args, "oauth", False),
            force_api_key=getattr(args, "api_key", False),
        )
    return 2


def _kimi_login(
    *,
    key: str | None,
    verify: bool,
    env_file: str,
    force_oauth: bool = False,
    force_api_key: bool = False,
) -> int:
    """Set up Kimi For Coding auth and write the resulting key to ``.env``.

    Auto-selects between three paths based on flags:

      * ``--key=KEY``  → non-interactive legacy: write KEY, verify, exit.
      * ``--oauth``    → always run the OAuth device flow.
      * ``--api-key``  → always run the legacy getpass prompt.
      * Default        → env/.env key present ⇒ fast-path; otherwise OAuth.
    """
    env_path = Path(env_file)

    # Every exit path must print the agent-hint. Wrap in try/finally so the
    # hint fires even on the unlikely exception.
    rc = 1
    try:
        rc = _kimi_login_impl(
            key=key,
            verify=verify,
            env_path=env_path,
            force_oauth=force_oauth,
            force_api_key=force_api_key,
        )
        return rc
    finally:
        _print_kimi_agent_hint()


def _kimi_login_impl(
    *,
    key: str | None,
    verify: bool,
    env_path: Path,
    force_oauth: bool,
    force_api_key: bool,
) -> int:
    # 1. Explicit --key wins: legacy scripted path.
    if key:
        return _kimi_persist_and_verify(key, env_path=env_path, verify=verify)

    # 2. Resolve any pre-existing key from env → .env file.
    existing = os.environ.get(_KIMI_ENV_KEY, "") or _read_env_var(env_path, _KIMI_ENV_KEY)

    # 3. --oauth forces OAuth even when a key is already set.
    if force_oauth:
        return _kimi_oauth_login(env_path=env_path, verify=verify)

    # 4. Fast-path: key already set and we're not being forced into a path.
    if existing and not force_api_key:
        if verify:
            print(f"Found {_KIMI_ENV_KEY} in environment/.env — verifying...")
            ok, err = _kimi_ping(existing)
            if not ok:
                print(f"Existing key failed verification: {err}", file=sys.stderr)
                print(
                    "Hint: re-run with --oauth to refresh, or --api-key to paste "
                    "a new key.",
                    file=sys.stderr,
                )
                return 1
            print(f"Verified. {_KIMI_ENV_KEY} already set — verified.")
        else:
            print(f"Found {_KIMI_ENV_KEY} in environment/.env — skipping verification.")
        return 0

    # 5. --api-key forces the legacy getpass path even with no existing key.
    if force_api_key:
        return _kimi_getpass_login(env_path=env_path, verify=verify)

    # 6. Default when no key is set: OAuth device flow.
    return _kimi_oauth_login(env_path=env_path, verify=verify)


def _kimi_persist_and_verify(
    api_key: str, *, env_path: Path, verify: bool, refresh_token: str | None = None
) -> int:
    """Write the key to ``env_path`` and (optionally) ping-verify it."""
    if verify:
        print("Verifying key... ", end="", flush=True)
        ok, err = _kimi_ping(api_key)
        if not ok:
            print("FAILED")
            print(f"Error: {err}", file=sys.stderr)
            return 1
        print("OK")

    _upsert_env_var(env_path, _KIMI_ENV_KEY, api_key)
    if refresh_token:
        _upsert_env_var(env_path, _KIMI_REFRESH_ENV_KEY, refresh_token)
    print()
    print(f"  Key written to {env_path} as {_KIMI_ENV_KEY}.")
    if refresh_token:
        print(f"  Refresh token stored as {_KIMI_REFRESH_ENV_KEY}.")
    print()
    print("Sessions using provider='kimi-coding-plan' will pick it up.")
    return 0


def _kimi_getpass_login(*, env_path: Path, verify: bool) -> int:
    """Legacy getpass-paste flow (pre-v2.0.31)."""
    print("Kimi For Coding — API key setup")
    print()
    print(f"  Get your key at: {_KIMI_DASHBOARD_URL}")
    print()
    try:
        import getpass
        resolved_key = getpass.getpass(f"  Paste your {_KIMI_ENV_KEY}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1

    if not resolved_key:
        print("Error: no API key provided.", file=sys.stderr)
        return 1

    return _kimi_persist_and_verify(resolved_key, env_path=env_path, verify=verify)


def _kimi_oauth_login(*, env_path: Path, verify: bool) -> int:
    """Run Moonshot's KLIP-14 OAuth device-authorization flow and persist."""
    try:
        import httpx
    except ImportError:
        print(
            "Error: httpx is required for OAuth login but is not installed.",
            file=sys.stderr,
        )
        return 1

    print()
    print("Kimi For Coding — OAuth device-authorization flow")
    print()

    try:
        tokens = _run_kimi_device_code_flow(httpx)
    except KeyboardInterrupt:
        print("\nOAuth cancelled.")
        return 130
    except _KimiOAuthExpired:
        print("OAuth flow expired — please retry", file=sys.stderr)
        return 1
    except _KimiOAuthDenied as exc:
        print(f"OAuth rejected by server: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"OAuth error: {exc}", file=sys.stderr)
        return 1

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token") or None
    if not access_token:
        print("Error: OAuth response did not contain an access_token.", file=sys.stderr)
        return 1

    print()
    print("OAuth login successful — persisting access_token as KIMI_FOR_CODING_API_KEY.")
    return _kimi_persist_and_verify(
        access_token, env_path=env_path, verify=verify, refresh_token=refresh_token
    )


# ── Moonshot KLIP-14 device-authorization flow ──────────────────────────────


class _KimiOAuthDenied(RuntimeError):
    """User denied / expired before polling completed (terminal)."""


class _KimiOAuthExpired(RuntimeError):
    """The flow's ``expires_in`` elapsed without user approval."""


def _stable_device_id() -> str:
    """Return a stable non-PII 16-hex device id.

    Derived from ``hostname + platform.platform()``. Stable across invocations
    on the same machine but reveals nothing sensitive to Moonshot.
    """
    seed = (socket.gethostname() + platform.platform()).encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _kimi_oauth_headers() -> dict[str, str]:
    """Required Moonshot headers for every OAuth request."""
    # Import lazily so tests that don't exercise OAuth never import httpx.
    try:
        from butterfly import __version__ as butterfly_version  # type: ignore
    except Exception:
        butterfly_version = "2.0.31"
    # Fall back to reading pyproject if __version__ is missing.
    return {
        "Content-Type": "application/json",
        "X-Msh-Platform": "butterfly",
        "X-Msh-Version": butterfly_version,
        "X-Msh-Device-Name": socket.gethostname() or "unknown",
        "X-Msh-Device-Model": "cli",
        "X-Msh-Device-Id": _stable_device_id(),
        "X-Msh-Os-Version": platform.platform(),
    }


def _run_kimi_device_code_flow(httpx_module) -> dict:
    """Perform Moonshot's device-code OAuth and return the token response.

    Raises:
      _KimiOAuthDenied — access_denied / expired_token during polling.
      _KimiOAuthExpired — device_code expires_in elapsed without success.
      RuntimeError — any other network / protocol error.
    """
    headers = _kimi_oauth_headers()

    # Step 1: request a device code.
    try:
        with httpx_module.Client(timeout=httpx_module.Timeout(15.0)) as client:
            resp = client.post(
                _KIMI_OAUTH_DEVICE_URL,
                json={"client_id": _KIMI_OAUTH_CLIENT_ID},
                headers=headers,
            )
    except Exception as exc:
        raise RuntimeError(f"Failed to request device code: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"Device-authorization request returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    data = resp.json() or {}
    device_code = data.get("device_code", "")
    user_code = data.get("user_code", "")
    verification_uri = data.get("verification_uri", "")
    verification_uri_complete = data.get("verification_uri_complete") or verification_uri
    expires_in = int(data.get("expires_in") or 600)
    interval = max(1, int(data.get("interval") or 5))

    if not device_code or not user_code:
        raise RuntimeError("Device-authorization response is missing required fields.")

    # Step 2: show the user what to do.
    print("To sign in, open this URL in your browser:")
    print(f"  \033[94m{verification_uri_complete}\033[0m")
    if verification_uri and verification_uri != verification_uri_complete:
        print(f"  — or go to {verification_uri} and enter code \033[94m{user_code}\033[0m")
    print()
    print("Waiting for sign-in... (Ctrl+C to cancel)")

    # Step 3: poll for the token.
    start = time.monotonic()
    current_interval = interval

    with httpx_module.Client(timeout=httpx_module.Timeout(15.0)) as client:
        while True:
            if time.monotonic() - start >= expires_in:
                raise _KimiOAuthExpired()
            _sleep(current_interval)

            try:
                poll = client.post(
                    _KIMI_OAUTH_TOKEN_URL,
                    json={
                        "grant_type": _KIMI_OAUTH_GRANT_TYPE,
                        "device_code": device_code,
                        "client_id": _KIMI_OAUTH_CLIENT_ID,
                    },
                    headers=headers,
                )
            except Exception as exc:
                raise RuntimeError(f"Token-poll network error: {exc}") from exc

            if poll.status_code == 200:
                result = poll.json() or {}
                if not result.get("access_token"):
                    raise RuntimeError(
                        "Token response is missing access_token: "
                        f"{str(result)[:200]}"
                    )
                return result

            # RFC 8628 puts the error code in the JSON body on non-200.
            err_code = ""
            try:
                err_code = (poll.json() or {}).get("error", "") or ""
            except Exception:
                err_code = ""

            if err_code == "authorization_pending":
                continue
            if err_code == "slow_down":
                current_interval = min(current_interval * 2, _KIMI_OAUTH_POLL_INTERVAL_CAP)
                continue
            if err_code in ("access_denied", "expired_token"):
                raise _KimiOAuthDenied(err_code)

            # Unknown non-200 without a recognisable error code.
            raise RuntimeError(
                f"Token poll returned HTTP {poll.status_code} (error={err_code!r}): "
                f"{poll.text[:200]}"
            )


def _sleep(seconds: float) -> None:
    """Indirection point so tests can monkeypatch the polling sleep."""
    time.sleep(seconds)


def _read_env_var(env_path: Path, key: str) -> str:
    """Return the value of ``KEY`` in ``env_path``, or ``""`` if absent."""
    if not env_path.exists():
        return ""
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(f"export {key}="):
                return line[len(f"export {key}="):].strip()
            if line.startswith(f"{key}="):
                return line[len(f"{key}="):].strip()
    except OSError:
        pass
    return ""


def _kimi_ping(api_key: str) -> tuple[bool, str]:
    """Send a minimal chat completion to verify the key. Returns (ok, error_msg)."""
    try:
        import httpx
    except ImportError:
        print(
            "Warning: httpx not installed — skipping key verification.",
            file=sys.stderr,
        )
        return True, ""

    try:
        from butterfly.llm_engine.providers.kimi import _KIMI_USER_AGENT, _KIMI_OPENAI_BASE_URL
        ua = _KIMI_USER_AGENT
        base = _KIMI_OPENAI_BASE_URL
    except ImportError:
        ua = "claude-code/0.1.0"
        base = "https://api.kimi.com/coding/v1/"

    body = {
        "model": _KIMI_VERIFY_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
            resp = client.post(
                f"{base}chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": ua,
                },
            )
    except Exception as exc:
        return False, f"network error: {exc}"

    if resp.status_code in (200, 400):
        # 400 means the request was understood (key accepted); model errors are OK for a ping.
        return True, ""
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Write or replace KEY=value in a .env file.

    Preserves the ``export`` prefix when replacing an existing
    ``export KEY=old`` line so the resulting file stays valid for ``source``-
    style loading.
    """
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"export {key}="):
                lines.append(f"export {key}={value}")
                found = True
            elif line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
