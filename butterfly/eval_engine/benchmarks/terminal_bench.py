"""Terminal-bench adapter.

Upstream: https://github.com/laude-institute/terminal-bench

Like SWE-bench, the real harness runs each task in a Docker container
with a tmux session attached. We run the agent's command(s) on the host
in **smoke mode** (sandboxed under a tmp directory) and delegate to the
upstream ``tb`` CLI in **upstream mode** when it's available.

Submission contract: the agent's ``output`` is a single shell command
(or a ``\n``-separated sequence). Each smoke task ships a checker
function that inspects the post-run state (file contents, exit code).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from importlib import util as _util
from pathlib import Path
from typing import Callable, Iterator

from butterfly.eval_engine.benchmarks.base import Benchmark
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


_INFO = BenchmarkInfo(
    id="terminal-bench",
    name="Terminal-bench",
    description=(
        "Laude Institute's terminal/shell agent benchmark — multi-step "
        "tasks where the agent must drive a real shell to completion. "
        "Reported by Claude Opus 4.x and Kimi K2.x."
    ),
    homepage="https://github.com/laude-institute/terminal-bench",
    task_type="terminal",
    metric="success_rate",
    requires_docker=True,
    default_limit=5,
)


def _upstream_available() -> bool:
    return _util.find_spec("terminal_bench") is not None and bool(
        shutil.which("docker")
    )


def _check_hello(workdir: Path) -> tuple[bool, dict]:
    target = workdir / "hello.txt"
    if not target.is_file():
        return False, {"reason": "hello.txt missing"}
    text = target.read_text(encoding="utf-8").strip()
    return text == "hello world", {"contents": text}


def _check_count_files(workdir: Path) -> tuple[bool, dict]:
    target = workdir / "count.txt"
    if not target.is_file():
        return False, {"reason": "count.txt missing"}
    expected = sum(1 for p in workdir.iterdir() if p.is_file() and p.name != "count.txt")
    try:
        actual = int(target.read_text(encoding="utf-8").strip())
    except ValueError:
        return False, {"reason": "count.txt is not an integer"}
    return actual == expected, {"expected": expected, "actual": actual}


def _check_tarball(workdir: Path) -> tuple[bool, dict]:
    target = workdir / "out.tar.gz"
    if not target.is_file():
        return False, {"reason": "out.tar.gz missing"}
    return target.stat().st_size > 0, {"size": target.stat().st_size}


_SMOKE_TASKS: list[dict] = [
    {
        "task_id": "smoke__write-hello",
        "instructions": (
            "Create a file named ``hello.txt`` in the current directory "
            "whose contents are exactly ``hello world`` (no trailing "
            "newline beyond what ``echo -n`` would produce)."
        ),
        "setup": [],
        "checker": _check_hello,
    },
    {
        "task_id": "smoke__count-files",
        "instructions": (
            "Count how many regular files exist in the current "
            "directory (excluding ``count.txt`` itself) and write the "
            "count to ``count.txt``."
        ),
        "setup": ["touch a.txt b.txt c.txt"],
        "checker": _check_count_files,
    },
    {
        "task_id": "smoke__tar-archive",
        "instructions": (
            "Create a gzip-compressed tarball ``out.tar.gz`` containing "
            "every regular file in the current directory."
        ),
        "setup": ["echo one > one.txt", "echo two > two.txt"],
        "checker": _check_tarball,
    },
]


# Commands we refuse to execute even in the host-sandbox smoke runner.
# This is a defence-in-depth layer; the real protection is the dedicated
# tmpdir + the fact that the agent is asked to solve a tiny task. We
# still reject anything pointing outside the workdir or invoking package
# managers / network tooling.
_BANNED_TOKENS = (
    "sudo",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "mkfs",
    ":(){:|:&};:",
    "curl ",
    "wget ",
    "scp ",
    "ssh ",
    "apt ",
    "apt-get",
    "yum ",
    "pip ",
    "npm ",
    "shutdown",
    "reboot",
    "chmod 777 /",
    "/etc/passwd",
    "/etc/shadow",
)


def _is_safe(command: str) -> tuple[bool, str | None]:
    lower = command.lower()
    for tok in _BANNED_TOKENS:
        if tok in lower:
            return False, f"refused: command contains {tok!r}"
    return True, None


class TerminalBenchAdapter(Benchmark):
    info = _INFO

    def __init__(self, *, mode: str = "auto", timeout_s: float = 20.0) -> None:
        if mode not in ("auto", "smoke", "upstream"):
            raise ValueError(f"Unknown mode: {mode!r}")
        if mode == "auto":
            mode = "upstream" if _upstream_available() else "smoke"
        self.mode = mode
        self.timeout_s = timeout_s

    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        if self.mode == "upstream":
            yield from self._iter_upstream(limit=limit)
            return
        for spec in _SMOKE_TASKS[: limit or len(_SMOKE_TASKS)]:
            yield EvalTask(
                task_id=spec["task_id"],
                benchmark=self.info.id,
                prompt=(
                    "You are solving a Terminal-bench shell task. "
                    "Reply with ONLY the shell command(s) needed, "
                    "one per line, no Markdown fences.\n\n"
                    f"Task: {spec['instructions']}"
                ),
                metadata={
                    "task_id": spec["task_id"],
                    "setup": list(spec["setup"]),
                    "mode": "smoke",
                },
            )

    def _iter_upstream(self, *, limit: int | None) -> Iterator[EvalTask]:
        # Upstream mode mirrors the swebench-adapter pattern: we don't
        # ship the dataset, the reviewer points TBENCH_DATASET at a
        # task-index JSONL produced by ``tb tasks list --json``.
        path = Path(os.environ.get("TBENCH_DATASET", ""))
        if not path or not path.is_file():
            raise FileNotFoundError(
                "Terminal-bench task index not found. Export "
                "TBENCH_DATASET=<path-to-tasks.jsonl>."
            )
        import json
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if limit is not None and i >= limit:
                    break
                spec = json.loads(line)
                yield EvalTask(
                    task_id=spec["task_id"],
                    benchmark=self.info.id,
                    prompt=spec.get("instructions", ""),
                    metadata={
                        "task_id": spec["task_id"],
                        "mode": "upstream",
                    },
                )

    def grade(self, task: EvalTask, submission: Submission) -> TaskResult:
        if submission.error:
            return TaskResult(
                task_id=task.task_id,
                status="errored",
                details={"error": submission.error},
                submission=submission,
            )
        if task.metadata.get("mode") == "upstream":
            return TaskResult(
                task_id=task.task_id,
                status="skipped",
                details={
                    "note": (
                        "upstream grading is deferred to the terminal-bench "
                        "harness; re-grade with `tb run --submission <path>`."
                    ),
                    "command": submission.output,
                },
                submission=submission,
            )
        return self._grade_smoke(task, submission)

    def _grade_smoke(self, task: EvalTask, submission: Submission) -> TaskResult:
        command = (submission.output or "").strip()
        if not command:
            return TaskResult(
                task_id=task.task_id,
                status="failed",
                details={"reason": "empty submission"},
                submission=submission,
            )
        safe, reason = _is_safe(command)
        if not safe:
            return TaskResult(
                task_id=task.task_id,
                status="errored",
                details={"reason": reason, "command": command},
                submission=submission,
            )
        spec = next((s for s in _SMOKE_TASKS if s["task_id"] == task.task_id), None)
        if spec is None:
            return TaskResult(
                task_id=task.task_id,
                status="errored",
                details={"reason": f"unknown smoke task {task.task_id!r}"},
                submission=submission,
            )
        with tempfile.TemporaryDirectory(prefix="tbench_") as tmp:
            workdir = Path(tmp)
            for setup in spec["setup"]:
                subprocess.run(
                    setup, cwd=workdir, shell=True,
                    capture_output=True, timeout=self.timeout_s,
                )
            try:
                proc = subprocess.run(
                    command, cwd=workdir, shell=True,
                    capture_output=True, timeout=self.timeout_s,
                    text=True,
                )
            except subprocess.TimeoutExpired as exc:
                return TaskResult(
                    task_id=task.task_id,
                    status="errored",
                    details={"reason": "command timed out", "command": command,
                             "timeout_s": self.timeout_s, "stderr": exc.stderr or ""},
                    submission=submission,
                )
            check_fn: Callable[[Path], tuple[bool, dict]] = spec["checker"]
            passed, info = check_fn(workdir)
        return TaskResult(
            task_id=task.task_id,
            status="passed" if passed else "failed",
            score=1.0 if passed else 0.0,
            details={
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                **info,
            },
            submission=submission,
        )
