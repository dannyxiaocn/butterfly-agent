# Skill Engine — Todo

## Active (Module 9)

- [ ] Skill frontmatter: extend to Claude Code compatible subset (`allowed_tools`, `arguments`, `argument-hint`, `when_to_use`, `context`, `model`)
- [ ] Skill tool: access rights and context modification (tool allowlist, thinking/model override)
- [ ] Skill arguments: upgrade from `$ARGUMENTS` + simple positional to named params, defaults, escaping, error hints
- [ ] Skill resources: standardized discovery of `agents/`, `prompts/`, `references/` in skill dirs
- [ ] Skill prompt persistence: multi-turn, history compact, sub-agent/fork scenarios
- [ ] Skill sources: session / entity / user three-level with priority and dedup
- [ ] Conditional skill activation: path-pattern or workspace-context based
- [ ] Skill observability: load/use events to runtime stats
- [ ] Skill engine e2e tests: provider interaction simulation

## Completed

- [x] Progressive disclosure: catalog in prompt, body loaded on demand
- [x] Skills loading from entity directories
- [x] Memory layer 60-line truncation with bash hint (3c12fce)
- [x] Creator-mode skill for self-extending agents
