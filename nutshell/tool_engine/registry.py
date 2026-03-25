"""Unified tool registry — merges built-in callables + provider swap.

Built-in callables (was runtime/tools/_registry.py):
    get_builtin(name) → Callable | None

Provider swap (was runtime/tool_provider_factory.py):
    resolve_tool_impl(tool_name, provider_name) → Callable | None
    list_providers(tool_name) → list[str]
    registered_tools() → list[str]
"""
from __future__ import annotations

import importlib
from typing import Callable


# ── Built-in factories ────────────────────────────────────────────────────────

def _make_bash() -> Callable:
    from nutshell.tool_engine.executor.bash import create_bash_tool
    return create_bash_tool()._func


def _make_web_search() -> Callable:
    from nutshell.tool_engine.providers.web_search.brave import _brave_search
    return _brave_search


def _make_send_to_session() -> Callable:
    from nutshell.tool_engine.providers.session_msg import send_to_session
    return send_to_session


def _make_propose_entity_update() -> Callable:
    from nutshell.tool_engine.providers.entity_update import propose_entity_update
    return propose_entity_update


def _make_spawn_session() -> Callable:
    from nutshell.tool_engine.providers.spawn_session import spawn_session
    return spawn_session


def _make_fetch_url() -> Callable:
    from nutshell.tool_engine.providers.fetch_url import fetch_url
    return fetch_url


def _make_recall_memory() -> Callable:
    from nutshell.tool_engine.providers.recall_memory import recall_memory
    return recall_memory


def _make_state_diff() -> Callable:
    from nutshell.tool_engine.providers.state_diff import state_diff
    return state_diff


def _make_git_checkpoint() -> Callable:
    from nutshell.tool_engine.providers.git_checkpoint import git_checkpoint
    return git_checkpoint



def _make_app_notify() -> Callable:
    from nutshell.tool_engine.providers.app_notify import app_notify
    return app_notify

_BUILTIN_FACTORIES: dict[str, Callable[[], Callable]] = {
    "bash":                   _make_bash,
    "web_search":             _make_web_search,
    "send_to_session":        _make_send_to_session,
    "propose_entity_update":  _make_propose_entity_update,
    "spawn_session":          _make_spawn_session,
    "fetch_url":              _make_fetch_url,
    "recall_memory":          _make_recall_memory,
    "state_diff":             _make_state_diff,
    "git_checkpoint":         _make_git_checkpoint,
    "app_notify":             _make_app_notify,
}


def get_builtin(name: str) -> Callable | None:
    """Return the implementation callable for a built-in tool, or None."""
    factory = _BUILTIN_FACTORIES.get(name)
    return factory() if factory is not None else None


# ── Provider swap ─────────────────────────────────────────────────────────────

_PROVIDER_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "web_search": {
        "brave":  ("nutshell.tool_engine.providers.web_search.brave",  "_brave_search"),
        "tavily": ("nutshell.tool_engine.providers.web_search.tavily", "_tavily_search"),
    },
}


def resolve_tool_impl(tool_name: str, provider_name: str) -> Callable | None:
    """Return the async impl callable for (tool_name, provider_name), or None if unknown."""
    providers = _PROVIDER_REGISTRY.get(tool_name, {})
    entry = providers.get(provider_name)
    if not entry:
        return None
    module_path, func_name = entry
    try:
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError):
        return None


def list_providers(tool_name: str) -> list[str]:
    """Return list of registered provider names for a tool."""
    return list(_PROVIDER_REGISTRY.get(tool_name, {}).keys())


def registered_tools() -> list[str]:
    """Return all tool names that have registered providers."""
    return list(_PROVIDER_REGISTRY.keys())
