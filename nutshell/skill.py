from dataclasses import dataclass


@dataclass
class Skill:
    """A skill injects knowledge or behavior into an agent's system prompt."""
    name: str
    description: str
    prompt_injection: str
