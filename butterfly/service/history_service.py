from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .sessions_service import _validate_session_id


def get_history(session_id: str, system_sessions_dir: Path, context_since: int = 0) -> dict:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    from butterfly.runtime.ipc import FileIPC
    from butterfly.session_engine.session_status import read_session_status

    ipc = FileIPC(system_dir)
    events: list[dict] = []
    context_offset = context_since
    for event, off in ipc.tail_history(context_since):
        events.append(event)
        context_offset = off

    events_offset = ipc.events_size()
    status_payload = read_session_status(system_dir)
    if status_payload.get("model_state") == "running":
        events_offset = ipc.last_running_event_offset()

    return {"events": events, "context_offset": context_offset, "events_offset": events_offset}


def _parse_since(value: str) -> float:
    if value == 'now':
        import time
        return time.time()
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        ts = float(value)
        if ts > 1_000_000_000:
            return ts
    except (ValueError, TypeError):
        pass
    raise ValueError(f"Cannot parse --since value: {value!r}. Use 'now', an ISO-8601 datetime, or a UNIX timestamp.")


def _turn_ts(turn: dict) -> float | None:
    raw = turn.get('ts')
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    parts.append(f"[tool: {block.get('name', '?')}({json.dumps(block.get('input', {}), ensure_ascii=False)})]")
                elif block.get('type') == 'tool_result':
                    inner = block.get('content', '')
                    preview = (inner[:80] + '…') if isinstance(inner, str) and len(inner) > 80 else inner
                    parts.append(f"[result: {preview}]")
        return ' '.join(p for p in parts if p)
    return str(content)


def _load_context(context_path: Path) -> tuple[dict, list]:
    lines = [l for l in context_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    inputs_by_id: dict[str, dict] = {}
    turns: list[dict] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get('type') == 'user_input':
            if ev.get('id'):
                inputs_by_id[ev['id']] = ev
        elif ev.get('type') == 'turn':
            turns.append(ev)
    return inputs_by_id, turns


def get_log_turns(session_id: str, system_sessions_dir: Path, n=None, since=None) -> list[dict]:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    context_path = system_dir / 'context.jsonl'
    if not (system_dir / 'manifest.json').exists():
        raise FileNotFoundError(session_id)
    if not context_path.exists():
        return []
    since_ts = _parse_since(since) if since is not None else None
    inputs_by_id, turns = _load_context(context_path)
    if since_ts is not None:
        turns = [t for t in turns if (_turn_ts(t) or 0) > since_ts]
    elif n is not None and n > 0:
        turns = turns[-n:]
    rows = []
    for turn in turns:
        uid = turn.get('user_input_id')
        user_ev = inputs_by_id.get(uid) if uid else None
        ts = (user_ev or turn).get('ts', '')[:16].replace('T', ' ')
        agent_lines = []
        for msg in turn.get('messages', []):
            if msg.get('role') == 'assistant':
                text = _flatten_content(msg.get('content', ''))
                if text:
                    agent_lines.append(text)
        rows.append({
            'ts': ts,
            'user': user_ev.get('content', '') if user_ev else '',
            'agent': agent_lines,
            'usage': turn.get('usage') or {},
            'turn': turn,
        })
    return rows


def get_pending_inputs(session_id: str, system_sessions_dir: Path, n=None) -> list[dict]:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    context_path = system_dir / 'context.jsonl'
    if not (system_dir / 'manifest.json').exists():
        raise FileNotFoundError(session_id)
    if not context_path.exists():
        return []
    inputs_by_id, turns = _load_context(context_path)
    matched_inputs = {turn.get('user_input_id') for turn in turns if turn.get('user_input_id')}
    pending = [ev for ev in inputs_by_id.values() if ev.get('id') not in matched_inputs]
    if n is not None:
        pending = pending[-n:]
    return [{
        'ts': ev.get('ts', '')[:16].replace('T', ' '),
        'user': ev.get('content', ''),
    } for ev in pending]


def get_token_report(session_id: str, system_sessions_dir: Path) -> list[dict]:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    context_path = system_dir / 'context.jsonl'
    if not (system_dir / 'manifest.json').exists():
        raise FileNotFoundError(session_id)
    if not context_path.exists():
        return []
    inputs_by_id, turns = _load_context(context_path)
    rows = []
    for i, turn in enumerate(turns, 1):
        usage = turn.get('usage') or {}
        uid = turn.get('user_input_id')
        user_ev = inputs_by_id.get(uid) if uid else None
        raw = user_ev.get('content', '') if user_ev else ('[task]' if turn.get('pre_triggered') else '')
        trigger = (raw[:40] + '…') if len(raw) > 40 else raw
        rows.append({
            'index': i,
            'ts': (user_ev or turn).get('ts', '')[:16].replace('T', ' '),
            'trigger': trigger,
            'input': usage.get('input', 0) or 0,
            'output': usage.get('output', 0) or 0,
            'cache_read': usage.get('cache_read', 0) or 0,
            'cache_write': usage.get('cache_write', 0) or 0,
        })
    return rows


