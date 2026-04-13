# Entity — Todo

## Active

- [ ] Entity validation: check config.yaml schema on load (missing required fields, unknown keys)

## Completed

- [x] Entity catalog with README (bc2cc6f)
- [x] Entity inheritance: link/own/append (b4fbc50) — **removed in v1.3.85**
- [x] Entity scaffolding: nutshell entity new (new_agent.py)
- [x] Replace inheritance with flat init_from copy model (v1.3.85)
- [x] Entity versioning via agent_version in meta session params (v1.3.85)

## Future

- [ ] When user updates entity/, meta session needs "update from entity" workflow (merge entity changes with accumulated meta changes — see entity_state.py TODO)
- [ ] Normal session optional "update agent core" capability to promote session improvements to meta (see entity_state.py TODO)
