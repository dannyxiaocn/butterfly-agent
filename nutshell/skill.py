from dataclasses import dataclass

from nutshell.base.skill import BaseSkill


@dataclass
class Skill(BaseSkill):
    """A skill injects knowledge or behavior into an agent's system prompt."""
    name: str
    description: str
    prompt_injection: str

    def to_prompt_fragment(self) -> str:
        return self.prompt_injection
