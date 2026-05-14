# EvalEngine

Long-horizon agent evaluation harness. Lives at `butterfly/eval_engine/`
and exposes an HTTP service under `/api/eval/...`.

## What it evaluates

Three benchmarks — the canonical long-horizon set reported by both
Kimi K2.x and Claude Opus 4.x model cards in 2025-2026:

| id | benchmark | task type | upstream |
| -- | --------- | --------- | -------- |
| `swe-bench-verified` | SWE-bench Verified | code patches | https://github.com/princeton-nlp/SWE-bench |
| `terminal-bench`     | Terminal-bench     | shell tasks  | https://github.com/laude-institute/terminal-bench |
| `tau-bench`          | TAU-bench          | tool-use dialogue | https://github.com/sierra-research/tau-bench |

Each adapter ships in two modes:

- **smoke** — a tiny bundled task set graded in-process with no
  external dependencies. This is what tests + CI run.
- **upstream** — delegates to the official harness (Docker for SWE-bench
  and Terminal-bench, pure Python for TAU-bench). Auto-selected when
  the upstream package + required tooling are available.

The reviewer can pin the mode via `adapter_config: {"mode": "smoke"}`
or `"upstream"` in the run-request body.

## Architecture

```
butterfly/eval_engine/
  types.py            EvalTask / Submission / TaskResult / EvalRun / RunSummary
  agent_adapter.py    AgentAdapter protocol + CallableAdapter + ButterflyAgentAdapter
  benchmarks/         one file per benchmark, each implements Benchmark
  registry.py         {id -> factory} for benchmarks
  store.py            on-disk persistence (run.json + results.jsonl per run)
  runner.py           execute_run(benchmark, adapter, store) — asyncio
  service.py          EvalService — singleton + submit/list/get/cancel/delete
  api.py              FastAPI router under /api/eval
```

The runner is benchmark-agnostic: `Benchmark.iter_tasks()` yields
`EvalTask`s, the runner asks `AgentAdapter.run(task)` for a
`Submission`, then `Benchmark.grade(task, submission)` produces a
`TaskResult`. The runner persists each result through `EvalStore` as
it lands so the API can show live progress.

## HTTP surface

All endpoints under `/api/eval`:

| method | path | purpose |
| ------ | ---- | ------- |
| GET    | `/benchmarks`               | catalog (id, name, metric, mode info) |
| GET    | `/adapters`                 | adapters registered for HTTP launch |
| GET    | `/runs`                     | list every persisted run |
| POST   | `/runs`                     | schedule a run (sync or fire-and-forget) |
| GET    | `/runs/{run_id}`            | run metadata + summary |
| GET    | `/runs/{run_id}/results`    | per-task results |
| POST   | `/runs/{run_id}/cancel`     | signal cancellation |
| DELETE | `/runs/{run_id}`            | delete a finished run |

### Request: `POST /api/eval/runs`

```json
{
  "benchmark":   "tau-bench",
  "adapter":     "echo",
  "limit":       3,
  "parallel":    1,
  "adapter_config": {"mode": "smoke", "domain": "retail"},
  "sync":        false
}
```

`sync: false` (default) returns immediately with a queued `EvalRun`;
the reviewer polls `GET /runs/{id}`. `sync: true` blocks until the
run finishes and returns the final `EvalRun`.

## Plugging in a real agent

The HTTP layer can only refer to adapters by string name. Register
your agent once at process startup:

```python
from butterfly.eval_engine.api import register_adapter
from butterfly.eval_engine import ButterflyAgentAdapter

register_adapter(
    "claude-opus-4-7",
    ButterflyAgentAdapter(lambda: build_my_agent(), name="claude-opus-4-7"),
)
```

Then a reviewer calls:

```
POST /api/eval/runs
{"benchmark": "swe-bench-verified", "adapter": "claude-opus-4-7"}
```

Anything implementing the `AgentAdapter` protocol works —
`CallableAdapter` wraps a plain function for the simplest case.

## Persistence layout

```
<repo>/_evals/<run_id>/
  run.json         # EvalRun serialised (status, summary, timestamps)
  results.jsonl    # one TaskResult per line, append-only
```

`<run_id>` follows `YYYYMMDD-HHMMSS-<benchmark>-<uuid4>`.
