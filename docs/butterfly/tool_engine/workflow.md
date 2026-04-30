# Workflow — design

A **workflow** is a tool that runs an ordered list of sub-agent steps in
sequence. Each step spawns a child session through the existing
`SubAgentTool` machinery, so every step lands in the parent's panel as a
sub-session card with no special UI work — same shape the live `subagent_new`
flow already renders.

The tool body is intentionally trivial: it loops over `steps` and calls
`SubAgentTool.execute(...)` per step, splicing the previous step's reply
into the next step's prompt at the literal `{prev}` placeholder.

## Why "just sub-agents"?

Earlier drafts considered an in-process `InlineAgentRunner` that would
load an agent and call `agent.run()` directly. That's faster but invisible
to the UI: no panel card, no events.jsonl streaming, no sidebar entry. The
team agreement is that workflows show up the same way as sub-agents — one
panel card per step — so we use the same path verbatim.

## Schema

```json
{
  "steps": [
    {"name": "research",  "agent": "agent",         "task": "Find recent..."},
    {"name": "critique",  "agent": "butterfly_dev", "task": "Critique:\n{prev}", "mode": "explorer"},
    {"name": "draft",                                "task": "Final report based on:\n{prev}"}
  ]
}
```

  * `name` — short display label for the panel card (≤ 40 chars).
  * `agent` — which agent to spawn (any `agenthub/<name>/`). Optional;
    defaults to the calling agent. The child session inherits the agent's
    full tool list, prompt, and config.
  * `task` — the sub-agent's first user input. `{prev}` is replaced with
    the previous step's reply (empty string for the first step).
  * `mode` — `explorer` (Guardian-sandboxed) or `executor` (full write
    access). Default `executor`.

## Background mode

`workflow` is `backgroundable: true`. With `run_in_background=true` the
parent fires the whole pipeline and gets a placeholder result; the
final aggregated log arrives later as a `tool_output(task_id=...)`-style
notification, same path as backgrounded `subagent_new` and `bash`. Each
step's child session is still individually visible in the panel as it
runs.

The `WorkflowRunner` writes per-step progress meta onto its panel entry
(`current_step`, `step_count`, `step_name`) so the UI can show
"step 2 of 3 — critique" while waiting. Per-step replies populate
`meta.result` on completion.

## Output shape

The returned tool result is the per-step replies concatenated with brief
headers, so the calling agent retains the full audit trail in its
context:

```
[workflow step 1 · research · agent=agent · mode=executor]
<step 1 reply>

---

[workflow step 2 · critique · agent=butterfly_dev · mode=explorer]
<step 2 reply>
```

This is large by design — workflows are an audit-trail tool, not a
silent dispatcher. For workflows whose intermediate steps you don't want
in the parent context, prefer chaining `subagent_new` calls manually and
discarding the intermediates.

## Authoring

Add `workflow` to your agent's `tools.md`. No additional config — the
loader picks up the same `parent_session_id` / `sessions_base` /
`system_sessions_base` / `agent_base` already injected for `subagent_new`.

## Limitations (v1)

  * No DAG / parallel branches; steps are strictly sequential.
  * No conditional steps (`if prev contains [BLOCKED] then …`); the
    calling agent has to inspect the result and decide.
  * Killing the workflow via the panel marks it stopped between steps;
    the in-flight step continues until it returns. v2 can cascade-kill
    the in-flight `SubAgentTool` call directly.
