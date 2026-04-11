from __future__ import annotations

from typing import AsyncIterator
from pathlib import Path

from .sessions_service import _validate_session_id


def send_message(session_id: str, content: str, system_sessions_dir: Path, *, caller: str = "human") -> str:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    from nutshell.runtime.bridge import BridgeSession
    return BridgeSession(system_dir).send_message(content, caller=caller)


def interrupt_session(session_id: str, system_sessions_dir: Path) -> None:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    from nutshell.runtime.bridge import BridgeSession
    BridgeSession(system_dir).send_interrupt()


async def wait_for_reply(
    session_id: str,
    msg_id: str,
    system_sessions_dir: Path,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.5,
) -> str | None:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    from nutshell.runtime.bridge import BridgeSession
    return await BridgeSession(system_dir).async_wait_for_reply(
        msg_id,
        timeout=timeout,
        poll_interval=poll_interval,
    )


async def iter_events(
    session_id: str,
    system_sessions_dir: Path,
    *,
    context_offset: int = 0,
    events_offset: int = 0,
    poll_interval: float = 0.3,
) -> AsyncIterator[tuple[dict, int, int]]:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    from nutshell.runtime.bridge import BridgeSession
    async for event, ctx, evt in BridgeSession(system_dir).async_iter_events(
        context_offset=context_offset,
        events_offset=events_offset,
        poll_interval=poll_interval,
    ):
        yield event, ctx, evt


def build_ready_notifying_ipc(system_dir: Path, ready_event) -> object:
    from nutshell.runtime.ipc import FileIPC

    ipc = FileIPC(system_dir)
    original_context_size = ipc.context_size
    patched = False

    def _patched_context_size() -> int:
        nonlocal patched
        result = original_context_size()
        if not patched:
            patched = True
            ready_event.set()
        return result

    ipc.context_size = _patched_context_size  # type: ignore[method-assign]
    return ipc
