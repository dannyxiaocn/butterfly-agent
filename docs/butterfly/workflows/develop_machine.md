# Develop Machine — design

The **Develop Machine** is the first concrete workflow built on
butterfly's [`workflow` tool](../tool_engine/workflow.md). It runs a
fixed three-stage pipeline to ship one minimal code-doc-test triplet
end to end:

```
developer ──► reviewer ──► merger
   │             │            │
[DONE]      [APPROVE]      [MERGED]
[BLOCKED]   [REQUEST_      [BLOCKED]
[ERROR]      CHANGES]      [ERROR]
            [BLOCKED]
```

Each stage is a separate agent template under `agenthub/`:

- [`agenthub/developer/`](../../../agenthub/developer/)
- [`agenthub/reviewer/`](../../../agenthub/reviewer/)
- [`agenthub/merger/`](../../../agenthub/merger/)

The [`skillhub/develop-machine/`](../../../skillhub/develop-machine/SKILL.md)
skill is the user-facing entry point — it explains *when* to use the
pipeline and *how* to call the `workflow` tool with these three agents.

## Why a workflow, not a team?

Butterfly has two multi-agent primitives:

| Primitive | Topology | Coordination | UI shape |
|-----------|----------|--------------|----------|
| `kind: team` | flat chat-room | teamchat messages | one team session, members as sub-sessions |
| `workflow` tool | fixed pipeline | `{prev}` splice | one panel card per step under the calling session |

The Develop Machine is a **pipeline** — strict ordering, no back-talk
between stages, no shared scratch space. That's `workflow`. Modelling
it as a team would force chat semantics onto a one-way handoff and add
coordination that the work doesn't need.

## Why three stages?

The three-stage split mirrors the smallest viable code-shipping pipeline:

| Stage | Owns | Touches the repo? |
|-------|------|------------------|
| **developer** | Producing the triplet (code + doc + test). | Yes — `executor` mode. |
| **reviewer**  | Verifying the triplet against disk. | No — `explorer` mode (reads only). |
| **merger**    | Running tests, version bump, commit, push. | Yes — `executor` mode. |

Splitting reviewer and merger matters because verification and landing
have different blast radii. The reviewer's worst-case mistake is "let a
bad triplet through" (recoverable — fix forward). The merger's
worst-case mistake is "force-push the wrong thing" (much harder to
recover). Different jobs, different constraints, different agents.

## The atomic unit: code + doc + test

The developer's hard rule (see `agenthub/developer/prompts/system.md`)
is that every change ships as one triplet:

| Slot | Default location |
|------|------------------|
| code | `butterfly/<pkg>/...` or `toolhub/<name>/` |
| doc  | matching `docs/butterfly/<pkg>/design.md` (or sibling page) |
| test | `tests/butterfly/<pkg>/test_<name>.py` (mirrors source) |

Two non-negotiables:

1. **Minimality** — the diff does only what the task says. No
   refactors, no abstractions with a single caller, no defensive
   handling for cases that can't happen.
2. **Completeness** — all three slots present. If a slot legitimately
   doesn't apply (pure rename, doc-only edit), the developer says so
   *explicitly* in the `[DONE]` reply so the reviewer doesn't have to
   guess.

These two principles cut both ways:

- They constrain the developer to small, reviewable diffs.
- They constrain the reviewer to a small, deterministic checklist —
  the review itself stays minimal.

## Stage verdicts

Each stage ends its reply with a marker the next stage parses:

```
developer:  [DONE]  | [BLOCKED] | [ERROR]
reviewer:   [APPROVE] | [REQUEST_CHANGES] | [BLOCKED]
merger:     [MERGED] | [BLOCKED] | [ERROR]
```

The reviewer's `[REQUEST_CHANGES]` is the failure shape that closes the
loop cheaply — the orchestrator re-runs the workflow with the
fix-list as the developer's next task. `[BLOCKED]` propagates: any
stage that sees an upstream block relays it without trying to land
work. This keeps the pipeline well-typed — every reply has exactly one
of a known set of terminal markers, and downstream stages can branch
deterministically on them.

## Inheritance from butterfly's design

The pipeline reuses butterfly's existing machinery; no new toolhub or
session-engine code is required:

- **Sub-agent isolation** — each stage gets its own session under
  `sessions/<id>/`, its own `_sessions/<id>/` system twin, its own
  context. No bleed-through.
- **Audit trail** — `workflow` returns the per-stage replies
  concatenated with headers, so the calling agent retains the full
  history in its context. See
  [`docs/butterfly/tool_engine/workflow.md`](../tool_engine/workflow.md).
- **Panel cards** — each stage is one sub-agent panel entry, visible
  in the parent session's sidebar live.
- **Backgroundable** — `workflow` is `backgroundable: true`, so the
  whole pipeline can run as one fire-and-forget call.
- **Filesystem-as-everything** — stages communicate via:
  - the `{prev}` reply splice (in-context handoff)
  - shared on-disk state (`git`, the working tree)
  No databases, no sockets.

## Limitations (v1)

- **Strictly sequential.** No parallel review-and-test, no
  conditional fan-out. The `workflow` tool itself only does linear
  pipelines.
- **One triplet per run.** Multi-triplet tasks must be looped
  externally — the orchestrator splits the work into triplet-sized
  chunks and calls Develop Machine once per chunk.
- **No automatic rollback.** If the merger pushes and then a bug is
  discovered, the fix is a new triplet on the next run, not an
  un-merge.
- **No DAG.** A future v2 could introduce parallel review (e.g.
  reviewer + security-reviewer in parallel before the merger), but
  that requires extending the `workflow` tool itself, not the Develop
  Machine agents.

## See also

- [`docs/butterfly/tool_engine/workflow.md`](../tool_engine/workflow.md) — the underlying tool
- [`docs/butterfly/session_engine/agent_team.md`](../session_engine/agent_team.md) — the chat-based multi-agent primitive
- [`skillhub/develop-machine/SKILL.md`](../../../skillhub/develop-machine/SKILL.md) — how to call this workflow
