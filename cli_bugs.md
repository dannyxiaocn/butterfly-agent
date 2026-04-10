# Nutshell CLI Bug Report

*Generated: 2026-04-10 — Full CLI test round*

---

## Bug 1: `--session` 在 `log` 命令中被 argparse 误解为 `--sessions-base` [高危]

### 现象
```bash
nutshell log --session test-cli-session
# 实际效果：查看的是错误的 session，不会报错
```

### 根因
argparse 默认开启 `allow_abbrev=True`（前缀缩写匹配）。
`log` 子命令有 `--sessions-base` 和 `--system-base` 两个隐藏参数，
`--session` 是 `--sessions-base` 的合法前缀缩写，因此被静默地匹配为：
```
--sessions-base test-cli-session
```
这导致 sessions 查找目录被设为 `test-cli-session`（不存在），最终选出错误的 session。

### 影响命令
所有使用了 positional `session_id` + 隐藏 `--sessions-base` 的子命令：
- `nutshell log`
- `nutshell tasks`
- `nutshell visit`
- `nutshell prompt-stats`
- `nutshell token-report`
- `nutshell kanban`

### 修复方案
在 `main()` 的 top-level parser 中设置 `allow_abbrev=False`，或者给上述命令的子 parser 也设置。

---

## Bug 2: CLI 接口不一致 — `chat` 用 `--session`，其他命令用 positional [中危]

### 现象
```bash
# chat 用 flag：
nutshell chat --session test-cli-session "消息"

# 其他命令用 positional：
nutshell log test-cli-session
nutshell tasks test-cli-session
nutshell visit test-cli-session
nutshell prompt-stats test-cli-session
nutshell token-report test-cli-session
```

### 根因
`chat` 命令的设计思路不同（消息是必需的 positional，session 是可选的），
其他命令只需要 session_id，设计为 positional。
这两种风格混用，导致用户（尤其从 `chat` 切换到 `log`）自然地写出 `nutshell log --session ...` 而不报错，但行为完全错误（见 Bug 1）。

### 修复方案
选一：给 `log`/`tasks`/`visit`/`prompt-stats`/`token-report` 也添加 `--session` alias（保留 positional 同时支持 flag）。
选二：修复 Bug 1（allow_abbrev=False），让错误用法至少报错而不是静默错误。

---

## Bug 3: `nutshell log` 默认 session 选择依赖 last_run_at，但文档说"最近活跃" [低危]

### 现象
`nutshell sessions` 的顺序和 `nutshell log` 默认选取的 session 不一致。
`log` 选的是 `_sort_sessions()[0]`，优先级排序为：napping < idle < running，
而 `sessions` 命令排序逻辑相同。实际表现是 meta session（agent_meta）在某些状态下会被优先选中，而用户通常期望选择"最近用于聊天的 session"。

### 根因
`_session_priority` 函数将 meta session 和 non-meta session 混排。
用户想要的"最近的 chat session"可能排在 meta session 之后。

---

## Bug 4: `--no-wait` 成功发送时无任何输出 [低危]

### 现象
```bash
nutshell chat --session xxx --no-wait "消息"
# 输出：（空）
```
没有任何确认信息，用户不知道消息是否真的发送成功。

### 建议
输出一行类似 `[queued] message sent to xxx` 的确认。

---

## 验证通过的功能（正常）

| 命令 | 结果 |
|------|------|
| `nutshell sessions` | ✅ 正确列出所有 session |
| `nutshell friends` | ✅ IM 风格展示，在线状态正确 |
| `nutshell friends --json` | ✅ JSON 输出格式正确 |
| `nutshell new [id] --entity [name]` | ✅ 创建 session 正常 |
| `nutshell chat --session [id] "msg"` | ✅ 消息发送 + agent 执行正常 |
| `nutshell chat --session [id] --no-wait "msg"` | ✅ 异步发送正常（但见 Bug 4）|
| `nutshell log [session_id]` | ✅ Positional 方式正确显示历史 |
| `nutshell log [session_id] -n N` | ✅ -n 参数正常 |
| `nutshell tasks [session_id]` | ✅ Task card 显示正常 |
| `nutshell kanban` | ✅ 全局 task 看板正常 |
| `nutshell visit [session_id]` | ✅ agent room 视图正常 |
| `nutshell prompt-stats [session_id]` | ✅ Prompt 空间分析正常 |
| `nutshell token-report [session_id]` | ✅ Token 报告正常 |
| `nutshell meta [entity]` | ✅ 元信息查看正常 |
| `nutshell dream [entity]` | ✅ 命令存在，参数正确 |
| Context 连续性 | ✅ 多轮对话历史正确追加到 context.jsonl |
| History 加载 | ✅ `load_history()` 从 context.jsonl 的 `turn` events 重建 agent._history |
| `_sessions/<id>/context.jsonl` | ✅ 存储格式正确（user_input + turn events）|
| `_sessions/<id>/events.jsonl` | ✅ 运行时事件独立存储 |

---

## Bug 5: `nutshell entity new -n NAME` 仍会触发交互式提示（要求选择 parent）[低危]

### 现象
```bash
nutshell entity new -n my-agent
# 输出: "Extend which entity? ..."
# 然后 EOFError (非交互环境下)
```

### 根因
`cmd_entity` 中当 `args.standalone` 和 `args.extends` 都未指定时，会调用 `_ask_parent()` 进行交互式询问。`-n NAME` 只跳过了 name 询问，未跳过 parent 询问。

### 修复方案
添加 `--parent NAME` flag 或让 `-n NAME` + 无 `--extends/--standalone` 时默认使用 `agent` 作为 parent（当前 help 示例误导用户认为 `-n` 足够非交互）。

### 临时 workaround
```bash
nutshell entity new -n my-agent --extends agent
# 或
nutshell entity new -n my-agent --standalone
```

---

---

## Bug 6: Web API `/api/sessions/{session_id}/history` 返回 HTTP 500 [高危]

### 现象
```bash
curl http://localhost:8080/api/sessions/lifecycle-test/history
# 输出：Internal Server Error (HTTP 500)
# 所有 session 均返回 500
```

### 根因
`ui/web/app.py` 第 30 行只导入了 `write_session_status`，但 `get_history` 路由（第 242 行）调用了未导入的 `read_session_status`：
```python
# 原来（bug）：
from nutshell.session_engine.session_status import write_session_status
# ...
status_payload = read_session_status(system_dir)  # NameError!
```

### 修复方案
```python
from nutshell.session_engine.session_status import read_session_status, write_session_status
```

### 影响
所有 session 的 history API 均不可用，Web UI 无法显示对话历史。

---

## 修复状态

| Bug | 状态 | 修复说明 |
|-----|------|----------|
| Bug 1 | ✅ 已修复 | `ui/cli/main.py`: 给 top-level parser 添加 `allow_abbrev=False`；同时给各子 parser 显式添加 `--session` flag，从根本上消除歧义 |
| Bug 2 | ✅ 已修复 | `ui/cli/main.py`: `log`/`tasks`/`visit`/`prompt-stats`/`token-report` 均添加 `--session ID` alias（positional 仍保留）。使用 `default=argparse.SUPPRESS` 防止 positional 覆盖 optional |
| Bug 3 | 待处理 | 低优先级，暂不修改 |
| Bug 4 | ✅ 已修复 | `ui/cli/chat.py` `_continue_session()`: `no_wait=True` 时输出 `[queued] message sent to {session_id}` |
| Bug 5 | 待处理 | `entity new -n NAME` 仍触发交互式 parent 询问；临时 workaround：加 `--extends agent` 或 `--standalone` |
| Bug 6 | ✅ 已修复 | `ui/web/app.py` line 30: 添加 `read_session_status` 到 import；重启 nutshell-web 后验证所有 session history 返回 200 |
