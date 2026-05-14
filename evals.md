# Repo-level evals.md — which benchmarks the eval engine runs by default.
#
# One eval name per line. Lines starting with `#` are comments. Names
# must match a directory under `evalhub/<name>/`. The CLI flag
# `--enable a,b,c` overrides this file for a single invocation; the
# HTTP `POST /api/eval/runs` body still drives a single benchmark per
# run.

swe-bench-verified
terminal-bench
tau-bench
