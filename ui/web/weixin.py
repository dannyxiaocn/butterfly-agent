"""Butterfly WeChat Bridge.

Bridges WeChat (via ilink bot API) to a Butterfly session.
Reads token from ~/.openclaw/openclaw-weixin/accounts/ (requires OpenClaw QR login).
Runs as an asyncio background task alongside the FastAPI web server.

Supported WeChat commands (send as message text):
  /new [agent]    — create a new session and make it active
  /stop           — stop current session
  /start          — resume current session
  /switch <id>    — switch active session
  /sessions       — list all sessions
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from butterfly.service import send_message as service_send_message, start_session as service_start_session, stop_session as service_stop_session, wait_for_reply as service_wait_for_reply

_WEIXIN_STATE_DIR = Path.home() / ".openclaw" / "openclaw-weixin"
_WEIXIN_ACCOUNTS_INDEX = _WEIXIN_STATE_DIR / "accounts.json"
_WEIXIN_ACCOUNTS_DIR = _WEIXIN_STATE_DIR / "accounts"

_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
_CHANNEL_VERSION = "1.0.3"
_LONG_POLL_TIMEOUT = 38.0   # slightly above server's 35-second timeout
_REPLY_TIMEOUT = 120.0       # max seconds to wait for agent reply


def _is_meta_session_id(session_id: str | None) -> bool:
    return bool(session_id) and str(session_id).endswith("_meta")


def _wechat_uin() -> str:
    """Base64-encoded random uint32 for X-WECHAT-UIN header."""
    return base64.b64encode(struct.pack(">I", int.from_bytes(os.urandom(4), "big"))).decode()


def _api_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _wechat_uin(),
    }


class WeixinBridge:
    """Async WeChat ↔ Butterfly bridge.

    Polls WeChat for incoming messages, routes them to the active Butterfly session via
    FileIPC, waits for the agent reply, and sends it back to the WeChat user.
    """

    def __init__(self, sessions_dir: Path, system_sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._sys_dir = system_sessions_dir
        self._account_id: str | None = None
        self._token: str | None = None
        self._base_url: str = _DEFAULT_BASE_URL
        self._get_updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}   # from_user_id → context_token
        self._current_session: str | None = None
        self._lock = asyncio.Lock()                 # serialises send+wait pairs
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self.status: str = "idle"
        self.error: str | None = None

    # ── Account loading ──────────────────────────────────────────────────────

    def load_account(self) -> bool:
        """Load WeChat token from OpenClaw state files. Returns True if configured."""
        if not _WEIXIN_ACCOUNTS_INDEX.exists():
            self.status = "no_account"
            self.error = "No WeChat accounts found. Run: openclaw channels login --channel openclaw-weixin"
            return False
        try:
            account_ids: list[str] = json.loads(_WEIXIN_ACCOUNTS_INDEX.read_text())
        except Exception:
            self.status = "error"
            self.error = "Failed to read accounts index"
            return False
        if not account_ids:
            self.status = "no_account"
            self.error = "No WeChat accounts registered"
            return False

        account_id = account_ids[0]
        account_file = _WEIXIN_ACCOUNTS_DIR / f"{account_id}.json"
        if not account_file.exists():
            self.status = "no_account"
            self.error = f"Account file not found for {account_id}"
            return False
        try:
            data: dict = json.loads(account_file.read_text())
        except Exception as exc:
            self.status = "error"
            self.error = f"Failed to read account file: {exc}"
            return False

        token = (data.get("token") or "").strip()
        if not token:
            self.status = "no_account"
            self.error = "No token in account file. Run QR login again."
            return False

        self._account_id = account_id
        self._token = token
        self._base_url = (data.get("baseUrl") or "").strip() or _DEFAULT_BASE_URL

        # Restore sync cursor
        sync_file = _WEIXIN_ACCOUNTS_DIR / f"{account_id}.sync.json"
        if sync_file.exists():
            try:
                self._get_updates_buf = json.loads(sync_file.read_text()).get("get_updates_buf", "")
            except Exception:
                pass

        # Restore per-user context tokens
        tokens_file = _WEIXIN_ACCOUNTS_DIR / f"{account_id}.context-tokens.json"
        if tokens_file.exists():
            try:
                loaded = json.loads(tokens_file.read_text())
                if isinstance(loaded, dict):
                    self._context_tokens = loaded
            except Exception:
                pass

        # Auto-select most recently active session
        self._current_session = self._most_recent_session()
        return True

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
                st = json.loads((d / "status.json").read_text())
                ts = st.get("last_run_at", "")
                if ts > best_ts:
                    best_ts, best = ts, d.name
            except Exception:
                pass
        return best

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save_sync_cursor(self) -> None:
        if not self._account_id:
            return
        _WEIXIN_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        (_WEIXIN_ACCOUNTS_DIR / f"{self._account_id}.sync.json").write_text(
            json.dumps({"get_updates_buf": self._get_updates_buf})
        )

    def _save_context_tokens(self) -> None:
        if not self._account_id:
            return
        _WEIXIN_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        (_WEIXIN_ACCOUNTS_DIR / f"{self._account_id}.context-tokens.json").write_text(
            json.dumps(self._context_tokens)
        )

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    async def _get_updates(self, client: httpx.AsyncClient) -> dict:
        resp = await client.post(
            f"{self._base_url}/ilink/bot/getupdates",
            json={
                "get_updates_buf": self._get_updates_buf,
                "base_info": {"channel_version": _CHANNEL_VERSION},
            },
            headers=_api_headers(self._token),
            timeout=_LONG_POLL_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    async def _send_text(
        self,
        client: httpx.AsyncClient,
        to_user: str,
        text: str,
        context_token: str | None = None,
    ) -> None:
        msg: dict = {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": f"butterfly-{int(time.time() * 1000)}",
            "message_type": 2,   # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        await client.post(
            f"{self._base_url}/ilink/bot/sendmessage",
            json={"msg": msg, "base_info": {"channel_version": _CHANNEL_VERSION}},
            headers=_api_headers(self._token),
            timeout=10.0,
        )

    # ── Agent reply waiting ───────────────────────────────────────────────────
    # Delegated to BridgeSession.async_wait_for_reply() which uses
    # user_input_id matching (more reliable than watching for 'agent' event
    # type, which could be from a concurrent task turn).

    # ── Command handling ──────────────────────────────────────────────────────

    async def _handle_command(
        self,
        client: httpx.AsyncClient,
        from_user: str,
        text: str,
        ctx_token: str | None,
    ) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/new":
            agent = arg or "agenthub/agent"
            sid = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + uuid.uuid4().hex[:4]
            try:
                from butterfly.service.sessions_service import create_session as _create_session
                _create_session(sid, agent, sessions_dir=self._sessions_dir, system_sessions_dir=self._sys_dir)
                self._current_session = sid
                reply = f"✅ 新 session 已创建: {sid}\nAgent: {agent}"
            except Exception as exc:
                reply = f"⚠️ 创建失败: {exc}"

        elif cmd == "/stop":
            if not self._current_session:
                reply = "⚠️ 没有活跃 session"
            else:
                try:
                    if not service_stop_session(self._current_session, self._sys_dir):
                        reply = f"⚠️ Session '{self._current_session}' 不存在"
                    else:
                        reply = f"⏸ Session '{self._current_session}' 已暂停"
                except Exception as exc:
                    reply = f"⚠️ 错误: {exc}"

        elif cmd == "/start":
            if not self._current_session:
                reply = "⚠️ 没有活跃 session"
            else:
                try:
                    if not service_start_session(self._current_session, self._sys_dir):
                        reply = f"⚠️ Session '{self._current_session}' 不存在"
                    else:
                        reply = f"▶ Session '{self._current_session}' 已恢复"
                except Exception as exc:
                    reply = f"⚠️ 错误: {exc}"

        elif cmd == "/switch":
            if not arg:
                reply = "用法: /switch <session-id>"
            elif _is_meta_session_id(arg):
                reply = "⚠️ 微信不能连接到 meta session"
            elif not (self._sys_dir / arg).exists():
                reply = f"⚠️ Session '{arg}' 不存在"
            else:
                self._current_session = arg
                reply = f"✅ 已切换到: {arg}"

        elif cmd == "/sessions":
            rows = self._list_sessions_summary()
            if not rows:
                reply = "没有 session"
            else:
                lines = []
                for s in rows:
                    marker = "▶" if s["id"] == self._current_session else " "
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

        await self._send_text(client, from_user, reply, ctx_token)

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
                st = json.loads((d / "status.json").read_text())
                status = st.get("status", "active")
            except Exception:
                status = "?"
            result.append({"id": d.name, "status": status})
        return sorted(result, key=lambda s: s["id"])

    # ── Message processing ────────────────────────────────────────────────────

    async def _process_message(self, client: httpx.AsyncClient, msg: dict) -> None:
        from_user = msg.get("from_user_id", "")
        ctx_token = msg.get("context_token")

        if ctx_token:
            self._context_tokens[from_user] = ctx_token
            self._save_context_tokens()

        # Extract text from item_list
        text = ""
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:  # TEXT
                text = ((item.get("text_item") or {}).get("text") or "").strip()
                break
        if not text:
            return

        if text.startswith("/"):
            await self._handle_command(client, from_user, text, ctx_token)
            return

        if not self._current_session:
            await self._send_text(
                client, from_user,
                "⚠️ 没有活跃 session。\n发送 /new 创建，或 /sessions 查看已有 session。",
                ctx_token,
            )
            return
        if _is_meta_session_id(self._current_session):
            await self._send_text(
                client, from_user,
                "⚠️ 微信不能直接和 meta session 对话。",
                ctx_token,
            )
            self._current_session = None
            return

        sys_dir = self._sys_dir / self._current_session
        if not sys_dir.exists():
            await self._send_text(
                client, from_user,
                f"⚠️ Session '{self._current_session}' 不存在，请发送 /new 创建。",
                ctx_token,
            )
            self._current_session = None
            return

        async with self._lock:
            msg_id = service_send_message(self._current_session, text, self._sys_dir, caller="human")
            reply = await service_wait_for_reply(self._current_session, msg_id, self._sys_dir, timeout=_REPLY_TIMEOUT)

        if reply:
            await self._send_text(client, from_user, reply, ctx_token)
        else:
            await self._send_text(client, from_user, "⚠️ Agent 未回复（超时）", ctx_token)

    # ── Main polling loop ────────────────────────────────────────────────────

    async def _run(self) -> None:
        async with httpx.AsyncClient(trust_env=False) as client:
            while True:
                try:
                    data = await self._get_updates(client)
                    buf = data.get("get_updates_buf")
                    if buf:
                        self._get_updates_buf = buf
                        self._save_sync_cursor()
                    for msg in data.get("msgs") or []:
                        if msg.get("message_type") == 2:  # skip bot's own messages
                            continue
                        task = asyncio.create_task(self._process_message(client, msg))
                        self._pending.add(task)
                        task.add_done_callback(self._pending.discard)
                except asyncio.CancelledError:
                    for t in self._pending:
                        t.cancel()
                    self.status = "stopped"
                    return
                except Exception as exc:
                    self.status = "error"
                    self.error = str(exc)
                    await asyncio.sleep(5)
                    self.status = "running"
                    self.error = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.load_account():
            return  # status/error already set
        self.status = "running"
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.status = "stopped"
