"""nutshell-review-updates — interactive CLI to review entity update requests.

Usage:
    nutshell-review-updates          # list pending updates and review each one
    nutshell-review-updates --list   # list only, don't prompt

Exit codes:
    0  — completed (some updates may have been applied or rejected)
    1  — error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _show_record(record, idx: int, total: int) -> None:
    bar = "─" * 60
    print(f"\n{bar}")
    print(f"  Update {idx}/{total}  [id: {record.id}]")
    print(bar)
    print(f"  Session : {record.session_id}")
    print(f"  Time    : {record.ts}")
    print(f"  File    : {record.file_path}")
    print(f"\n  Reason  : {record.reason}")
    print(f"\n  Content preview ({min(len(record.content), 500)} / {len(record.content)} chars):")
    print("  " + "\n  ".join(record.content[:500].splitlines()))
    if len(record.content) > 500:
        print("  ... (truncated)")
    print(bar)


def main() -> None:
    from nutshell.runtime.env import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="nutshell-review-updates",
        description="Review entity update requests submitted by agents.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List pending updates without prompting to approve/reject",
    )
    parser.add_argument(
        "--updates-dir", type=Path, default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    from nutshell.runtime.entity_updates import (
        list_pending_updates,
        apply_update,
        reject_update,
    )

    updates_base = args.updates_dir
    repo_root = args.repo_root

    pending = list_pending_updates(updates_base)

    if not pending:
        print("No pending entity update requests.")
        return

    print(f"\n{len(pending)} pending update request(s).")

    if args.list:
        for i, record in enumerate(pending, 1):
            print(f"\n  {i}. [{record.ts}] {record.file_path}  (session: {record.session_id})")
            print(f"     Reason: {record.reason[:80]}")
        return

    applied = 0
    rejected = 0

    for i, record in enumerate(pending, 1):
        _show_record(record, i, len(pending))
        while True:
            try:
                choice = input("\n  [a]pply / [r]eject / [s]kip / [q]uit: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                break
            if choice in ("a", "apply"):
                try:
                    apply_update(record.id, updates_base=updates_base, entity_base=repo_root)
                    print(f"  ✓ Applied — {record.file_path} updated.")
                    applied += 1
                except Exception as exc:
                    print(f"  ✗ Error applying: {exc}", file=sys.stderr)
                break
            elif choice in ("r", "reject"):
                try:
                    reject_update(record.id, updates_base=updates_base)
                    print(f"  ✗ Rejected.")
                    rejected += 1
                except Exception as exc:
                    print(f"  ✗ Error rejecting: {exc}", file=sys.stderr)
                break
            elif choice in ("s", "skip"):
                print("  Skipped.")
                break
            elif choice in ("q", "quit"):
                print(f"\nDone. Applied: {applied}, Rejected: {rejected}, Remaining: {len(pending) - i}")
                return
            else:
                print("  Please enter 'a', 'r', 's', or 'q'.")

    print(f"\nDone. Applied: {applied}, Rejected: {rejected}.")


if __name__ == "__main__":
    main()
