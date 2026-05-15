"""SWE-bench Verified adapter.

Upstream: https://github.com/princeton-nlp/SWE-bench

Smoke mode ships a tiny curated set of instances we can grade in-process
(unified-diff syntax + a target file/substring check). Upstream mode
delegates to the official ``swebench`` harness when ``swebench`` is
installed and a Docker daemon is reachable. Auto-detected; force a
mode with ``adapter_config: {"mode": "smoke" | "upstream"}``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from importlib import util as _util
from pathlib import Path
from typing import Iterator

from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


def _upstream_available() -> bool:
    return _util.find_spec("swebench") is not None and bool(
        shutil.which("docker")
    )


_SMOKE_INSTANCES: list[dict] = [
    {
        "instance_id": "smoke__off-by-one",
        "problem_statement": (
            "The `last_index` helper returns ``len(seq)`` instead of "
            "``len(seq) - 1`` for a non-empty sequence. Produce a unified "
            "diff that fixes the off-by-one and includes the line "
            "``return len(seq) - 1``."
        ),
        "expected_substrings": ["return len(seq) - 1"],
        "expected_changed_files": ["utils.py"],
    },
    {
        "instance_id": "smoke__null-guard",
        "problem_statement": (
            "`format_user` crashes when ``user`` is ``None``. Produce a "
            "unified diff that adds an early ``if user is None: return "
            "\"\"`` guard."
        ),
        "expected_substrings": ["if user is None"],
        "expected_changed_files": ["formatter.py"],
    },
    {
        "instance_id": "smoke__missing-import",
        "problem_statement": (
            "`parse_iso` calls ``datetime.fromisoformat`` without "
            "importing ``datetime``. Produce a unified diff adding "
            "``from datetime import datetime`` at the top of the file."
        ),
        "expected_substrings": ["from datetime import datetime"],
        "expected_changed_files": ["dates.py"],
    },
]


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def _extract_changed_files(patch: str) -> list[str]:
    return sorted({m.group(2) for m in _DIFF_PATH_RE.finditer(patch)})


class Adapter(Benchmark):
    """SWE-bench Verified — exposed via evalhub plugin conventions."""

    def __init__(
        self,
        info: BenchmarkInfo,
        *,
        mode: str = "auto",
        dataset_dir: Path | None = None,
    ) -> None:
        if mode not in ("auto", "smoke", "upstream"):
            raise ValueError(f"Unknown mode: {mode!r}")
        if mode == "auto":
            mode = "upstream" if _upstream_available() else "smoke"
        self.info = info
        self.mode = mode
        self._dataset_dir = dataset_dir

    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        if self.mode == "upstream":
            yield from self._iter_upstream(limit=limit)
            return
        for inst in _SMOKE_INSTANCES[: limit or len(_SMOKE_INSTANCES)]:
            target_file = inst["expected_changed_files"][0]
            expected_line = inst["expected_substrings"][0]
            # Pre-canned reference solution — used by the CLI's
            # mock-passing adapter as a universal "pass" signal so the
            # wiring sanity check works for every benchmark, not just
            # tau-bench (PR #72 review item 1).
            expected_output = (
                f"diff --git a/{target_file} b/{target_file}\n"
                f"--- a/{target_file}\n"
                f"+++ b/{target_file}\n"
                "@@\n"
                f"+    {expected_line}\n"
            )
            yield EvalTask(
                task_id=inst["instance_id"],
                benchmark=self.info.id,
                prompt=(
                    f"You are solving SWE-bench instance "
                    f"`{inst['instance_id']}`.\n\n"
                    f"{inst['problem_statement']}\n\n"
                    "Reply with ONLY a unified diff (``diff --git ...``)."
                ),
                metadata={
                    "instance_id": inst["instance_id"],
                    "expected_substrings": inst["expected_substrings"],
                    "expected_changed_files": inst["expected_changed_files"],
                    "expected_output": expected_output,
                    "mode": "smoke",
                },
            )

    def _iter_upstream(self, *, limit: int | None) -> Iterator[EvalTask]:
        path = self._dataset_dir or Path(os.environ.get("SWEBENCH_DATASET", ""))
        if not path or not path.is_file():
            raise FileNotFoundError(
                "SWE-bench Verified dataset JSONL not found. Set "
                "SWEBENCH_DATASET=<path-to-verified.jsonl> or pass "
                "dataset_dir to the adapter."
            )
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if limit is not None and i >= limit:
                    break
                inst = json.loads(line)
                yield EvalTask(
                    task_id=inst["instance_id"],
                    benchmark=self.info.id,
                    prompt=inst.get("problem_statement", ""),
                    metadata={
                        "instance_id": inst["instance_id"],
                        "repo": inst.get("repo"),
                        "base_commit": inst.get("base_commit"),
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
            return self._grade_upstream(task, submission)
        return self._grade_smoke(task, submission)

    def _grade_smoke(self, task: EvalTask, submission: Submission) -> TaskResult:
        patch = submission.output or ""
        expected_subs = task.metadata.get("expected_substrings", [])
        expected_files = task.metadata.get("expected_changed_files", [])
        changed = _extract_changed_files(patch)
        sub_hit = all(s in patch for s in expected_subs)
        file_hit = (
            not expected_files or any(f in changed for f in expected_files)
        )
        looks_like_patch = patch.lstrip().startswith("diff --git ")
        passed = sub_hit and file_hit and looks_like_patch
        return TaskResult(
            task_id=task.task_id,
            status="passed" if passed else "failed",
            score=1.0 if passed else 0.0,
            details={
                "substring_hit": sub_hit,
                "file_hit": file_hit,
                "looks_like_patch": looks_like_patch,
                "changed_files": changed,
            },
            submission=submission,
        )

    def _grade_upstream(self, task: EvalTask, submission: Submission) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            status="skipped",
            details={
                "note": (
                    "upstream grading is deferred to the swebench harness; "
                    "re-grade with `python -m swebench.harness.run_evaluation`."
                ),
                "patch": submission.output,
            },
            submission=submission,
        )
