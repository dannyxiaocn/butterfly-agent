"""Phase 6 — CLI dispatch layer over ``butterfly.runtime.io``.

Two layers registered here:

1. ``butterfly io <function> [args...]`` — reflection into ``runtime.io``.
   Every reader/writer in ``butterfly/runtime/io.py`` is reachable by name
   so CI, shell scripts, and regression tests can drive the whole system
   without a browser (invariant I6 from DESIGN.md).

2. Friendly aliases — ``chat``, ``stop``, ``start``, ``interrupt``,
   ``delete``, ``tasks``, ``task-upsert``, ``task-delete``, ``shell``,
   ``config-set``, ``prompt-edit``. These are thin argparse wrappers that
   call exactly one ``runtime.io.*`` function. No business logic lives
   here.

The module is deliberately small: DESIGN.md §6 pins the CLI as a
"dispatch layer", not a home for rendering logic. Pretty-tables for
``sessions`` / ``tasks`` / ``log`` live alongside the alias handler that
needs them — a reader of ``io_command.py`` can trace every command top
to bottom without cross-file hops.
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from butterfly.runtime import io as _io


# ── Generic reflection ───────────────────────────────────────────────────────


def _io_function_names() -> frozenset[str]:
    """Snapshot of every public io.* function name defined in io.py.

    Computed ONCE at import time from the raw module (filtered by
    ``inspect.isfunction`` + ``__module__`` match). Lookup at
    ``cmd_io`` time resolves the CURRENT attribute on the module, so
    pytest monkeypatches are honoured even though the patched function
    would fail the ``__module__`` filter.
    """
    out: set[str] = set()
    for name in dir(_io):
        if name.startswith("_"):
            continue
        obj = getattr(_io, name)
        if not callable(obj) or not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", "") != _io.__name__:
            continue
        out.add(name)
    return frozenset(out)


_IO_FUNCTION_NAMES: frozenset[str] = _io_function_names()


def _public_io_functions() -> dict[str, Callable[..., Any]]:
    """Live {name: current callable} map. Honours monkeypatches."""
    out: dict[str, Callable[..., Any]] = {}
    for name in _IO_FUNCTION_NAMES:
        obj = getattr(_io, name, None)
        if callable(obj):
            out[name] = obj
    return out


def _parse_cli_value(raw: str) -> Any:
    """Parse a CLI arg as JSON first, fall back to string.

    ``--since_id=10`` → int 10. ``--text=hello`` → str "hello" (because
    ``hello`` isn't valid JSON). ``--enabled=true`` → bool True. Empty
    string stays empty string (not the JSON nothing-value).
    """
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _coerce_result(obj: Any) -> Any:
    """Normalise a function return value into a JSON-ready structure.

    Dispatch order (most → least specific):
      1. ``Event`` / anything with a ``to_dict`` method → ``.to_dict()``
      2. dataclass instance → ``dataclasses.asdict``
      3. iterator / generator → materialise + recurse
      4. list / tuple → map recurse over elements
      5. dict → map recurse over values
      6. primitives (str, int, float, bool, None) → unchanged
      7. anything else → ``str(obj)`` (safety net)
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    # Prefer an explicit to_dict (Event, PanelEntry, etc.) over
    # dataclasses.asdict — some dataclasses have custom serialisers.
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return _coerce_result(obj.to_dict())
        except TypeError:
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _coerce_result(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: _coerce_result(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce_result(v) for v in obj]
    # Iterator / generator path. Must come AFTER str/list/dict — those
    # are technically iterable but have structural meaning.
    if hasattr(obj, "__iter__") and hasattr(obj, "__next__"):
        return [_coerce_result(v) for v in obj]
    # Inspect generators directly (``inspect.isgenerator`` catches the
    # generator case even before __iter__/__next__ resolve cleanly).
    if inspect.isgenerator(obj):
        return [_coerce_result(v) for v in obj]
    return str(obj)


def _format_signature(fn: Callable[..., Any]) -> str:
    try:
        return f"{fn.__name__}{inspect.signature(fn)}"
    except (TypeError, ValueError):
        return fn.__name__


def _print_io_list() -> int:
    """Print every ``io.*`` function with its signature. Discovery aid."""
    fns = _public_io_functions()
    width = max((len(n) for n in fns), default=0)
    for name in sorted(fns):
        sig = inspect.signature(fns[name])
        print(f"  {name:<{width}}  {sig}")
    return 0


def cmd_io(args: argparse.Namespace) -> int:
    """Dispatch ``butterfly io <function> ...``.

    Layered error handling:
      - unknown function / bad arity → exit 2
      - ``FileNotFoundError`` / ``ValueError`` / ``IOError`` → exit 2
      - everything else propagates (developer bug — traceback is the
        right UX for that).
    """
    fn_name = getattr(args, "io_function", None)
    if not fn_name or fn_name == "list":
        return _print_io_list()

    fns = _public_io_functions()
    if fn_name not in fns:
        print(
            f"Error: unknown io function {fn_name!r}. "
            f"Run `butterfly io list` for the full catalog.",
            file=sys.stderr,
        )
        return 2
    fn = fns[fn_name]

    # Split raw extras into positional + --kw=value.
    positionals: list[Any] = []
    kwargs: dict[str, Any] = {}
    for raw in args.io_extras:
        if raw.startswith("--"):
            body = raw[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                kwargs[k.replace("-", "_")] = _parse_cli_value(v)
            else:
                # bare ``--flag`` → boolean True
                kwargs[body.replace("-", "_")] = True
        else:
            positionals.append(_parse_cli_value(raw))

    try:
        result = fn(*positionals, **kwargs)
    except TypeError as exc:
        # Surface wrong-arity / unknown-kwarg cleanly (argparse can't
        # validate this — we don't know the function's sig upfront).
        print(
            f"Error: bad arguments for {fn_name}: {exc}\n"
            f"  signature: {_format_signature(fn)}",
            file=sys.stderr,
        )
        return 2
    except (FileNotFoundError, ValueError, IOError, NotImplementedError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    coerced = _coerce_result(result)
    print(json.dumps(coerced, indent=2, ensure_ascii=False, default=str))
    return 0


# ── Friendly aliases ─────────────────────────────────────────────────────────


def _resolve_session_positional(args: argparse.Namespace) -> str:
    """Most alias handlers take a ``session_id`` positional. Centralise
    the "defaulted to latest when absent" UX so each handler doesn't
    reimplement it. Today only ``tasks`` / ``log`` use the fallback; the
    write aliases REQUIRE an explicit id.
    """
    sid = getattr(args, "session_id", None)
    if sid:
        return sid
    infos = _io.list_sessions()
    if not infos:
        raise FileNotFoundError("no sessions found")
    return infos[0]["id"]


# Existing chat / new / stop / start parsers live in ui.cli.main and
# keep their legacy UX (--inject-memory, --no-wait, pretty tables).
# The spec's ``butterfly chat <sid> <text>`` / etc. forms are still
# reachable via the reflection layer:
#
#   butterfly io send_message <sid> --text="hello"
#   butterfly io create_session <sid> --agent=NAME
#   butterfly io stop_session <sid>
#
# That keeps one code path per write without breaking argparse shapes
# that the main.py tests pin.


# ---- interrupt --------------------------------------------------------------

def cmd_alias_interrupt(args: argparse.Namespace) -> int:
    try:
        _io.interrupt_session(args.session_id, text=args.text)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    label = f"interrupted {args.session_id}"
    if args.text:
        label += f" (+text)"
    print(label)
    return 0


# ---- delete -----------------------------------------------------------------

def cmd_alias_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            f"Error: delete is destructive; re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2
    try:
        _io.delete_session(args.session_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"deleted {args.session_id}")
    return 0


# ---- sessions ---------------------------------------------------------------

def cmd_alias_sessions(args: argparse.Namespace) -> int:
    infos = _io.list_sessions(include_archived=args.archived)
    if getattr(args, "as_json", False):
        print(json.dumps(infos, indent=2, ensure_ascii=False, default=str))
        return 0
    if not infos:
        print("No sessions found.")
        return 0
    COL = {"id": 26, "agent": 16, "status": 10}
    print(f"{'ID':<{COL['id']}}  {'AGENT':<{COL['agent']}}  {'STATUS':<{COL['status']}}  LAST RUN")
    print("-" * (sum(COL.values()) + 8))
    for s in infos:
        status = s.get("status") or "?"
        last = s.get("last_run_at") or s.get("created_at") or "-"
        print(
            f"{s.get('id',''):<{COL['id']}}  {s.get('agent','?'):<{COL['agent']}}  "
            f"{status:<{COL['status']}}  {last}"
        )
    return 0


# ---- log --------------------------------------------------------------------

def _render_event_line(ev: dict) -> str:
    """One-line human rendering of an event. Trims long payloads."""
    etype = ev.get("type", "?")
    ts = ev.get("ts", 0)
    payload = ev.get("payload") or {}
    snippet = ""
    for key in ("text", "name", "tool_name", "reason", "kind"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            snippet = val.replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:79] + "…"
            break
    return f"[{ev.get('id','?'):>5}] {etype:<24} {snippet}".rstrip()


def cmd_alias_log(args: argparse.Namespace) -> int:
    try:
        sid = _resolve_session_positional(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    since_id = args.since
    try:
        events = list(_io.read_events(sid, since_id=since_id))
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.num and not args.watch:
        events = events[-args.num:]

    for ev in events:
        print(_render_event_line(ev.to_dict()))

    if not args.watch:
        return 0

    # Watch mode: poll read_events starting at the last-seen id until
    # Ctrl+C. Polling cadence 1s — fast enough to feel live, slow enough
    # not to pummel the disk. Uses latest_event_id() as a cheap cursor.
    cursor = max(
        (ev.id for ev in events),
        default=since_id if since_id is not None else 0,
    )
    try:
        while True:
            time.sleep(1.0)
            try:
                new_events = list(_io.read_events(sid, since_id=cursor))
            except FileNotFoundError:
                return 0
            for ev in new_events:
                print(_render_event_line(ev.to_dict()))
                cursor = ev.id
    except KeyboardInterrupt:
        return 0


# ---- tasks ------------------------------------------------------------------

def cmd_alias_tasks(args: argparse.Namespace) -> int:
    try:
        sid = _resolve_session_positional(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        cards = _io.read_task_cards(sid)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "as_json", False):
        print(json.dumps(cards, indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"[{sid}] task cards ({len(cards)})")
    print("-" * 60)
    if not cards:
        print("(empty)")
        return 0
    for c in cards:
        interval = c.get("check_interval") or 0
        interval_str = f"every {interval:g}s" if interval else "on-demand"
        script_str = "[script]" if c.get("script") is not None else ""
        print(f"  [{c.get('status','?')}] {c.get('name','?')}  ({interval_str}) {script_str}")
        desc = (c.get("description") or "").strip()
        for line in desc.splitlines()[:3]:
            print(f"      {line}")
    return 0


# ---- task-upsert / task-delete ---------------------------------------------

def cmd_alias_task_upsert(args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {}
    if args.description is not None:
        kwargs["description"] = args.description
    if args.script is not None:
        kwargs["script"] = args.script
    if args.check_interval is not None:
        kwargs["check_interval"] = args.check_interval
    if args.notes is not None:
        kwargs["notes"] = args.notes
    if args.progress is not None:
        kwargs["progress"] = args.progress
    try:
        _io.upsert_task(args.session_id, args.name, **kwargs)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"upserted task {args.name} in {args.session_id}")
    return 0


def cmd_alias_task_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Error: task-delete is destructive; re-run with --yes to confirm.", file=sys.stderr)
        return 2
    try:
        _io.delete_task(args.session_id, args.name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"deleted task {args.name} from {args.session_id}")
    return 0


# ---- shell ------------------------------------------------------------------

def cmd_alias_shell(args: argparse.Namespace) -> int:
    try:
        _io.terminal_input(args.session_id, args.text, source="cli")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print("ok")
    return 0


# ---- config-set -------------------------------------------------------------

def cmd_alias_config_set(args: argparse.Namespace) -> int:
    value = _parse_cli_value(args.value)
    try:
        _io.update_config(args.session_id, args.key, value)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"config {args.key} updated")
    return 0


# ---- prompt-edit ------------------------------------------------------------

def _run_editor(initial: str) -> str:
    """Open $EDITOR on a tmp file seeded with ``initial``; return new body.

    If $EDITOR is unset, fall back to ``vi`` (POSIX default). The editor
    is invoked via ``subprocess.run`` with a shell=False list form so a
    user-supplied EDITOR like ``code --wait`` still works via the
    shlex split.
    """
    import shlex

    editor = os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(initial)
        path = fh.name
    try:
        cmd = shlex.split(editor) + [path]
        subprocess.run(cmd, check=False)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def cmd_alias_prompt_edit(args: argparse.Namespace) -> int:
    try:
        current = _io.read_prompt(args.session_id, args.name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    new_body = _run_editor(current)
    if new_body == current:
        print("no changes")
        return 0
    try:
        _io.update_prompt(args.session_id, args.name, new_body)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"prompt {args.name} updated")
    return 0


# ── argparse wiring ──────────────────────────────────────────────────────────


def _add_io_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "io",
        help="Call butterfly.runtime.io.<fn> by name. See `butterfly io list`.",
        description=(
            "Reflect into butterfly.runtime.io and call any public function.\n"
            "Positional args become positionals; --kw=val becomes keyword args.\n"
            "Values parse as JSON when possible (so --since_id=10 is int 10)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("io_function", nargs="?", default=None,
                   help="io.<name> to invoke; pass 'list' (or omit) to enumerate")
    p.add_argument("io_extras", nargs=argparse.REMAINDER,
                   help="Positional + --kw=val args forwarded to the function")
    p.set_defaults(func=cmd_io)


def _add_aliases(subparsers) -> None:
    # NOTE: existing ``chat`` / ``new`` / ``stop`` / ``start`` /
    # ``sessions`` / ``log`` / ``tasks`` parsers are owned by
    # ``ui.cli.main`` (they predate Phase 6 and carry UX like
    # --inject-memory / --no-wait / pretty-tables). Main.py retargets
    # them through runtime.io internally. This block registers only
    # the NEW aliases introduced in Phase 6.

    # interrupt <sid> [--text=T]
    p = subparsers.add_parser(
        "interrupt", help="Interrupt a running session (optionally with a follow-up message)."
    )
    p.add_argument("session_id")
    p.add_argument("--text", default=None, help="Optional follow-up message.")
    p.set_defaults(func=cmd_alias_interrupt)

    # delete <sid> --yes
    p = subparsers.add_parser("delete", help="Delete a session (requires --yes).")
    p.add_argument("session_id")
    p.add_argument("--yes", action="store_true", help="Confirm destructive delete.")
    p.set_defaults(func=cmd_alias_delete)

    # task-upsert <sid> <name> [...]
    p = subparsers.add_parser("task-upsert", help="Create or update a task card.")
    p.add_argument("session_id")
    p.add_argument("name")
    p.add_argument("--description", default=None)
    p.add_argument("--script", default=None)
    p.add_argument("--check-interval", dest="check_interval", type=float, default=None)
    p.add_argument("--notes", default=None)
    p.add_argument("--progress", default=None)
    p.set_defaults(func=cmd_alias_task_upsert)

    # task-delete <sid> <name> --yes
    p = subparsers.add_parser("task-delete", help="Delete a task card (requires --yes).")
    p.add_argument("session_id")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_alias_task_delete)

    # shell <sid> <text>
    p = subparsers.add_parser("shell", help="Send one line of terminal input.")
    p.add_argument("session_id")
    p.add_argument("text")
    p.set_defaults(func=cmd_alias_shell)

    # config-set <sid> <key> <value>
    p = subparsers.add_parser(
        "config-set", help="Update one key in core/config.yaml (value parsed as JSON when possible)."
    )
    p.add_argument("session_id")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_alias_config_set)

    # prompt-edit <sid> <name>
    p = subparsers.add_parser(
        "prompt-edit",
        help="Open $EDITOR on a prompt (system/task/env); writes back via io.update_prompt.",
    )
    p.add_argument("session_id")
    p.add_argument("name")
    p.set_defaults(func=cmd_alias_prompt_edit)


def register_io_commands(subparsers) -> None:
    """Wire ``butterfly io`` + aliases into the top-level argparse tree.

    Existing ``chat`` / ``new`` / ``stop`` / ``start`` / ``sessions`` /
    ``log`` / ``tasks`` parsers are registered by ``ui.cli.main`` itself
    (they predate Phase 6 and carry UX baggage — --inject-memory,
    --no-wait, tail mode). This function adds ONLY the new commands.
    """
    _add_io_parser(subparsers)
    _add_aliases(subparsers)
