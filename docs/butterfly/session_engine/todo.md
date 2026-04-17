# Session Engine — Todo

## Active / Known Issues

- [ ] `populate_meta_from_agent()` copies agent's `params` block but does not deep-merge with meta session defaults — fields present only in defaults may be lost on first bootstrap

## Completed

- [x] Meta session as agent instantiation unit (9472524)
- [x] Meta session child management tools (6a1c5c4)
- [x] Agent inheritance: link/own/append fields (b4fbc50) — **removed in v1.3.85**
- [x] Agent update proposal system (6bf957d) — **replaced by PR-based mecam/agent-update in v1.3.85**
- [x] Task card system replacing tasks.md
- [x] session_type three-state (ephemeral/default/persistent)
- [x] Layered memory single-direction flow (5d895fc)
- [x] Hook integration: on_loop_start, on_loop_end, on_tool_done (29f4996)
- [x] Replace agent inheritance with flat init_from copy model (v1.3.85)
- [x] Meta session version management + child session staleness notices (v1.3.85)

## Future

- [ ] Agent creation wizard improvements
- [ ] When agenthub/ is updated by user, meta session needs "update from agent" workflow (see agent_state.py TODO)
- [ ] Normal session optional "update agent core" capability (see agent_state.py TODO)
