# Skill Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `loader.py` | Loads directory skills (`skills/<name>/SKILL.md`) and legacy flat markdown skills |
| `renderer.py` | Builds the prompt block listing file-backed skills and inlining non-file-backed ones |

## Usage

```python
from pathlib import Path
from nutshell.skill_engine import SkillLoader, build_skills_block

skills = SkillLoader().load_dir(Path("core/skills"))
prompt_block = build_skills_block(skills)
```

In normal runtime, `Session` does this automatically before each activation.

## Important Behaviors

- Frontmatter fields `name`, `description`, `when_to_use` drive skill discovery
- Inline skills are injected directly into the prompt; file-backed skills are only catalogued until loaded
- Skill directories can carry extra files alongside `SKILL.md`; the `skill` tool exposes those paths when loading
