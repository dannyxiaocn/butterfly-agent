"""Thin compatibility shim over nutshell.service.sessions_service."""
from nutshell.service.sessions_service import _is_stale_stopped, is_meta_session as _is_meta_session_id, sort_sessions as _sort_sessions
from nutshell.service.sessions_service import get_session as _service_get_session, create_session as _service_create_session


def _read_session_info(session_dir, system_dir):
    return _service_get_session(system_dir.name, session_dir.parent, system_dir.parent)


def _init_session(sessions_dir, system_sessions_dir, session_id, entity, heartbeat=None):
    return _service_create_session(session_id, entity, sessions_dir=sessions_dir, system_sessions_dir=system_sessions_dir)
