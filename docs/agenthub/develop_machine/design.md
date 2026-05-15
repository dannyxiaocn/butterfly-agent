# Develop-Machine — design

**Develop-Machine** is the first end-to-end workflow shipped in
`agenthub/`. It is a `kind: team` session whose three members
(developer · checker · reviewer) collaborate to take a user
requirement all the way to a "merged" draft PR.

## Why a team, not a workflow tool?

The existing `workflow` tool (see
[`docs/butterfly/tool_engine/workflow.md`](../../butterfly/tool_engine/workflow.md))
is a linear pipeline — one sub-agent's reply feeds the next. The
Develop-Machine flow is iterative: developer ↔ checker may loop N
times before reviewer is even pinged, and the reviewer round can kick
the loop back to developer. That control flow doesn't fit a linear
pipeline, but it does fit `kind: team` semantics where members
@-mention each other on `teamchat` and react to fresh PR comments.

So the workflow is **the team config plus the role prompts**. No new
runtime code is introduced; we ride the existing team-router /
teamchat plumbing verbatim.

## Roles

| Role        | Agent          | teamchat_mode | Leader? |
| ----------- | -------------- | ------------- | ------- |
| `developer` | `dm_developer` | `silent`      | yes     |
| `checker`   | `dm_checker`   | `default`     |         |
| `reviewer`  | `dm_reviewer`  | `silent`      |         |

- `developer` is leader so bare user input ("build me X") routes to
  them through the team router (see
  [`docs/butterfly/session_engine/agent_team.md`](../../butterfly/session_engine/agent_team.md)).
- `checker` is the only `default`-mode member. Every teamchat post
  fans out to them — that's what makes them the hub.
- `developer` and `reviewer` are `silent` so they wake **only** when
  explicitly `@`-mentioned. Combined with prompt-level rules, this
  enforces the communication graph below.

## Communication graph

```
                ┌──────────────┐
                │   user       │
                └──────┬───────┘
                       │ (bare input → leader)
                       ▼
                ┌──────────────┐         ┌──────────────┐
                │  developer   │◀─@dev───│   checker    │
                │ (dm_developer)│──@check▶│ (dm_checker) │
                └──────────────┘         └──────┬──────┬┘
                                                │@rev  │@check
                                                ▼      │
                                         ┌──────────────┐
                                         │   reviewer   │
                                         │ (dm_reviewer)│
                                         └──────────────┘
```

The rules — enforced by prompts, not by the runtime:

- **developer ↔ checker only.** Developer's prompt forbids
  `@reviewer`, `@all`, or any direct address to reviewer.
- **reviewer ↔ checker only.** Reviewer's prompt forbids
  `@developer`, `@all`, or any direct address to developer.
- **checker is the only translator.** Concerns from one side reach the
  other only after checker reads, decides whether they agree, and
  forwards (or pushes back).

If a developer or reviewer prompt regresses and they `@`-cross-talk,
the recipient is in `silent` mode so the cross-talk still wakes them.
That is intentional — silent mode is a soft guard, not a hard one. The
prompts are the contract. We may add a teamchat policy validator in a
later revision if drift becomes a problem in practice.

## The "PR"

There is no GitHub-side PR in v1. The PR is a single markdown file
in the team's shared workspace:

```
sessions/<team_id>/workspace/<feature_slug>/
  requirements.md          # written by developer after talking to the user
  PR.md                    # the "pull request" — status + comment thread
  <source files…>          # the actual implementation
```

`PR.md` schema is documented in
[`dm_developer/prompts/system.md`](../../../agenthub/dm_developer/prompts/system.md);
the high-level shape is:

```
# PR: <slug>
Status: draft | check-pending | check-failed | review-pending | approved | merged
Branch: dm/<slug>

## Summary
…

## Files changed
…

## Acceptance
- [ ] …

## Comments
### [developer] <ts>
…
### [checker] <ts>
…
### [reviewer] <ts>
…
```

Authority for the `Status:` field is split by transition:

| Transition                                | Who flips it |
| ----------------------------------------- | ------------ |
| `draft` → `check-pending`                 | developer    |
| `check-pending` → `check-failed`          | checker      |
| `check-failed` → `check-pending`          | developer    |
| `check-pending` → `review-pending`        | checker      |
| `review-pending` → `check-pending`        | checker (forwarding reviewer concerns) |
| `review-pending` → `approved`             | checker      |
| `approved` → `merged`                     | developer    |

Reviewer never flips `Status:` directly — concerns and approvals reach
the file as a comment block, and checker translates them into the
status change. Comments are append-only — every role appends, none
rewrite.

### Why a markdown file and not real `gh pr create`?

Two reasons:
- **Self-contained.** A first workflow shouldn't require GitHub
  credentials, a remote, or network. The flow has to be testable in
  CI without any external dependency.
- **Audit trail.** The PR file lives next to the code it describes,
  in the workspace the reviewer is already reading. The comment
  thread is the audit trail; there is no second source of truth.

A future revision can wrap `PR.md` with a GitHub-side draft PR using
the MCP tools — the prompts won't change.

## Shared workspace bootstrap

A `kind: team` session creates `sessions/<team_id>/` at init time but
does **not** pre-create a `workspace/` subdirectory — there's no
team-level agent loop to do it. Developer bootstraps the workspace on
its first activation:

```bash
member_id=$(basename "$(pwd)")
team_id=$(jq -r .member_of_team "../../_sessions/$member_id/manifest.json")
mkdir -p "../$team_id/workspace"
```

Path notes:
- Each member's bash cwd defaults to `sessions/<member_session_id>/`
  (see `butterfly_dev/prompts/env.md`), so the `_sessions/` sibling is
  reached via `../../_sessions/` and the team session via
  `../<team_id>/`.
- Checker and reviewer carry a read-only variant of the snippet (no
  `mkdir`) in their own prompts so any role can recover the workspace
  path independently.

Only the developer's prompt invokes `mkdir`; the developer owns the
workspace lifecycle. There is exactly one `<feature_slug>` directory
under `workspace/` at any time, so checker and reviewer discover it
by `ls`-ing the workspace.

## State machine

`Status:` in `PR.md` is the canonical state. The flow:

```
draft
  └── developer writes PR.md ───────────────▶ check-pending
        ├── checker finds blockers ─────────▶ check-failed
        │     └── developer pushes fix ─────▶ check-pending
        └── checker approves ───────────────▶ review-pending
              ├── reviewer requests changes ─▶ check-pending
              │     └── (back through checker → developer)
              └── reviewer approves ────────▶ approved
                    └── developer lands ────▶ merged
```

No role reads `Status:` to *decide* what to do — they decide from the
teamchat ping. `Status:` is for the human watching the session feed.

## Skipped on purpose (v1)

- **No real GitHub PR.** As above; the PR is a markdown file.
- **No automated CI.** Checker runs tests by hand with `bash`. CI
  would be a checker upgrade, not a workflow change.
- **No new runtime code.** Everything is config + prompts. If the
  prompts can't enforce a rule, we don't enforce it. Specifically:
  - Hard prevention of developer→reviewer cross-talk requires a
    teamchat fan-out filter; not needed yet.
  - Concurrent checker/reviewer rounds on different PRs in the same
    team would need per-PR teamchat threads; we assume one PR at a
    time.
- **No `task_create` poll loop.** Each role wakes on a teamchat
  interrupt; nobody polls.

## Authoring / extending

To swap in a different developer (say, a smaller model for cheap
prototyping), point `members[0].agent` at another agenthub directory
that follows the same `system.md` contract. The team manifest itself
is the only file you have to edit.

## Tests

See [`tests/agenthub/test_develop_machine.py`](../../../tests/agenthub/test_develop_machine.py)
— covers manifest validation, role-agent file presence, and
communication-rule invariants (modes, leader, member set).
