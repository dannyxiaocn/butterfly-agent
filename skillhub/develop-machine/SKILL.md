---
name: develop-machine
description: >
  Drive the Develop Machine workflow — a fixed three-stage pipeline
  (developer → reviewer → merger) for shipping a single minimal
  code-doc-test triplet end to end. Load this skill when the user asks to
  "run develop machine", "ship a triplet through the pipeline", asks for
  developer-reviewer-merger orchestration, or any time a change is small
  enough to fit one triplet and the caller wants the full review-and-land
  cycle in one tool call. The skill explains how to invoke the workflow
  tool with the three Develop Machine agents and how to interpret each
  stage's verdict.
---

## What this is

**Develop Machine** is the first concrete workflow built on butterfly's
`workflow` tool. It runs three sub-agent stages in strict sequence,
each implemented as its own agent under `agenthub/`:

1. **developer** — implements one minimal `code + doc + test` triplet
2. **reviewer** — verifies the triplet against disk
3. **merger** — runs tests, version-bumps, commits, pushes

Each stage's reply is spliced into the next stage's task as `{prev}`.
The pipeline preserves the full audit trail (each stage's reply is in
the parent's tool result) but does NOT leak intermediate tool calls,
thinking, or partial messages between stages.

## When to use it

Use Develop Machine when ALL of the following hold:

- The task fits one minimal triplet (code change small enough that
  *one* set of code/doc/test files closes it).
- You want the full developer → reviewer → merger handoff, not just a
  raw implementation.
- The work happens on a single branch, no cross-repo or cross-PR
  coordination.

If the task is multi-triplet (e.g. "refactor the whole tool engine"),
run Develop Machine once per triplet — don't try to cram several
features into one developer invocation. The minimization principle is
load-bearing for review quality.

If you just want a one-shot implementation without review/merge, call
`subagent_new` with `agent_name="developer"` directly instead.

## How to invoke it

The orchestrator agent must have the `workflow` tool in its `tools.md`.
Invocation shape:

```json
{
  "steps": [
    {
      "name": "develop",
      "agent": "developer",
      "mode": "executor",
      "task": "<the actual task spec — written as a clean prompt for a fresh agent; cite paths, constraints, acceptance criteria>"
    },
    {
      "name": "review",
      "agent": "reviewer",
      "mode": "explorer",
      "task": "Review this developer output against the repo. Verify minimality, the code-doc-test triplet, and that tests pass at the smallest scope.\n\n{prev}"
    },
    {
      "name": "merge",
      "agent": "merger",
      "mode": "executor",
      "task": "Land this approved triplet on the working branch per butterfly's versioning rule. Stop if you don't see [APPROVE].\n\n{prev}"
    }
  ]
}
```

Notes:

- The reviewer runs in `explorer` mode — it should not be writing to
  the repo, only reading.
- The developer and merger run in `executor` mode — both need write
  access (developer for the diff, merger for version bump + commit).
- `{prev}` resolves to the previous stage's final reply. Do not strip
  the verdict markers (`[DONE]`, `[APPROVE]`); the next stage uses them
  to decide whether to proceed.

## Interpreting verdicts

Each stage ends its reply with one of a small set of markers. Reading
the workflow's tool result, you can scan the trailing line of each
stage to know what happened:

| Stage     | Success    | Soft-failure       | Hard-failure |
|-----------|------------|--------------------|--------------|
| developer | `[DONE]`   | `[BLOCKED]`        | `[ERROR]`    |
| reviewer  | `[APPROVE]`| `[REQUEST_CHANGES]`| `[BLOCKED]`  |
| merger    | `[MERGED]` | `[BLOCKED]`        | `[ERROR]`    |

If the reviewer returns `[REQUEST_CHANGES]`, the merger's
no-`[APPROVE]` short-circuit kicks in and it will relay the block. The
calling agent then decides whether to re-run the workflow with the
fix-list spliced into the developer step, or surface the changes to the
user.

## Backgrounding

`workflow` is `backgroundable: true`. For long pipelines, set
`run_in_background: true` on the tool call and continue working —
the final aggregated log arrives as a background completion. Each
stage still shows up as its own panel card while it runs, so you can
inspect progress mid-flight.

## Limitations (v1)

- Strictly sequential — no parallel branches, no conditional steps.
- The merger cannot un-merge. If a problem is discovered after
  `[MERGED]`, fix it as a new triplet (the next pipeline run).
- One triplet per pipeline run. Multi-triplet tasks must be looped
  externally.

## See also

- `docs/butterfly/workflows/develop_machine.md` — design rationale
- `docs/butterfly/tool_engine/workflow.md` — the underlying `workflow` tool
- `agenthub/developer/`, `agenthub/reviewer/`, `agenthub/merger/` — the stage agents
