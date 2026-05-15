# EvalEngine

Long-horizon agent evaluation harness. Code lives at
`butterfly/eval_engine/`. Benchmarks ship as **evalhub plugins**
(mirrors of `toolhub` / `skillhub`).

## What it evaluates

Three built-in plugins — the canonical long-horizon set reported by
both Kimi K2.x and Claude Opus 4.x model cards in 2025-2026:

| id | benchmark | task type | upstream |
| -- | --------- | --------- | -------- |
| `swe-bench-verified` | SWE-bench Verified | code patches | https://github.com/princeton-nlp/SWE-bench |
| `terminal-bench`     | Terminal-bench     | shell tasks  | https://github.com/laude-institute/terminal-bench |
| `tau-bench`          | TAU-bench          | tool-use dialogue | https://github.com/sierra-research/tau-bench |

Each plugin runs in two modes:

- **smoke** — a tiny bundled task set graded in-process with no
  external dependencies. CI uses this.
- **upstream** — delegates to the official harness (Docker for
  SWE-bench / Terminal-bench, pure Python for TAU-bench). Auto-selected
  when the upstream package + Docker are available.

Force mode via `adapter_config: {"mode": "smoke"}` (API) or
`--mode smoke` (CLI).

## Layout

```
evalhub/<name>/
  eval.json        # static descriptor (BenchmarkInfo)
  adapter.py       # defines `class Adapter(Benchmark)`

evals.md           # one enabled eval name per line (whitelist)

butterfly/eval_engine/
  types.py         EvalTask / Submission / TaskResult / EvalRun / RunSummary
  benchmark.py     Benchmark ABC — plugins inherit this
  agent_adapter.py AgentAdapter protocol + Callable / Butterfly adapters
  loader.py        EvalLoader — reads evalhub/<name>/, dynamic import
  registry.py      thin shim — loader-first, in-process overrides for tests
  store.py         on-disk persistence (run.json + results.jsonl per run)
  runner.py        asyncio runner; bounded parallelism; cancellation
  service.py       EvalService — singleton + submit/list/get/cancel/delete
  api.py           FastAPI router under /api/eval (mounted by ui/web/app.py)
  cli.py           `butterfly eval [list|run]` argparse wiring
```

## Adding a new benchmark

1. Create `evalhub/<your-bench>/` with an `eval.json` matching
   `BenchmarkInfo` and an `adapter.py` exposing
   `class Adapter(Benchmark)` whose constructor takes
   `(info: BenchmarkInfo, **kwargs)`.
2. Add `<your-bench>` on its own line in `evals.md` (or pass
   `--enable <your-bench>` on the CLI).
3. Done — `butterfly eval list` and `GET /api/eval/benchmarks` pick it
   up automatically.

## CLI

```bash
# Catalog
butterfly eval list
butterfly eval list --json

# Run benchmarks from evals.md
butterfly eval run --adapter mock-passing --limit 3

# Run a subset, forcing smoke mode
butterfly eval run --enable tau-bench,terminal-bench \
    --adapter mock-passing --mode smoke --limit 2

# Plug in a real agent registered via register_adapter()
butterfly eval run --enable swe-bench-verified --adapter claude-opus-4-7
```

`--enable a,b,c` overrides `evals.md` for one invocation. `--adapter`
defaults to `echo` (the no-op baseline); `mock-passing` echoes each
task's expected output for a wiring sanity check. Anything else is
resolved against the process-local registry filled by
`butterfly.eval_engine.api.register_adapter`.

Per-benchmark forks: `--adapter-config '{"domain": "retail"}'` passes
JSON kwargs into every adapter's `__init__`.

## HTTP surface — `/api/eval/...`

| method | path | purpose |
| ------ | ---- | ------- |
| GET    | `/benchmarks`               | catalog (evalhub plugins + overrides) |
| GET    | `/adapters`                 | adapter names registered for HTTP launch |
| GET    | `/runs`                     | list every persisted run |
| POST   | `/runs`                     | schedule a run (sync or fire-and-forget) |
| GET    | `/runs/{run_id}`            | run metadata + live summary |
| GET    | `/runs/{run_id}/results`    | per-task TaskResult list |
| POST   | `/runs/{run_id}/cancel`     | signal cancellation |
| DELETE | `/runs/{run_id}`            | delete a finished run |

### Request body for `POST /api/eval/runs`

```json
{
  "benchmark":      "tau-bench",
  "adapter":        "echo",
  "limit":          3,
  "parallel":       1,
  "adapter_config": {"mode": "smoke", "domain": "retail"},
  "sync":           false
}
```

`sync: false` returns immediately with a queued `EvalRun`; poll
`GET /runs/{id}` to watch progress. `sync: true` blocks until the
run finishes.

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

Then a reviewer calls `POST /api/eval/runs {"benchmark": "...",
"adapter": "claude-opus-4-7"}` or `butterfly eval run --adapter
claude-opus-4-7`.

## Persistence layout

```
<repo>/_evals/<run_id>/
  run.json         # EvalRun serialised
  results.jsonl    # one TaskResult per line, append-only
```

`<run_id>` = `YYYYMMDD-HHMMSS-<benchmark>-<uuid4>`.
