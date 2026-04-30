"""Team config — typed view over a `kind: team` agenthub config.yaml.

A team is a small group of agents that share a persistent ``teamchat`` and run
each in their own child session. The team itself is rendered in the UI like a
regular session whose timeline is the group chat; member sessions appear as
sub-session cards (panel entries) underneath. Per-member chat behaviour is
captured by ``teamchat_mode``:

  default  — interrupted by every teamchat message
  silent   — only interrupted when @-mentioned (by name or @all); otherwise
             receives an unread summary on the next natural wakeup

This module is pure config — no IO besides reading config.yaml. The runtime
team session class lives in ``team_session.py`` and the persistence helpers in
``teamchat.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_MODES = ("default", "silent")


@dataclass(frozen=True)
class MemberSpec:
    """One row of the team's member list."""
    name: str               # in-team handle used by `@<name>` mentions
    agent: str              # references agenthub/<agent>/ — same string the
                            # single-agent loader takes
    mode: str               # one of VALID_MODES


@dataclass(frozen=True)
class TeamSpec:
    """Parsed `kind: team` manifest.

    ``leader`` is the member that receives bare user input (no @-mention).
    Every team must declare a leader; loaders raise on missing/unknown leader
    so a typo doesn't silently route to nobody.
    """
    name: str
    description: str
    leader: str
    members: tuple[MemberSpec, ...]
    raw: dict[str, Any]   # original manifest, for any forward-compatible fields

    @property
    def member_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.members)

    def member(self, name: str) -> MemberSpec | None:
        for m in self.members:
            if m.name == name:
                return m
        return None


def is_team_manifest(manifest: dict[str, Any]) -> bool:
    """True when this manifest declares ``kind: team``.

    ``kind`` is optional on the single-agent path — absence means "agent".
    """
    return (manifest.get("kind") or "agent") == "team"


def parse_team_spec(name: str, manifest: dict[str, Any]) -> TeamSpec:
    """Validate + materialise a ``TeamSpec`` from a raw manifest dict.

    Raises ``ValueError`` on any of:
      * missing/empty ``members`` list
      * member rows lacking ``name`` or ``agent``
      * duplicate member names
      * unknown ``teamchat_mode``
      * missing ``leader`` or leader not present in ``members``
    """
    raw_members = manifest.get("members") or []
    if not isinstance(raw_members, list) or not raw_members:
        raise ValueError(
            f"team {name!r}: 'members' must be a non-empty list"
        )

    members: list[MemberSpec] = []
    seen: set[str] = set()
    for idx, row in enumerate(raw_members):
        if not isinstance(row, dict):
            raise ValueError(
                f"team {name!r}: members[{idx}] must be a mapping"
            )
        m_name = str(row.get("name") or "").strip()
        m_agent = str(row.get("agent") or "").strip()
        m_mode = str(row.get("teamchat_mode") or "default").strip()
        if not m_name:
            raise ValueError(f"team {name!r}: members[{idx}].name is required")
        if not m_agent:
            raise ValueError(f"team {name!r}: members[{idx}].agent is required")
        if m_name in seen:
            raise ValueError(
                f"team {name!r}: duplicate member name {m_name!r}"
            )
        if m_mode not in VALID_MODES:
            raise ValueError(
                f"team {name!r}: members[{idx}].teamchat_mode must be one of "
                f"{VALID_MODES}, got {m_mode!r}"
            )
        seen.add(m_name)
        members.append(MemberSpec(name=m_name, agent=m_agent, mode=m_mode))

    leader = str(manifest.get("leader") or "").strip()
    if not leader:
        raise ValueError(
            f"team {name!r}: 'leader' is required and must name one of the members"
        )
    if leader not in seen:
        raise ValueError(
            f"team {name!r}: leader {leader!r} not in members ({sorted(seen)})"
        )

    return TeamSpec(
        name=name,
        description=str(manifest.get("description") or "").strip(),
        leader=leader,
        members=tuple(members),
        raw=dict(manifest),
    )


def load_team_spec(team_dir: Path) -> TeamSpec:
    """Read ``<team_dir>/config.yaml`` and parse into a TeamSpec.

    ``team_dir`` is normally ``agenthub/<team_name>/``. Validates that the
    manifest actually declares ``kind: team`` — single-agent configs must
    not be loaded through this path.
    """
    from butterfly.session_engine.agent_config import AgentConfig

    config = AgentConfig.from_path(team_dir)
    manifest = config.manifest
    if not is_team_manifest(manifest):
        raise ValueError(
            f"{team_dir}: config.yaml is not a team (kind != 'team')"
        )
    return parse_team_spec(team_dir.name, manifest)
