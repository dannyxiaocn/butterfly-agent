"""Tests for ``EvalLoader`` — the evalhub discovery + plugin loader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.eval_engine.loader import EvalLoader, _read_evals_md
from butterfly.eval_engine.benchmark import Benchmark


# ── repo-level fixtures ──────────────────────────────────────────────────────

def test_default_loader_finds_three_builtins():
    """The three plugins shipped under ``evalhub/`` must be discoverable."""
    loader = EvalLoader()
    ids = {info.id for info in loader.list_available()}
    assert {"swe-bench-verified", "terminal-bench", "tau-bench"} <= ids


def test_default_loader_reads_repo_evals_md():
    """``evals.md`` at the repo root enables every built-in plugin."""
    loader = EvalLoader()
    assert set(loader.list_enabled()) == {
        "swe-bench-verified", "terminal-bench", "tau-bench",
    }


def test_default_loader_returns_real_benchmark():
    loader = EvalLoader()
    bench = loader.load("tau-bench", mode="smoke")
    assert isinstance(bench, Benchmark)
    assert bench.info.id == "tau-bench"
    tasks = list(bench.iter_tasks(limit=1))
    assert len(tasks) == 1


# ── synthetic plugin layouts ─────────────────────────────────────────────────

def _seed_plugin(root: Path, name: str, *, info: dict, adapter_src: str) -> None:
    plugin = root / name
    plugin.mkdir(parents=True)
    (plugin / "eval.json").write_text(json.dumps(info), encoding="utf-8")
    (plugin / "adapter.py").write_text(adapter_src, encoding="utf-8")


_STUB_ADAPTER = '''
from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.types import EvalTask, TaskResult


class Adapter(Benchmark):
    def __init__(self, info, *, mode: str = "auto"):
        self.info = info
        self.mode = mode

    def iter_tasks(self, *, limit=None):
        yield EvalTask(task_id="t0", benchmark=self.info.id, prompt="hi",
                       metadata={"mode": self.mode})

    def grade(self, task, submission):
        return TaskResult(
            task_id=task.task_id,
            status="passed" if submission.output == "ok" else "failed",
            score=1.0 if submission.output == "ok" else 0.0,
            submission=submission,
        )
'''


def test_loader_discovers_custom_plugin(tmp_path):
    _seed_plugin(
        tmp_path,
        "custom",
        info={
            "id": "custom", "name": "Custom", "description": "stub",
            "homepage": "", "task_type": "stub", "metric": "ok",
        },
        adapter_src=_STUB_ADAPTER,
    )
    loader = EvalLoader(evalhub_dir=tmp_path)
    ids = {info.id for info in loader.list_available()}
    assert ids == {"custom"}
    bench = loader.load("custom", mode="smoke")
    assert bench.info.id == "custom"
    assert bench.mode == "smoke"


def test_loader_reads_custom_evals_md(tmp_path):
    _seed_plugin(tmp_path, "a", info={
        "id": "a", "name": "A", "description": "", "homepage": "",
        "task_type": "stub", "metric": "ok",
    }, adapter_src=_STUB_ADAPTER)
    _seed_plugin(tmp_path, "b", info={
        "id": "b", "name": "B", "description": "", "homepage": "",
        "task_type": "stub", "metric": "ok",
    }, adapter_src=_STUB_ADAPTER)
    md = tmp_path / "evals.md"
    md.write_text("# comment\na\n\n# skipped\nb\n", encoding="utf-8")
    loader = EvalLoader(evalhub_dir=tmp_path, evals_md_path=md)
    assert loader.list_enabled() == ["a", "b"]


def test_load_enabled_returns_adapter_instances(tmp_path):
    for name in ("a", "b"):
        _seed_plugin(tmp_path, name, info={
            "id": name, "name": name, "description": "", "homepage": "",
            "task_type": "stub", "metric": "ok",
        }, adapter_src=_STUB_ADAPTER)
    md = tmp_path / "evals.md"
    md.write_text("a\nb\n", encoding="utf-8")
    loader = EvalLoader(evalhub_dir=tmp_path, evals_md_path=md)
    adapters = loader.load_enabled(adapter_configs={"a": {"mode": "smoke"}})
    assert [a.info.id for a in adapters] == ["a", "b"]
    assert adapters[0].mode == "smoke"


def test_load_missing_plugin_raises(tmp_path):
    loader = EvalLoader(evalhub_dir=tmp_path)
    with pytest.raises(KeyError):
        loader.load("nope")


def test_load_enabled_skips_unknown(tmp_path, capsys):
    md = tmp_path / "evals.md"
    md.write_text("missing-plugin\n", encoding="utf-8")
    loader = EvalLoader(evalhub_dir=tmp_path, evals_md_path=md)
    out = loader.load_enabled()
    assert out == []
    err = capsys.readouterr().err
    assert "missing-plugin" in err


def test_load_rejects_adapter_without_class(tmp_path):
    plugin = tmp_path / "broken"
    plugin.mkdir()
    (plugin / "eval.json").write_text(
        json.dumps({
            "id": "broken", "name": "broken", "description": "",
            "homepage": "", "task_type": "stub", "metric": "ok",
        }),
        encoding="utf-8",
    )
    (plugin / "adapter.py").write_text(
        "# missing Adapter class\nx = 1\n", encoding="utf-8",
    )
    loader = EvalLoader(evalhub_dir=tmp_path)
    with pytest.raises(ImportError):
        loader.load("broken")


def test_info_tolerates_unknown_keys(tmp_path):
    """Forward-compat: an unknown JSON key shouldn't crash discovery."""
    plugin = tmp_path / "future"
    plugin.mkdir()
    (plugin / "eval.json").write_text(
        json.dumps({
            "id": "future", "name": "future", "description": "",
            "homepage": "", "task_type": "stub", "metric": "ok",
            "v3_extra_field": "ignored",
        }),
        encoding="utf-8",
    )
    (plugin / "adapter.py").write_text(_STUB_ADAPTER, encoding="utf-8")
    loader = EvalLoader(evalhub_dir=tmp_path)
    info = loader.get_info("future")
    assert info is not None
    assert info.id == "future"


def test_read_evals_md_handles_comments_and_blanks(tmp_path):
    md = tmp_path / "evals.md"
    md.write_text(
        "# header\n\nfoo\n  # indented comment\n  bar  \n\n",
        encoding="utf-8",
    )
    assert _read_evals_md(md) == ["foo", "bar"]


def test_read_evals_md_missing_returns_empty(tmp_path):
    assert _read_evals_md(tmp_path / "absent.md") == []
