# `nutshell/skill_engine`

This subsystem loads `SKILL.md` files from disk and renders the skill catalog that the agent sees in its system prompt.

## What This Part Is

- `loader.py`: loads directory skills (`skills/<name>/SKILL.md`) and legacy flat markdown skills.
- `renderer.py`: builds the prompt block that lists file-backed skills and inlines non-file-backed ones.

## How To Use It

```python
from pathlib import Path
from nutshell.skill_engine import SkillLoader, build_skills_block

skills = SkillLoader().load_dir(Path("core/skills"))
prompt_block = build_skills_block(skills)
```

In normal runtime usage, `Session` does this automatically before each activation.

## How It Contributes To The Whole System

- Skills are how entities and sessions provide reusable workflows without bloating the default prompt.
- File-backed skills use progressive disclosure: the prompt shows a catalog, and the agent loads the full body through the built-in `skill` tool only when needed.
- `core.Agent` depends on this subsystem to keep the prompt compact while still exposing a large capability surface.

## Important Behavior

- Frontmatter fields `name`, `description`, and `when_to_use` drive skill discovery.
- Inline skills are injected directly; file-backed skills are only catalogued until loaded.
- Skill directories can carry extra files alongside `SKILL.md`, and the `skill` tool exposes those paths when loading the skill.

