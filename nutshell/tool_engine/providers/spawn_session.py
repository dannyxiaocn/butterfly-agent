"""spawn_session — built-in tool for agents to create new sessions dynamically.

The new session is written to disk and picked up automatically by nutshell-server.
The spawning agent can then communicate with it via send_to_session.

Usage:
    spawn_session(entity="agent", initial_message="Analyse the dataset in docs/data.csv")
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


async def spawn_session(
    *,
    entity: str = "agent",
    initial_message: str = "",
    heartbeat: float = 600.0,
    _sessions_base: Path | None = None,
    _system_sessions_base: Path | None = None,
    _entity_base: Path | None = None,
) -> str:
    """Create a new agent session and return its session_id.

    The session is initialised with the given entity's prompts, tools, and skills.
    If nutshell-server is running, the new session will be picked up automatically.
    Use send_to_session to communicate with the spawned session.

    Args:
        entity:          Entity name to use (default: 'agent').
        initial_message: Optional first message written to the new session's context.
        heartbeat:       Heartbeat interval in seconds (default: 600).
    """
    from nutshell.runtime.session_factory import init_session

    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Avoid collision if the caller is also named with the current timestamp
    caller_id = os.environ.get("NUTSHELL_SESSION_ID", "")
    if session_id == caller_id:
        import time
        time.sleep(1)
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    try:
        init_session(
            session_id=session_id,
            entity_name=entity,
            sessions_base=_sessions_base,
            system_sessions_base=_system_sessions_base,
            entity_base=_entity_base,
            heartbeat=heartbeat,
            initial_message=initial_message or None,
        )
    except Exception as exc:
        return f"Error spawning session: {exc}"

    msg = f"Session '{session_id}' created (entity: {entity})."
    if initial_message:
        msg += f"\nInitial message written. Use send_to_session to communicate."
    else:
        msg += "\nNo initial message — use send_to_session or write to the session's tasks.md."
    return msg
