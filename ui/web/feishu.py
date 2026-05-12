"""Butterfly Feishu / Lark Bridge.

Bridges Feishu (Lark) bot conversations to a Butterfly session, mirroring
``ui/web/weixin.py`` so Feishu can run as an input alongside WeChat.

This is a butterfly-flavoured port of hermes-agent's
``gateway/platforms/feishu.py``: same Lark Open Platform identity model
(open_id / chat_id / message_id), same WebSocket long-connection transport
via the official ``lark_oapi`` SDK, same per-message admission gating
(group @mention required, P2P always admitted) and same persistent
``message_id``-based dedup. Everything else (cards, batching, media
upload, post-markdown rendering, drive comments) was dropped in favour of
parity with the WeChat bridge — Butterfly's chat surface is text-only
today and the additional surface area didn't earn its keep.

Credentials live in ``~/.butterfly/feishu/`` (mirrors the WeChat bridge's
``~/.openclaw/openclaw-weixin/`` layout):

  accounts.json                     — list of registered app_ids.
  accounts/<app_id>.json            — credentials + bot identity.
  accounts/<app_id>.seen.json       — persisted message_id dedup state.
  accounts/<app_id>.session.json    — last-active Butterfly session id.

Environment variables override ``accounts.json``:

  FEISHU_APP_ID, FEISHU_APP_SECRET  — credentials.
  FEISHU_DOMAIN                     — "feishu" (default) or "lark".
  FEISHU_BOT_OPEN_ID                — bot's own open_id (for @mention match).
  FEISHU_BOT_NAME                   — bot's display name (mention fallback).
  FEISHU_REQUIRE_MENTION            — "true" (default) / "false". Group only.

Supported commands (sent as message text, same set as WeChat):

  /new [agent]    — create a new session and make it active
  /stop           — stop current session
  /start          — resume current session
  /switch <id>    — switch active session
  /sessions       — list all sessions
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from butterfly.service import (
    send_message as service_send_message,
    start_session as service_start_session,
    stop_session as service_stop_session,
    wait_for_reply as service_wait_for_reply,
)

# Eagerly import session_init at module load — symmetric with weixin.py.
# A broken install must trip on ``import ui.web.feishu``, not later when a
# Feishu user happens to type ``/new``.
from butterfly.session_engine import session_init as _session_init  # noqa: F401

# ── Optional lark_oapi import ─────────────────────────────────────────────────
#
# Mirrors hermes-agent's degradation pattern: the module imports cleanly
# without lark_oapi installed; ``FeishuBridge.start()`` then fails-fast with
# ``status="unavailable"`` and a helpful pip-install hint instead of crashing
# the whole web server at import time.
try:
    import lark_oapi as lark  # type: ignore
    from lark_oapi.api.im.v1 import (  # type: ignore
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN  # type: ignore
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler  # type: ignore
    from lark_oapi.ws import Client as FeishuWSClient  # type: ignore

    FEISHU_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    lark = None  # type: ignore[assignment]
    CreateMessageRequest = None  # type: ignore[assignment]
    CreateMessageRequestBody = None  # type: ignore[assignment]
    ReplyMessageRequest = None  # type: ignore[assignment]
    ReplyMessageRequestBody = None  # type: ignore[assignment]
    FEISHU_DOMAIN = "https://open.feishu.cn"  # type: ignore[assignment]
    LARK_DOMAIN = "https://open.larksuite.com"  # type: ignore[assignment]
    EventDispatcherHandler = None  # type: ignore[assignment]
    FeishuWSClient = None  # type: ignore[assignment]
    FEISHU_AVAILABLE = False


_FEISHU_STATE_DIR = Path.home() / ".butterfly" / "feishu"
_FEISHU_ACCOUNTS_INDEX = _FEISHU_STATE_DIR / "accounts.json"
_FEISHU_ACCOUNTS_DIR = _FEISHU_STATE_DIR / "accounts"

_REPLY_TIMEOUT = 120.0
# message_id is globally unique in Feishu so a small-ish window is plenty.
# Rule of thumb: enough to absorb a WS reconnect storm without burning RAM.
_DEDUPE_WINDOW = 256
_FEISHU_SEND_ATTEMPTS = 3
# Bound on the inbound queue depth — beyond this the oldest event is dropped
# rather than blocking the SDK callback thread (which would back-pressure the
# entire WS connection).
_INBOUND_QUEUE_MAX = 1000


def _is_meta_session_id(session_id: str | None) -> bool:
    return bool(session_id) and str(session_id).endswith("_meta")


# ── Lark message content parsing ─────────────────────────────────────────────
#
# Feishu's ``message.content`` is a JSON string whose schema depends on
# ``msg_type``. We only need to recognise text/post for the inbound command
# router; everything else degrades to "" (treated as no-text → ignored).

def _parse_message_content(msg_type: str, raw_content: str) -> str:
    """Extract user-visible text from a Feishu message content payload."""
    if not raw_content:
        return ""
    try:
        payload = json.loads(raw_content)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if msg_type == "text":
        return str(payload.get("text") or "").strip()
    if msg_type == "post":
        # ``post`` is a localised rich-text wrapper: {locale: {title, content: [[seg, ...], ...]}}.
        # We flatten zh_cn → en_us (preferred order) into plain text.
        for locale in ("zh_cn", "en_us"):
            block = payload.get(locale)
            if not isinstance(block, dict):
                continue
            lines: list[str] = []
            for row in block.get("content") or []:
                if not isinstance(row, list):
                    continue
                pieces: list[str] = []
                for seg in row:
                    if isinstance(seg, dict) and seg.get("tag") == "text":
                        pieces.append(str(seg.get("text") or ""))
                if pieces:
                    lines.append("".join(pieces))
            text = "\n".join(lines).strip()
            if text:
                return text
    # Other types (image, file, audio, sticker, …) — no text.
    return ""


def _extract_mentions(mentions: Any) -> list[dict]:
    """Normalise the mentions array regardless of dict/SDK-object shape."""
    out: list[dict] = []
    for m in mentions or []:
        if isinstance(m, dict):
            mid = m.get("id") or {}
            out.append({
                "key": str(m.get("key") or ""),
                "open_id": str((mid.get("open_id") if isinstance(mid, dict) else "") or ""),
                "name": str(m.get("name") or ""),
            })
        else:
            mid = getattr(m, "id", None)
            out.append({
                "key": str(getattr(m, "key", "") or ""),
                "open_id": str(getattr(mid, "open_id", "") or ""),
                "name": str(getattr(m, "name", "") or ""),
            })
    return out


def _is_bot_mentioned(mentions: list[dict], bot_open_id: str, bot_name: str) -> bool:
    if not bot_open_id and not bot_name:
        return False
    for m in mentions:
        if bot_open_id and m["open_id"] == bot_open_id:
            return True
        if bot_name and m["name"] == bot_name:
            return True
    return False


def _strip_mention_tokens(text: str, mentions: list[dict]) -> str:
    """Remove ``@_user_N`` placeholders Feishu inlines for each mention.

    Feishu replaces every @-mention in the text with a placeholder of the
    form ``@_user_<key>`` (where key is a 1-based index into the mentions
    array). Strip them so command parsing sees ``/new`` not ``@_user_1 /new``.
    """
    if not text:
        return text
    out = text
    for m in mentions:
        token = f"@_user_{m['key']}" if m["key"] else ""
        if token and token in out:
            out = out.replace(token, "")
    return out.strip()


# ── Bridge ────────────────────────────────────────────────────────────────────


class FeishuBridge:
    """Async Feishu ↔ Butterfly bridge.

    Receives messages via the official Lark WebSocket long-connection (run on
    a dedicated thread, callbacks bounce back to the asyncio loop via
    ``run_coroutine_threadsafe``), routes them to the active Butterfly session
    via the service layer, waits for the agent reply, and replies to the user
    with ``im.v1.message.reply`` (which threads under the original message).
    """

    def __init__(self, sessions_dir: Path, system_sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._sys_dir = system_sessions_dir

        self._app_id: str | None = None
        self._app_secret: str | None = None
        self._domain_name: str = "feishu"
        self._bot_open_id: str = ""
        self._bot_name: str = ""
        self._require_mention: bool = True

        self._current_session: str | None = None
        # Per-chat session map (chat_id → session_id) so multiple users /
        # multiple groups don't bleed into the same session. Optional;
        # _current_session is the global fallback when no per-chat entry
        # exists, which keeps the single-DM-user flow weixin-shaped.
        self._chat_sessions: dict[str, str] = {}

        self._lock = asyncio.Lock()      # serialises send+wait_for_reply pairs
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Inbound queue: SDK callback (background thread) → drain coroutine
        # (asyncio loop). Bounded so a stuck loop can't unbounded-queue events
        # and OOM the worker.
        self._inbound: asyncio.Queue | None = None

        # WS client + thread handle (lark client runs its own event loop on a
        # dedicated thread; we tear it down via ``stop()``).
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._ws_thread_loop: asyncio.AbstractEventLoop | None = None

        # Persistent message_id dedup. Feishu message_ids are stable across
        # WS reconnects, unlike WeChat's per-delivery client_id, so we key
        # directly on message_id rather than a content fingerprint.
        self._seen_msgs: deque[str] = deque(maxlen=_DEDUPE_WINDOW)
        self._seen_set: set[str] = set()
        # In-memory cache of the bot's own outbound message_ids — short LRU so
        # we don't dispatch on bot self-echos that some Feishu deployments
        # surface as inbound events.
        self._own_msg_ids: deque[str] = deque(maxlen=_DEDUPE_WINDOW)
        self._own_msg_set: set[str] = set()

        self.status: str = "idle"
        self.error: str | None = None

    # ── Account loading ──────────────────────────────────────────────────────

    def load_account(self) -> bool:
        """Load Feishu credentials from env or ``accounts.json``.

        Env vars take precedence (so a one-off ``FEISHU_APP_ID=… python -m
        ui.web.app`` invocation works without writing a file). When no creds
        are configured we set ``status="no_account"`` and return False — the
        web server still starts; the bridge just stays dormant.
        """
        env_app_id = os.getenv("FEISHU_APP_ID", "").strip()
        env_app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if env_app_id and env_app_secret:
            self._app_id = env_app_id
            self._app_secret = env_app_secret
            self._domain_name = (os.getenv("FEISHU_DOMAIN", "feishu").strip().lower() or "feishu")
            self._bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
            self._bot_name = os.getenv("FEISHU_BOT_NAME", "").strip()
            self._require_mention = _to_bool(os.getenv("FEISHU_REQUIRE_MENTION", "true"))
        else:
            if not _FEISHU_ACCOUNTS_INDEX.exists():
                self.status = "no_account"
                self.error = (
                    "No Feishu accounts configured. Set FEISHU_APP_ID + "
                    "FEISHU_APP_SECRET in env, or write credentials to "
                    f"{_FEISHU_ACCOUNTS_INDEX}."
                )
                return False
            try:
                account_ids = json.loads(_FEISHU_ACCOUNTS_INDEX.read_text(encoding="utf-8"))
            except Exception:
                self.status = "error"
                self.error = "Failed to read accounts index"
                return False
            if not account_ids:
                self.status = "no_account"
                self.error = "No Feishu accounts registered"
                return False
            account_id = str(account_ids[0])
            account_file = _FEISHU_ACCOUNTS_DIR / f"{account_id}.json"
            if not account_file.exists():
                self.status = "no_account"
                self.error = f"Account file not found for {account_id}"
                return False
            try:
                data: dict = json.loads(account_file.read_text(encoding="utf-8"))
            except Exception as exc:
                self.status = "error"
                self.error = f"Failed to read account file: {exc}"
                return False
            self._app_id = str(data.get("app_id") or account_id).strip()
            self._app_secret = str(data.get("app_secret") or "").strip()
            self._domain_name = str(data.get("domain") or "feishu").strip().lower()
            self._bot_open_id = str(data.get("bot_open_id") or "").strip()
            self._bot_name = str(data.get("bot_name") or "").strip()
            if "require_mention" in data:
                self._require_mention = _to_bool(data.get("require_mention"))

        if not self._app_id or not self._app_secret:
            self.status = "no_account"
            self.error = "FEISHU_APP_ID / FEISHU_APP_SECRET are required"
            return False

        # Restore dedup state + per-chat session map.
        self._restore_seen_messages()
        self._restore_chat_sessions()
        if not self._current_session:
            self._current_session = self._most_recent_session()
        return True

    def _account_state_path(self, suffix: str) -> Path:
        if not self._app_id:
            raise RuntimeError("account not loaded")
        return _FEISHU_ACCOUNTS_DIR / f"{self._app_id}.{suffix}.json"

    def _restore_seen_messages(self) -> None:
        try:
            path = self._account_state_path("seen")
        except RuntimeError:
            return
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        ids = data.get("message_ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return
        # Keep the deque-bounded ordering and rebuild the set in lock-step.
        for mid in ids[-_DEDUPE_WINDOW:]:
            mid = str(mid)
            self._seen_msgs.append(mid)
        self._seen_set = set(self._seen_msgs)

    def _save_seen_messages(self) -> None:
        try:
            path = self._account_state_path("seen")
        except RuntimeError:
            return
        try:
            _FEISHU_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"message_ids": list(self._seen_msgs)}),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _restore_chat_sessions(self) -> None:
        try:
            path = self._account_state_path("session")
        except RuntimeError:
            return
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        cs = data.get("chat_sessions")
        if isinstance(cs, dict):
            self._chat_sessions = {str(k): str(v) for k, v in cs.items() if v}
        cur = data.get("current_session")
        if isinstance(cur, str) and cur:
            self._current_session = cur

    def _save_chat_sessions(self) -> None:
        try:
            path = self._account_state_path("session")
        except RuntimeError:
            return
        try:
            _FEISHU_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "current_session": self._current_session,
                    "chat_sessions": self._chat_sessions,
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _most_recent_session(self) -> str | None:
        if not self._sys_dir.exists():
            return None
        best: str | None = None
        best_ts = ""
        for d in self._sys_dir.iterdir():
            if not d.is_dir() or not (d / "manifest.json").exists():
                continue
            if _is_meta_session_id(d.name):
                continue
            try:
                st = json.loads((d / "status.json").read_text(encoding="utf-8"))
                ts = st.get("last_run_at", "")
                if ts > best_ts:
                    best_ts, best = ts, d.name
            except Exception:
                pass
        return best

    # ── Lark client lifecycle ────────────────────────────────────────────────

    def _build_lark_client(self) -> Any:
        domain = LARK_DOMAIN if self._domain_name == "lark" else FEISHU_DOMAIN
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def _build_event_handler(self) -> Any:
        return (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_lark_message)
            .build()
        )

    def _start_ws_client(self) -> None:
        domain = LARK_DOMAIN if self._domain_name == "lark" else FEISHU_DOMAIN
        self._ws_client = FeishuWSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self._build_event_handler(),
            domain=domain,
        )

        def _runner() -> None:
            # The official lark WS client wants an event loop on the running
            # thread. Mirror hermes-agent's wrapper: install a fresh loop,
            # patch the SDK's module-level reference, then start. Exceptions
            # are surfaced to the bridge state so /api/feishu/status reports
            # them — silent crashes here would freeze inbound forever.
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._ws_thread_loop = loop
                try:
                    import lark_oapi.ws.client as ws_client_module  # type: ignore
                    ws_client_module.loop = loop  # type: ignore[attr-defined]
                except Exception:
                    # Older SDKs expose ``loop`` differently — best-effort.
                    pass
                self._ws_client.start()
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                self.status = "error"
                self.error = f"WS thread crashed: {type(exc).__name__}: {exc}"
            finally:
                self._ws_thread_loop = None

        self._ws_thread = threading.Thread(
            target=_runner,
            name="butterfly-feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()

    def _stop_ws_client(self) -> None:
        # The lark SDK's WS client doesn't expose a graceful stop hook;
        # disabling auto-reconnect + relying on the daemon thread to exit
        # with the process is the same pattern hermes uses.
        try:
            if self._ws_client is not None:
                setattr(self._ws_client, "_auto_reconnect", False)
        except Exception:
            pass
        if self._ws_thread_loop is not None and not self._ws_thread_loop.is_closed():
            try:
                self._ws_thread_loop.call_soon_threadsafe(self._ws_thread_loop.stop)
            except Exception:
                pass
        self._ws_client = None
        self._ws_thread = None

    # ── Inbound: SDK callback → asyncio queue ────────────────────────────────

    def _on_lark_message(self, data: Any) -> None:
        """Lark SDK callback. Runs on the WS thread; must not block.

        We bounce the event onto the bridge's asyncio loop via a thread-safe
        queue put. If the queue is full (loop wedged) we drop the oldest
        rather than the newest, matching hermes's degradation bias.
        """
        loop = self._loop
        queue = self._inbound
        if loop is None or queue is None or loop.is_closed():
            return

        def _enqueue() -> None:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(data)

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            # Loop closed between the .is_closed() check and the call.
            return

    async def _drain_inbound(self) -> None:
        """Pull events off the queue, dispatch each to ``_process_event``."""
        assert self._inbound is not None
        while True:
            data = await self._inbound.get()
            task = asyncio.create_task(self._process_event(data))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    # ── Inbound: per-event processing ────────────────────────────────────────

    async def _process_event(self, data: Any) -> None:
        try:
            event = getattr(data, "event", None)
            if event is None:
                return
            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)
            if message is None or sender is None:
                return

            message_id = str(getattr(message, "message_id", "") or "")
            if not message_id:
                return

            # Skip our own bot-origin echoes (some tenants surface them).
            sender_type = str(getattr(sender, "sender_type", "") or "").lower()
            sender_id_obj = getattr(sender, "sender_id", None)
            sender_open_id = str(getattr(sender_id_obj, "open_id", "") or "")
            if sender_type in {"app", "bot"}:
                return
            if self._bot_open_id and sender_open_id == self._bot_open_id:
                return
            if message_id in self._own_msg_set:
                return

            if self._seen_or_record(message_id):
                return  # WS redelivery
            # Persisting on every accepted event is cheap (bounded write) and
            # keeps dedup honest across crashes mid-burst.
            self._save_seen_messages()

            chat_id = str(getattr(message, "chat_id", "") or "")
            chat_type = str(getattr(message, "chat_type", "p2p") or "p2p")
            msg_type = str(getattr(message, "message_type", "") or "")
            raw_content = getattr(message, "content", "") or ""
            mentions = _extract_mentions(getattr(message, "mentions", None))

            # Mention gate: in groups we only respond when @-mentioned (or
            # when require_mention is explicitly disabled). P2P bypass.
            if chat_type != "p2p" and self._require_mention:
                if not _is_bot_mentioned(mentions, self._bot_open_id, self._bot_name):
                    return

            text = _parse_message_content(msg_type, raw_content)
            text = _strip_mention_tokens(text, mentions).strip()
            if not text:
                return  # empty / sticker / unsupported type

            await self._process_message(
                chat_id=chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
                message_id=message_id,
                text=text,
            )
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"

    async def _process_message(
        self,
        *,
        chat_id: str,
        chat_type: str,
        sender_open_id: str,
        message_id: str,
        text: str,
    ) -> None:
        if text.startswith("/"):
            await self._handle_command(chat_id, chat_type, message_id, text)
            return

        session_id = self._chat_sessions.get(chat_id) or self._current_session
        if not session_id:
            await self._send_reply(
                chat_id, message_id,
                "⚠️ 没有活跃 session。\n发送 /new 创建，或 /sessions 查看已有 session。",
            )
            return
        if _is_meta_session_id(session_id):
            await self._send_reply(
                chat_id, message_id,
                "⚠️ 飞书不能直接和 meta session 对话。",
            )
            self._chat_sessions.pop(chat_id, None)
            self._current_session = None
            self._save_chat_sessions()
            return
        if not (self._sys_dir / session_id).exists():
            await self._send_reply(
                chat_id, message_id,
                f"⚠️ Session '{session_id}' 不存在，请发送 /new 创建。",
            )
            self._chat_sessions.pop(chat_id, None)
            if self._current_session == session_id:
                self._current_session = None
            self._save_chat_sessions()
            return

        async with self._lock:
            msg_id = service_send_message(session_id, text, self._sys_dir, caller="human")
            reply = await service_wait_for_reply(
                session_id, msg_id, self._sys_dir, timeout=_REPLY_TIMEOUT,
            )
        if reply:
            await self._send_reply(chat_id, message_id, reply)
        else:
            await self._send_reply(chat_id, message_id, "⚠️ Agent 未回复（超时）")

    # ── Command handling ─────────────────────────────────────────────────────

    async def _handle_command(
        self, chat_id: str, chat_type: str, message_id: str, text: str,
    ) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/new":
            agent = arg or "agenthub/agent"
            sid = (
                datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                + "-"
                + uuid.uuid4().hex[:4]
            )
            try:
                from butterfly.service.sessions_service import create_session as _create_session
                _create_session(
                    sid, agent,
                    sessions_dir=self._sessions_dir,
                    system_sessions_dir=self._sys_dir,
                )
                self._chat_sessions[chat_id] = sid
                self._current_session = sid
                self._save_chat_sessions()
                reply = f"✅ 新 session 已创建: {sid}\nAgent: {agent}"
            except Exception as exc:
                # Surface the full traceback to the web log — the failure
                # mode this guards is the same one weixin had: an opaque
                # "No module named …" reply with no class prefix.
                traceback.print_exc()
                reply = f"⚠️ 创建失败: {type(exc).__name__}: {exc}"

        elif cmd == "/stop":
            session_id = self._chat_sessions.get(chat_id) or self._current_session
            if not session_id:
                reply = "⚠️ 没有活跃 session"
            else:
                try:
                    if not service_stop_session(session_id, self._sys_dir):
                        reply = f"⚠️ Session '{session_id}' 不存在"
                    else:
                        reply = f"⏸ Session '{session_id}' 已暂停"
                except Exception as exc:
                    reply = f"⚠️ 错误: {exc}"

        elif cmd == "/start":
            session_id = self._chat_sessions.get(chat_id) or self._current_session
            if not session_id:
                reply = "⚠️ 没有活跃 session"
            else:
                try:
                    if not service_start_session(session_id, self._sys_dir):
                        reply = f"⚠️ Session '{session_id}' 不存在"
                    else:
                        reply = f"▶ Session '{session_id}' 已恢复"
                except Exception as exc:
                    reply = f"⚠️ 错误: {exc}"

        elif cmd == "/switch":
            if not arg:
                reply = "用法: /switch <session-id>"
            elif _is_meta_session_id(arg):
                reply = "⚠️ 飞书不能连接到 meta session"
            elif not (self._sys_dir / arg).exists():
                reply = f"⚠️ Session '{arg}' 不存在"
            else:
                self._chat_sessions[chat_id] = arg
                self._current_session = arg
                self._save_chat_sessions()
                reply = f"✅ 已切换到: {arg}"

        elif cmd == "/sessions":
            rows = self._list_sessions_summary()
            if not rows:
                reply = "没有 session"
            else:
                active = self._chat_sessions.get(chat_id) or self._current_session
                lines = []
                for s in rows:
                    marker = "▶" if s["id"] == active else " "
                    lines.append(f"{marker} {s['id']} [{s['status']}]")
                reply = "\n".join(lines)

        else:
            reply = (
                f"未知命令: {cmd}\n"
                "可用命令:\n"
                "  /new [agent]    — 新建 session\n"
                "  /stop           — 暂停当前 session\n"
                "  /start          — 恢复当前 session\n"
                "  /switch <id>    — 切换 session\n"
                "  /sessions       — 列出所有 session"
            )

        await self._send_reply(chat_id, message_id, reply)

    def _list_sessions_summary(self) -> list[dict]:
        if not self._sys_dir.exists():
            return []
        result = []
        for d in self._sys_dir.iterdir():
            if not d.is_dir() or not (d / "manifest.json").exists():
                continue
            if _is_meta_session_id(d.name):
                continue
            try:
                st = json.loads((d / "status.json").read_text(encoding="utf-8"))
                status = st.get("status", "active")
            except Exception:
                status = "?"
            result.append({"id": d.name, "status": status})
        return sorted(result, key=lambda s: s["id"])

    # ── Outbound: send via lark SDK ──────────────────────────────────────────

    async def _send_reply(self, chat_id: str, reply_to_message_id: str, text: str) -> None:
        """Reply to a Feishu message; falls back to a fresh chat send on failure.

        ``im.v1.message.reply`` threads under the user's original message
        which is the conversational behaviour we want. When Feishu returns a
        ``reply target withdrawn`` error (codes 230011 / 231003) we degrade
        to a plain ``message.create`` so the user still sees the response.
        """
        if not self._client:
            return
        payload = json.dumps({"text": text}, ensure_ascii=False)
        last_exc: Exception | None = None
        for attempt in range(_FEISHU_SEND_ATTEMPTS):
            try:
                response = await self._send_reply_once(reply_to_message_id, payload)
                if _response_succeeded(response):
                    self._record_own_message_id(response)
                    return
                code = getattr(response, "code", 0)
                if code in (230011, 231003):
                    # Reply target gone — fall through to create.
                    break
                # Transient: retry with backoff.
                await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (2 ** attempt))

        # Fallback: send fresh in the chat.
        try:
            response = await self._send_create_once(chat_id, payload)
            if _response_succeeded(response):
                self._record_own_message_id(response)
                return
        except Exception as exc:
            last_exc = exc
        if last_exc is not None:
            traceback.print_exc()

    async def _send_reply_once(self, message_id: str, payload: str) -> Any:
        body = (
            ReplyMessageRequestBody.builder()
            .content(payload)
            .msg_type("text")
            .reply_in_thread(False)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        return await asyncio.to_thread(self._client.im.v1.message.reply, request)

    async def _send_create_once(self, chat_id: str, payload: str) -> Any:
        # Detect open_id (DM via sender open_id) vs chat_id (group). Feishu
        # returns chat_id for both DMs and groups; we keep the heuristic
        # narrow — open_id always starts with ``ou_`` — and fall back to
        # ``chat_id`` for everything else, which is what the SDK expects for
        # group sends.
        receive_id_type = "open_id" if chat_id.startswith("ou_") else "chat_id"
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(payload)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        return await asyncio.to_thread(self._client.im.v1.message.create, request)

    def _record_own_message_id(self, response: Any) -> None:
        try:
            data = getattr(response, "data", None)
            mid = getattr(data, "message_id", None)
        except Exception:
            mid = None
        if not mid:
            return
        if mid in self._own_msg_set:
            return
        self._own_msg_ids.append(mid)
        self._own_msg_set.add(mid)
        # Trim the set to the deque (deque auto-evicts oldest).
        while len(self._own_msg_set) > len(self._own_msg_ids):
            self._own_msg_set = set(self._own_msg_ids)
            break

    # ── Dedup helpers ────────────────────────────────────────────────────────
    #
    # Mirrors the WeChat bridge's ``_seen_or_record`` shape so the cross-
    # bridge tests can share assertions: deque + set staying in sync past
    # the maxlen boundary is the invariant both bridges depend on.

    def _msg_fingerprint(self, msg: Any) -> str:
        """Stable key for a Feishu message.

        Prefers ``message_id`` (Feishu's globally-unique key); when absent
        falls back to a sender + text + 5-second time-bucket hash so the
        function can still gate a malformed payload without crashing.
        """
        if isinstance(msg, dict):
            mid = str(msg.get("message_id") or "")
            sender = str(msg.get("sender_open_id") or msg.get("sender_id") or "")
            text = str(msg.get("text") or "")
        else:
            mid = str(getattr(msg, "message_id", "") or "")
            sender = str(getattr(msg, "sender_open_id", "") or "")
            text = str(getattr(msg, "text", "") or "")
        if mid:
            return mid
        bucket = int(time.time()) // 5
        h = hashlib.sha1()
        h.update(f"{sender}|{text}|{bucket}".encode("utf-8", "replace"))
        return h.hexdigest()

    def _seen_or_record(self, fp: str) -> bool:
        if fp in self._seen_set:
            return True
        self._seen_msgs.append(fp)
        self._seen_set.add(fp)
        # Resync the set with the bounded deque (deque auto-evicts oldest).
        while len(self._seen_set) > len(self._seen_msgs):
            self._seen_set = set(self._seen_msgs)
            break
        return False

    # ── Main loop / lifecycle ────────────────────────────────────────────────

    async def _run(self) -> None:
        """Build the Lark client, spawn the WS thread, drain inbound forever.

        ``status`` stays at ``"starting"`` until every setup step succeeds and
        we're about to enter the drain loop, then flips to ``"running"``.
        Setting it optimistically in :py:meth:`start` opens a window where
        ``/api/feishu/status`` reports ``running`` for a tick before the
        Lark-client build or WS-thread spawn fails and we'd flip to
        ``"error"`` / ``"unavailable"``.
        """
        if not FEISHU_AVAILABLE:
            self.status = "unavailable"
            self.error = (
                "lark_oapi not installed; run `pip install lark-oapi` to "
                "enable the Feishu bridge."
            )
            return

        self._loop = asyncio.get_running_loop()
        self._inbound = asyncio.Queue(maxsize=_INBOUND_QUEUE_MAX)

        try:
            self._client = self._build_lark_client()
        except Exception as exc:
            self.status = "error"
            self.error = f"Failed to build lark client: {type(exc).__name__}: {exc}"
            return

        try:
            self._start_ws_client()
        except Exception as exc:
            self.status = "error"
            self.error = f"Failed to start WS client: {type(exc).__name__}: {exc}"
            return

        # Setup complete — promote to "running" now, not in start(), so the
        # status endpoint can't catch us mid-setup with a stale "running".
        self.status = "running"
        self.error = None

        try:
            await self._drain_inbound()
        except asyncio.CancelledError:
            for t in self._pending:
                t.cancel()
            self._stop_ws_client()
            self.status = "stopped"
            return
        except Exception as exc:  # pragma: no cover
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._stop_ws_client()
            return

    def start(self) -> None:
        if not self.load_account():
            return  # status/error already set
        if not FEISHU_AVAILABLE:
            self.status = "unavailable"
            self.error = (
                "lark_oapi not installed; run `pip install lark-oapi` to "
                "enable the Feishu bridge."
            )
            return
        # Stay at "starting" until ``_run`` has built the client + spawned
        # the WS thread; ``_run`` flips us to "running" once drain begins.
        self.status = "starting"
        self.error = None
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._stop_ws_client()
        self.status = "stopped"


# ── Module-private helpers ────────────────────────────────────────────────────


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _response_succeeded(response: Any) -> bool:
    if response is None:
        return False
    success = getattr(response, "success", None)
    if callable(success):
        try:
            return bool(success())
        except Exception:
            return False
    code = getattr(response, "code", None)
    return code == 0
