# Session Engine — Todo

## Active / Known Bugs

- [ ] **init_session() params inheritance bug**: reads entity's agent.yaml directly for model/provider — does NOT walk extends chain. Child entities without explicit model/provider get `null` in params.json
- [ ] **populate_meta_from_entity() params bug**: same issue — meta session params.json only reads current entity's YAML
- [ ] **own/link/append underutilized**: these inheritance fields are defined in agent.yaml but only used for memory/playground sync in entity_state.py. AgentLoader, init_session, populate_meta_from_entity all ignore them
- [ ] **skills inheritance in populate_meta**: only copies current entity's skills/ — does not resolve inherited skills from parent entities

## Completed

- [x] Meta session as entity instantiation unit (9472524)
- [x] Meta session child management tools (6a1c5c4)
- [x] Entity inheritance: link/own/append fields (b4fbc50)
- [x] Entity update proposal system (6bf957d)
- [x] Task card system replacing tasks.md
- [x] session_type three-state (ephemeral/default/persistent)
- [x] Layered memory single-direction flow (5d895fc)
- [x] Hook integration: on_loop_start, on_loop_end, on_tool_done (29f4996)

## Future

- [ ] Unify the two inheritance resolution paths (AgentLoader vs init_session)
- [ ] Entity creation wizard improvements
