# Agent — Todo

## Active

- [ ] Agent validation: check config.yaml schema on load (missing required fields, unknown keys)

## Completed

- [x] Agent catalog with README (bc2cc6f)
- [x] Agent inheritance: link/own/append (b4fbc50) — **removed in v1.3.85**
- [x] Agent scaffolding: butterfly agent new (new_agent.py)
- [x] Replace inheritance with flat init_from copy model (v1.3.85)
- [x] Agent versioning via agent_version in meta session params (v1.3.85)

## Future

- [ ] When user updates agenthub/, meta session needs "update from agent" workflow (merge agent changes with accumulated meta changes — see agent_state.py TODO)
- [ ] Normal session optional "update agent core" capability to promote session improvements to meta (see agent_state.py TODO)
