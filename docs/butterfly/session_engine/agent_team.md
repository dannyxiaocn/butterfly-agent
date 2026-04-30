# AgentTeam — design

An **AgentTeam** is a session whose conversation surface is a small group
chat backed by N agent members. From the user's point of view the team
session looks like a normal session in the sidebar; clicking in shows a
chronological feed where each cell is attributed to a member. Each member
also has its own child session card underneath the team — the same shape
the existing `subagent_new` UI already renders.

## Disk layout

```
sessions/<team_id>/
  core/
    config.yaml              ← (copied from agenthub/<team>/) kind=team manifest mirror
    teamchat.jsonl           ← canonical group-chat log (append-only)
    members.json             ← {"<member_name>": "<child_session_id>", …}
    panel/<bg_*>.json        ← one TYPE_SUB_AGENT card per member

_sessions/<team_id>/
  manifest.json              ← {kind:"team", leader, members[…], members_map}
  context.jsonl              ← user_input rows (real human + synthetic from members)
  events.jsonl               ← team-router status events

sessions/<member_session_id>/                           ← regular sub-agent session
_sessions/<member_session_id>/manifest.json
  { parent_session_id: <team_id>,
    member_of_team:    <team_id>,
    member_name:       "<name>",
    teamchat_mode:     "default" | "silent" }
_sessions/<member_session_id>/teamchat_cursor.json     ← {"seq": N}
```

`init_team_session()` creates the team session AND spawns one child
session per member at creation time (no lazy-spawn). Idempotent:
re-invoking with an existing team_id only fills in members that aren't
already in `members.json`.

## Routing

The team session has **no** Agent loop. `TeamSession.run_daemon_loop`
tails its own `context.jsonl` for `user_input` rows whose `caller` is
`"human"` (filtering out the synthetic rows the `teamchat_send` tool
writes when members post) and forwards each one to the matching member:

  * If the message body contains `@<member_name>` and the name matches a
    real member, that member receives the message.
  * Otherwise the team's `leader` receives it.

Forwarding goes through `BridgeSession(member_system_dir).send_message(...,
mode="interrupt")` — the recipient's existing daemon picks it up like
any other user input. Before delivery the router prepends any unread
teamchat summary the recipient hasn't yet acknowledged and bumps the
recipient's cursor to the latest seq.

## Teamchat semantics

Two tools, auto-injected into every member session whose manifest carries
`member_of_team`:

  * `teamchat_send(text)` — append to `teamchat.jsonl`, parse `@name` /
    `@all` from the text body, fan-out to other members per their mode,
    and mirror the post to the team session's UI feed (a synthetic
    `user_input` row attributed to the sender).

  * `teamchat_view()` — return all unread messages visible to the caller
    (visibility = "everyone except the sender") in chronological order
    and bump the caller's cursor.

Two modes only, configured per member in the team's `config.yaml`:

| Mode      | Receives every post              | Receives @-mention                      |
|-----------|----------------------------------|------------------------------------------|
| `default` | Live interrupt (always)          | Same                                     |
| `silent`  | No live interrupt; queued unread | Live interrupt (`@<self>` or `@all`)     |

The unread summary is the brief form: `[teamchat] Unread since HH:MM:SS:`
followed by one line per other sender (`• planner → 3 messages (1 @you)`).
The full bodies live in `teamchat.jsonl` and are fetched via
`teamchat_view`.

## What the team session shows the user

`sessions/<team_id>/_sessions/<team_id>/context.jsonl` — same format as a
regular session. Sources of rows:

  * The user's own typing → standard `user_input` rows with
    `caller="human"`.
  * Each `teamchat_send` from a member → synthetic `user_input` rows with
    `caller="<member_name>"`, `source="teamchat"`, `teamchat_seq=<seq>`.

The frontend's existing `tail_history` reads these directly; v1 UI
support relies entirely on the existing user-cell rendering with `caller`
as the attribution. Per-member child sessions surface as
`TYPE_SUB_AGENT` panel entries — the existing sub-agent card renders
them verbatim.

## Mention parsing

`teamchat.parse_mentions` walks the text body with `(?<![A-Za-z0-9_])@(\w+)`,
case-insensitively matches against the team's roster, and dedups.
`@all` is recognised as a special token. Anything that isn't a known
member or `all` (e.g. `user@example.com`) is dropped. There's no
explicit `mentions` field on the tool — keep the model's input shape
minimal and let the system infer.

## What v1 deliberately leaves out

  * **No router LLM.** The team session has no agent of its own; routing
    is purely string-based. A future revision can introduce a
    "moderator" agent that reads the chat and orchestrates non-trivial
    handoffs.
  * **No private DM / sub-channels.** Every post is broadcast and visible
    to everyone via `teamchat_view`. `mentions` only changes wakeup
    behaviour, not visibility.
  * **No concurrent member runs from a single user turn.** `@all` from
    the *user side* still routes to the leader because spawning N
    concurrent agent runs from one human turn opens a UX rabbit hole
    (interrupt fan-out, who-replies-to-the-user, …) that v1 punts on.
    `@all` from a *member* still fan-outs as expected via teamchat.
  * **No persistence of in-flight summaries.** If the daemon restarts
    while a member has unread, the cursor is stable on disk so the
    next wakeup still sees them.

## Authoring a team

```yaml
# agenthub/<team_name>/config.yaml
agent: my_team
kind: team
description: Two-person team.
leader: planner
members:
  - name: planner
    agent: agent              # references agenthub/agent
    teamchat_mode: default
  - name: coder
    agent: butterfly_dev
    teamchat_mode: silent
```

The two member agents (`agent`, `butterfly_dev`) live alongside the team
under `agenthub/`; they are loaded with the existing `AgentLoader.load()`
and run as ordinary sub-agent sessions. To make the teamchat tools
available add them to each member agent's `tools.md`:

```
teamchat_send
teamchat_view
```

The loader silently skips both tools when the session is not part of a
team, so the same `tools.md` works for solo and team modes.
