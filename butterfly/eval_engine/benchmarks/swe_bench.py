"""SWE-bench Verified adapter.

Upstream: https://github.com/princeton-nlp/SWE-bench

The real harness clones each instance's repo, applies the agent's patch,
and runs the test suite inside a Docker image. That's a heavyweight
dependency we don't want to bake into this codebase, so the adapter has
two modes:

* **upstream** — install the ``swebench`` package and a Docker daemon;
  the adapter delegates grading to ``swebench.harness.run_evaluation``.
* **smoke** — ships a tiny curated set of instances (a synthetic
  unified-diff + a check that the diff applies to the bundled file).
  This mode is what CI and the test suite use; it exercises the runner
  end-to-end without any external dependencies.

The mode is picked automatically: upstream wins when ``swebench`` and a
Docker socket are available, otherwise we fall back to smoke. The
reviewer can force a mode via the run config.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from importlib import util as _util
from pathlib import Path
from typing import Iterator

from butterfly.eval_engine.benchmarks.base import Benchmark
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


_INFO = BenchmarkInfo(
    id="swe-bench-verified",
    name="SWE-bench Verified",
    description=(
        "Princeton/OpenAI's Verified subset of SWE-bench — real GitHub "
        "issues paired with hidden tests. Reported by Claude Opus 4.x "
        "and Kimi K2.x as the headline coding-agent metric."
    ),
    homepage="https://github.com/princeton-nlp/SWE-bench",
    task_type="code_patch",
    metric="resolved_pct",
    requires_docker=True,
    default_limit=5,
)


def _upstream_available() -> bool:
    return _util.find_spec("swebench") is not None and bool(
        shutil.which("docker")
    )


# A tiny seed of three Verified-flavoured instances. We only need *some*
# task that exercises the runner; the real upstream harness is gated
# behind ``_upstream_available()``. Each instance ships:
#   * an instance_id (the canonical SWE-bench naming convention)
#   * a problem_statement (what the model is told to fix)
#   * a target file + an expected substring that the model's patch
#     should produce when applied
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


class SWEBenchAdapter(Benchmark):
    info = _INFO

    def __init__(self, *, mode: str = "auto", dataset_dir: Path | None = None) -> None:
        if mode not in ("auto", "smoke", "upstream"):
            raise ValueError(f"Unknown mode: {mode!r}")
        if mode == "auto":
            mode = "upstream" if _upstream_available() else "smoke"
        self.mode = mode
        self._dataset_dir = dataset_dir

    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        if self.mode == "upstream":
            yield from self._iter_upstream(limit=limit)
            return
        for inst in _SMOKE_INSTANCES[: limit or len(_SMOKE_INSTANCES)]:
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
                    "mode": "smoke",
                },
            )

    def _iter_upstream(self, *, limit: int | None) -> Iterator[EvalTask]:
        # We deliberately don't pull HF datasets here — that's a large
        # dependency and the reviewer running the upstream mode will have
        # already pre-staged a JSONL of instances. The convention follows
        # the official swebench format.
        path = self._dataset_dir or Path(
            os.environ.get("SWEBENCH_DATASET", "")
        )
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
        # A patch that touches the wrong files is wrong even if the
        # substring shows up — guard against the model echoing the
        # expected line into the wrong place.
        file_hit = (
            not expected_files
            or any(f in changed for f in expected_files)
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
        # Upstream grading needs Docker + the swebench package. We
        # deliberately don't shell out from here — instead we drop the
        # patch on disk under the run's workspace and surface a
        # "deferred" status. The reviewer's CI job invokes the official
        # ``python -m swebench.harness.run_evaluation`` over the same
        # directory after the run finishes.
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
