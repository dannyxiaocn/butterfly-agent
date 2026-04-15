"""One-command login helpers for Codex and Kimi providers.

Exposes two subcommands:

* ``butterfly codex login`` — drives the ``codex`` CLI's OAuth flow (or prints
  install instructions if the CLI is missing), then verifies the resulting
  ``~/.codex/auth.json`` is parseable and contains tokens.
* ``butterfly kimi login`` — prompts for the Kimi For Coding API key (hiding
  input via ``getpass``), writes it to the repo ``.env`` with ``0600`` perms,
  and performs a lightweight validation ping.

Both commands are interactive-first. Non-interactive callers can pipe the key
on stdin (for kimi) or skip verification via ``--no-verify``.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CODEX_INSTALL_HINT = "npm install -g @openai/codex"
_KIMI_DASHBOARD_URL = "https://platform.moonshot.ai/console/api-keys"
_KIMI_ENV_KEY = "KIMI_FOR_CODING_API_KEY"


# ── `butterfly codex login` ──────────────────────────────────────────────────


def _add_codex_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "codex",
        allow_abbrev=False,
        help="Codex (ChatGPT-OAuth) provider helpers.",
        description=(
            "Codex provider helpers.\n\n"
            "Subcommands:\n"
            "  butterfly codex login      Run codex OAuth login and verify auth.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    csub = p.add_subparsers(dest="codex_cmd", metavar="COMMAND")
    csub.required = True

    login = csub.add_parser(
        "login",
        allow_abbrev=False,
        help="Run the Codex CLI OAuth login and verify credentials.",
        description=(
            "One-command Codex OAuth login.\n\n"
            "Step 1: ensure the `codex` CLI is installed.\n"
            "Step 2: shell out to `codex login` to run the OAuth flow.\n"
            "Step 3: read ~/.codex/auth.json and confirm the access_token parses.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip auth.json verification (just run the CLI).",
    )
    login.add_argument(
        "--skip-cli",
        action="store_true",
        help="Don't run `codex login`; only verify an existing auth.json.",
    )

    p.set_defaults(func=cmd_codex)


def cmd_codex(args) -> int:
    if args.codex_cmd == "login":
        return _codex_login(skip_cli=args.skip_cli, verify=not args.no_verify)
    return 2


def _codex_login(*, skip_cli: bool, verify: bool) -> int:
    if not skip_cli:
        codex_path = shutil.which("codex")
        if not codex_path:
            print("codex CLI not found on PATH.")
            print()
            print("Step 1 — install the codex CLI:")
            print(f"    {_CODEX_INSTALL_HINT}")
            print("  (or see https://github.com/openai/codex for other options)")
            print()
            print("Step 2 — log in:")
            print("    codex login")
            print()
            print("Step 3 — verify with butterfly:")
            print("    butterfly codex login --skip-cli")
            return 1

        print(f"Running `{codex_path} login` ...")
        print("  (the CLI will open a browser for ChatGPT OAuth)")
        try:
            rc = subprocess.call([codex_path, "login"])
        except OSError as exc:
            print(f"Error: failed to exec codex CLI: {exc}", file=sys.stderr)
            return 1
        if rc != 0:
            print(f"codex login exited with status {rc}.", file=sys.stderr)
            return rc

    if not verify:
        print("Skipping verification (--no-verify).")
        return 0

    return _verify_codex_auth()


def _verify_codex_auth() -> int:
    path = _CODEX_AUTH_PATH
    if not path.exists():
        print(f"Error: {path} not found after login.", file=sys.stderr)
        print("  Did `codex login` complete successfully?")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: could not parse {path}: {exc}", file=sys.stderr)
        return 1

    tokens = data.get("tokens") or {}
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access or not refresh:
        print(
            f"Error: {path} is missing access_token or refresh_token.",
            file=sys.stderr,
        )
        return 1

    # Attempt to surface the account id — same extraction Butterfly will use.
    account_id = ""
    try:
        from butterfly.llm_engine.providers.codex import _extract_account_id
        account_id = _extract_account_id(access, tokens.get("id_token", ""))
    except Exception as exc:  # pragma: no cover — best-effort decorative info
        print(f"Warning: could not extract account id: {exc}")

    print("Codex login verified.")
    print(f"  auth file:  {path}")
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
            "  butterfly kimi login       Prompt for API key, write .env, verify\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ksub = p.add_subparsers(dest="kimi_cmd", metavar="COMMAND")
    ksub.required = True

    login = ksub.add_parser(
        "login",
        allow_abbrev=False,
        help="Prompt for a Kimi API key, save it to .env, and verify.",
        description=(
            "One-command Kimi API key setup.\n\n"
            "Step 1: opens/points at the Moonshot dashboard so you can copy a key.\n"
            "Step 2: prompts for the key (input is hidden).\n"
            "Step 3: writes it to the repo .env (permissions 0600).\n"
            "Step 4: makes a small Anthropic-compatible request to validate.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env file (default: <repo>/.env)",
    )
    login.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the validation ping.",
    )
    login.add_argument(
        "--key",
        default=None,
        help="Pass the API key directly (non-interactive). "
             "If omitted, you'll be prompted.",
    )

    p.set_defaults(func=cmd_kimi)


def cmd_kimi(args) -> int:
    if args.kimi_cmd == "login":
        return _kimi_login(
            env_file=args.env_file,
            key_arg=args.key,
            verify=not args.no_verify,
        )
    return 2


def _kimi_login(
    *,
    env_file: Path | None,
    key_arg: str | None,
    verify: bool,
) -> int:
    if env_file is None:
        # Default: repo-root .env (same as runtime.env._REPO_ROOT heuristic).
        env_file = Path(__file__).resolve().parent.parent.parent / ".env"

    existing = os.environ.get(_KIMI_ENV_KEY) or os.environ.get("KIMI_API_KEY")

    print("Kimi For Coding — API key setup")
    print(f"  Dashboard: {_KIMI_DASHBOARD_URL}")
    print(f"  Env file:  {env_file}")
    print()

    if key_arg:
        key = key_arg.strip()
    else:
        if existing:
            reuse = _prompt(
                f"Found existing {_KIMI_ENV_KEY} in the environment. "
                "Reuse it? [Y/n]: "
            ).strip().lower()
            if reuse in ("", "y", "yes"):
                key = existing
            else:
                key = _prompt_secret("Paste your Kimi API key: ")
        else:
            print("Step 1 — create/copy a key at:")
            print(f"    {_KIMI_DASHBOARD_URL}")
            print("Step 2 — paste it below (input hidden).")
            key = _prompt_secret("Kimi API key: ")

    key = (key or "").strip()
    if not key:
        print("Error: empty key; aborting.", file=sys.stderr)
        return 1

    try:
        _write_env_key(env_file, _KIMI_ENV_KEY, key)
    except OSError as exc:
        print(f"Error: failed to write {env_file}: {exc}", file=sys.stderr)
        return 1

    os.environ[_KIMI_ENV_KEY] = key  # so a same-process verify picks it up
    print(f"Wrote {_KIMI_ENV_KEY} to {env_file} (chmod 0600).")

    if not verify:
        print("Skipping verification (--no-verify).")
        return 0

    ok, msg = _verify_kimi_key(key)
    if ok:
        print(f"Kimi key verified. {msg}")
        print()
        print("You can now run:")
        print("    butterfly chat --entity agent 'hello'")
        print("  (make sure your entity's provider is set to 'kimi-coding-plan')")
        return 0

    print(f"Warning: key saved, but validation failed: {msg}", file=sys.stderr)
    print("The key is written to .env anyway — you can retry later with:")
    print("    butterfly kimi login --no-verify")
    return 1


def _prompt(msg: str) -> str:
    # Isolated so tests can monkeypatch.
    try:
        return input(msg)
    except EOFError:
        return ""


def _prompt_secret(msg: str) -> str:
    try:
        return getpass.getpass(msg)
    except (EOFError, KeyboardInterrupt):
        return ""


def _write_env_key(env_file: Path, key: str, value: str) -> None:
    """Upsert ``key=value`` in *env_file*, creating the file if absent.

    Preserves other lines verbatim. Sets file mode to ``0600``.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)

    quoted = _quote_env_value(value)
    new_line = f"{key}={quoted}"

    lines: list[str] = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass


def _quote_env_value(value: str) -> str:
    """Quote an env value if it contains whitespace or shell-sensitive chars."""
    if any(c in value for c in " \t\"'$#\\"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _verify_kimi_key(api_key: str) -> tuple[bool, str]:
    """Best-effort validation ping against Kimi's Anthropic-compatible endpoint.

    Returns ``(True, info_msg)`` or ``(False, error_msg)``. Only network/auth
    errors are reported as failure; any other exception is treated as a soft
    warning so a working key isn't wrongly rejected because of (e.g.) a
    proxy-level hiccup.
    """
    try:
        from butterfly.llm_engine.providers.kimi import KimiForCodingProvider
        from butterfly.core.types import Message
    except ImportError as exc:
        return False, f"import error: {exc}"

    try:
        provider = KimiForCodingProvider(api_key=api_key, max_tokens=16)
    except Exception as exc:
        return False, f"provider init failed: {exc}"

    async def _ping() -> tuple[bool, str]:
        try:
            text, _tc, usage = await provider.complete(
                messages=[Message(role="user", content="ping")],
                tools=[],
                system_prompt="Reply with one word.",
                model="kimi-k2-turbo-preview",
            )
            return True, f"({usage.input_tokens}→{usage.output_tokens} tokens)"
        except Exception as exc:  # noqa: BLE001 — surface any provider error
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await provider.aclose()
            except Exception:
                pass

    try:
        return asyncio.run(_ping())
    except RuntimeError as exc:
        # asyncio.run raises if called inside a running loop (won't happen in
        # the CLI) — treat as soft failure.
        return False, f"runtime error: {exc}"
