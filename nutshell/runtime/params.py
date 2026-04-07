import json
from pathlib import Path

DEFAULT_PARAMS: dict = {
    "heartbeat_interval": 600.0,
    "model": None,          # None → use agent.yaml default
    "provider": None,       # None → use Anthropic
    "fallback_model": None,     # Optional fallback model if primary fails
    "fallback_provider": None,  # Optional fallback provider if primary fails
    "tool_providers": {"web_search": "brave"},  # web_search: "brave" | "tavily"
    "persistent": False,    # True → tick() fires even when tasks.md is empty
    "default_task": None,   # prompt used when persistent=True and tasks.md is empty
    "auto_model": False,    # True → auto-select model based on task complexity
    "thinking": False,      # True → enable extended thinking for this session
    "thinking_budget": 8000,  # budget_tokens for extended thinking (Anthropic/Kimi only)
    "thinking_effort": "high",  # reasoning effort level (Codex only): none/minimal/low/medium/high/xhigh
    "blocked_domains": [],
    "sandbox_max_fetch_chars": 50000,
    "blocked_domains": [],
    "sandbox_max_web_chars": 50000,
    "sandbox_max_web_chars": 50000,
}


def params_path(session_dir: Path) -> Path:
    return session_dir / "core" / "params.json"


def read_session_params(session_dir: Path) -> dict:
    p = params_path(session_dir)
    if not p.exists():
        return dict(DEFAULT_PARAMS)
    params = {**DEFAULT_PARAMS, **json.loads(p.read_text(encoding="utf-8"))}
    # Guard against zero/negative heartbeat_interval which would cause the timer to fire constantly
    interval = params.get("heartbeat_interval")
    if interval is not None and float(interval) < 1.0:
        params["heartbeat_interval"] = DEFAULT_PARAMS["heartbeat_interval"]
    return params


def write_session_params(session_dir: Path, **updates) -> None:
    current = read_session_params(session_dir)
    current.update(updates)
    p = params_path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def ensure_session_params(session_dir: Path, **defaults) -> None:
    """Create core/params.json if absent."""
    p = params_path(session_dir)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({**DEFAULT_PARAMS, **defaults}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
