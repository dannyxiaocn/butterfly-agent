"""Unified tool registry — provider swap for web_search and other multi-provider tools.

    resolve_tool_impl(tool_name, provider_name) → Callable | None
    list_providers(tool_name) → list[str]
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

_TOOLHUB_DIR = Path(__file__).resolve().parent.parent.parent / "toolhub"


def _load_from_toolhub(tool_name: str, module_name: str, func_name: str) -> Callable | None:
    """Dynamically load a function from a toolhub module."""
    module_path = _TOOLHUB_DIR / tool_name / f"{module_name}.py"
    if not module_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            f"toolhub_{tool_name}_{module_name}", module_path
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, func_name, None)
    except Exception:
        return None


# ── Provider swap ─────────────────────────────────────────────────────────────

_PROVIDER_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "web_search": {
        "brave":  ("brave",  "_brave_search"),
        "tavily": ("tavily", "_tavily_search"),
    },
}


def resolve_tool_impl(tool_name: str, provider_name: str) -> Callable | None:
    """Return the async impl callable for (tool_name, provider_name), or None."""
    providers = _PROVIDER_REGISTRY.get(tool_name, {})
    entry = providers.get(provider_name)
    if not entry:
        return None
    module_name, func_name = entry
    return _load_from_toolhub(tool_name, module_name, func_name)


def list_providers(tool_name: str) -> list[str]:
    return list(_PROVIDER_REGISTRY.get(tool_name, {}).keys())


def registered_tools() -> list[str]:
    return list(_PROVIDER_REGISTRY.keys())
