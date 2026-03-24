# Spec: CLI-ification + Multi-agent Session Tool

Date: 2026-03-24
Status: Approved (v2 — post spec-review fixes)

---

## 1. `nutshell-chat` — CLI 化聊天

### 命令接口

```bash
# 续聊已有 session（需要 nutshell-server 在运行）
nutshell-chat --session <session_id> "你的消息"

# 新建 session 并对话（自带 daemon，无需 server）
nutshell-chat "你的消息"
nutshell-chat --entity <entity_name> "你的消息"   # 默认 entity: agent

# 只发，不等（fire-and-forget）
nutshell-chat --session <session_id> --no-wait "你的消息"
nutshell-chat --no-wait "你的消息"   # 新建 session + 不等，仍输出 Session: <id>

# 超时控制
nutshell-chat --session <id> --timeout 60 "消息"
```

### 输出格式

续聊：
```
<agent 响应文本>
```

新建 session（最后一行固定格式，方便 shell 脚本 `grep`）：
```
<agent 响应文本>

Session: <session_id>
```

`--no-wait` 时（新建 session）：
```
Session: <session_id>
```

`--no-wait` 时（续聊）：无输出，exit(0)。

### 实现：`ui/cli/chat.py`

注册到 `pyproject.toml`：`nutshell-chat = "ui.cli.chat:main"`

#### 续聊流程（需要 server 在运行）

```
1. 验证 _sessions/<id>/manifest.json 存在
2. 读 _sessions/<id>/status.json，确认 status != "stopped"
3. 记录 context.jsonl 当前字节偏移 (offset_before)
4. 调用 FileIPC.send_message(session_id, content) → 返回 msg_id
5. 如果 --no-wait：exit(0)
6. 轮询 _sessions/<id>/context.jsonl（间隔 0.5s，超时默认 120s）：
   - 找到 offset_before 之后出现的 turn 事件
   - turn 事件中 user_input_id == msg_id（关联机制，见下方）
7. 提取 turn.messages 中最后一条 assistant 消息的文本
8. 打印到 stdout，exit(0)
```

**Turn 关联机制**：`send_message()` 写入 `user_input` 时带 `id` 字段（UUID）。Session daemon 在写 `turn` 事件时带 `user_input_id` 字段，指向触发本轮的 `user_input.id`。CLI 通过此字段匹配，避免误读 heartbeat 触发的 turn。

> 注：若现有 `turn` 事件无 `user_input_id` 字段，需在 `session.py` 中补充写入。

#### 新建 session 流程（自带 daemon，无需 server）

```
1. 选择 entity（默认 "agent"，--entity 可覆盖）
2. 调用 Session(entity_name, ...) 初始化（复用现有逻辑）
3. 启动 daemon 线程：asyncio.run(session.run_daemon_loop())（后台线程）
4. 等待 daemon 完成首次 input_offset 记录（通过 Event 同步）
   ← ⚠️ 必须先完成步骤 4，再执行步骤 5，否则消息被跳过
5. 调用 FileIPC.send_message() 写 user_input → 记录 msg_id
6. 打印 "Session: <id>"
7. 如果 --no-wait：exit(0)
8. 轮询 context.jsonl，等与 msg_id 匹配的 turn 事件（同续聊步骤 6-7）
9. 打印 agent 响应，再次打印 "Session: <id>"
10. 发信号停止 daemon 线程（设置 stop_event），等线程结束，exit(0)
```

**Daemon 退出**：`run_daemon_loop()` 接受一个 `stop_event: asyncio.Event`，轮询时检查它。CLI 在完成后 set 该 event，daemon 在下次轮询时退出。session 文件保留，下次 `nutshell-server` 启动时可恢复。

### 约束

- 不引入新依赖
- 所有错误到 stderr，exit(1)
- stdout 仅含响应文本 + `Session:` 行

---

## 2. `send_to_session` — Session 间通信工具

### Tool Schema（agent 看到的接口）

```json
{
  "name": "send_to_session",
  "description": "向另一个 Nutshell session 发送消息。mode=sync 时阻塞等待对方回复；mode=async 时立即返回。⚠️ 避免循环调用（A→B→A），会导致永久等待。",
  "input_schema": {
    "type": "object",
    "properties": {
      "session_id": { "type": "string", "description": "目标 session ID" },
      "message":    { "type": "string", "description": "发送的消息" },
      "mode":       { "type": "string", "enum": ["sync", "async"], "description": "默认 sync" },
      "timeout":    { "type": "number", "description": "sync 等待秒数（默认 60）" }
    },
    "required": ["session_id", "message"]
  }
}
```

### 实现：`nutshell/tool_engine/providers/session_msg.py`

#### Sync 流程

```
1. 验证 _sessions/<session_id>/manifest.json 存在，否则返回错误
2. FileIPC.send_message(session_id, message) → msg_id
3. 轮询 _sessions/<session_id>/context.jsonl（间隔 0.5s）
4. 找到 turn 事件且 turn.user_input_id == msg_id
5. 提取 assistant 文本，返回给调用方
6. 超时返回错误字符串（不 raise，agent 可处理）
```

#### Async 流程

```
1. 验证目标 session 存在
2. FileIPC.send_message(session_id, message)
3. 返回 "Message sent to session <id>"
```

### 注册

`tool_engine/registry.py` 的 `_BUILTIN_FACTORIES` 中加入：

```python
"send_to_session": lambda: _make_send_to_session_tool()
```

`entity/agent/tools/send_to_session.json` 放 schema 声明（与 `web_search.json` 结构相同）。

### 重要约束

- A→B→A 循环调用会造成永久等待，tool description 中已注明
- `send_to_session` 不可调用自身所在的 session（检测后返回错误）

---

## 3. 配套 `session.py` 改动

- `run_daemon_loop()` 增加 `stop_event: asyncio.Event | None = None` 参数
- 写 `turn` 事件时携带 `user_input_id` 字段（来自触发本轮的 `user_input.id`）
- `FileIPC.send_message()` 返回写入的 `msg_id`（UUID）

---

## 4. 实现顺序

1. **`session.py` / `ipc.py` 配套改动**（turn 关联、stop_event、msg_id 返回）
2. **`send_to_session` tool**（纯 Python，无 UI 依赖）
3. **`nutshell-chat` CLI**
4. **`entity/agent/tools/send_to_session.json`** + 示例

---

## 5. 测试覆盖

- `test_send_to_session.py` — sync/async/超时/session不存在/自身调用报错
- `test_cli_chat.py` — 新建session/续聊/`--no-wait`/daemon时序（先offset后写入）
- 更新 `test_ipc.py` — `send_message()` 返回 msg_id
- 更新 `test_session_capabilities.py` — turn 事件含 user_input_id
