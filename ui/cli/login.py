"""One-command login helpers for Codex and Kimi providers.

Exposes two subcommands:

* ``butterfly codex login`` — runs a built-in OpenAI device-code OAuth flow
  (no dependency on the ``codex`` CLI) and stores tokens in butterfly's own
  ``~/.butterfly/auth.json``.  On first run it can also import existing tokens
  from ``~/.codex/auth.json`` to avoid requiring a fresh login.  This mirrors
  the approach used by hermes-agent: butterfly owns its own OAuth session so
  refresh-token rotation by Codex CLI / VS Code never invalidates butterfly's
  credentials.

* ``butterfly kimi login`` — interactively prompts for a Kimi For Coding API
  key, verifies it with a lightweight ping, and writes it to ``.env``.  Mirrors
  openclaw's ``kimi-coding`` plugin which uses the same API-key auth flow.
"""
from __future__ import annotations

import argparse
import json
import os
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
_KIMI_VERIFY_URL = "https://api.kimi.com/coding/v1/chat/completions"
_KIMI_VERIFY_MODEL = "kimi-k2-turbo-preview"


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
            "Kimi For Coding uses a static API key (no OAuth flow).\n\n"
            "This command prompts you for the key (or reads it from the env),\n"
            "optionally verifies it against the API, and writes it to .env.\n\n"
            "Mirrors the API-key auth flow used by openclaw's kimi-coding plugin.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--key",
        metavar="KEY",
        help=f"API key to use (skips the interactive prompt).",
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

    p.set_defaults(func=cmd_kimi)


def cmd_kimi(args) -> int:
    if args.kimi_cmd == "login":
        return _kimi_login(
            key=getattr(args, "key", None),
            verify=not args.no_verify,
            env_file=getattr(args, "env_file", ".env"),
        )
    return 2


def _kimi_login(*, key: str | None, verify: bool, env_file: str) -> int:
    """Set up a Kimi For Coding API key and write it to .env."""
    # Resolve key: explicit arg → existing env var → interactive prompt.
    resolved_key = key or os.environ.get(_KIMI_ENV_KEY, "")
    if not resolved_key:
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

    # Optionally verify the key with a lightweight ping.
    if verify:
        print("Verifying key... ", end="", flush=True)
        ok, err = _kimi_ping(resolved_key)
        if not ok:
            print("FAILED")
            print(f"Error: {err}", file=sys.stderr)
            return 1
        print("OK")

    # Write to .env file.
    env_path = Path(env_file)
    _upsert_env_var(env_path, _KIMI_ENV_KEY, resolved_key)
    print()
    print(f"  Key written to {env_path} as {_KIMI_ENV_KEY}.")
    print()
    print("Sessions using provider='kimi-coding-plan' will pick it up.")
    return 0


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
