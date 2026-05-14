"""Tests for ``butterfly eval`` CLI surface.

Drives the argparse layer end-to-end via subprocess so we exercise the
real entrypoint (not just the cmd_* functions). Each invocation runs
in a tmpdir so artifacts don't pollute the repo's ``_evals/``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _butterfly_eval(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ui.cli.main", "eval", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=60,
    )


def test_eval_list_outputs_three_builtins():
    proc = _butterfly_eval("list")
    assert proc.returncode == 0, proc.stderr
    for needle in ("swe-bench-verified", "terminal-bench", "tau-bench"):
        assert needle in proc.stdout


def test_eval_list_json_marks_enabled_status():
    proc = _butterfly_eval("list", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    by_id = {row["id"]: row for row in data}
    assert by_id["tau-bench"]["enabled"] is True


def test_eval_run_with_enable_and_mock_passes(tmp_path):
    proc = _butterfly_eval(
        "run",
        "--enable", "tau-bench",
        "--adapter", "mock-passing",
        "--limit", "1",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode == 0, proc.stderr
    assert "passed=1/1" in proc.stdout
    # Persistence: artifacts must land under the --evals-root we passed.
    runs = list((tmp_path / "_evals").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "run.json").is_file()
    assert (runs[0] / "results.jsonl").is_file()


def test_eval_run_unknown_benchmark_exits_nonzero(tmp_path):
    proc = _butterfly_eval(
        "run",
        "--enable", "no-such-bench",
        "--adapter", "echo",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode != 0
    assert "no-such-bench" in proc.stderr


def test_eval_run_without_enable_uses_evals_md(tmp_path):
    # Seed a one-line evals.md so the default-path branch is hit.
    md = tmp_path / "evals.md"
    md.write_text("tau-bench\n", encoding="utf-8")
    proc = _butterfly_eval(
        "run",
        "--evals-md", str(md),
        "--adapter", "mock-passing",
        "--limit", "1",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode == 0, proc.stderr
    assert "▶ tau-bench" in proc.stdout


def test_eval_run_empty_manifest_errors(tmp_path):
    md = tmp_path / "evals.md"
    md.write_text("# nothing enabled\n", encoding="utf-8")
    proc = _butterfly_eval(
        "run",
        "--evals-md", str(md),
        "--adapter", "echo",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode != 0
    assert "nothing enabled" in proc.stderr


def test_eval_run_bad_adapter_config_errors(tmp_path):
    proc = _butterfly_eval(
        "run",
        "--enable", "tau-bench",
        "--adapter", "echo",
        "--adapter-config", "{not valid json",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode != 0
    assert "JSON" in proc.stderr


def test_eval_run_mode_smoke_forwarded(tmp_path):
    # Using --mode upstream against tau-bench's smoke adapter would
    # change the metadata; we use smoke to assert the flag plumbing
    # without depending on the upstream package being installed.
    proc = _butterfly_eval(
        "run",
        "--enable", "tau-bench",
        "--adapter", "mock-passing",
        "--mode", "smoke",
        "--limit", "1",
        "--evals-root", str(tmp_path / "_evals"),
    )
    assert proc.returncode == 0, proc.stderr
    assert "passed=1/1" in proc.stdout
