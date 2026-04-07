# ui

一句话定位：提供 nutshell 的命令行和 Web 交互入口，把用户操作转成对 runtime/session 的调用。

## 文件/子目录列表
- `__init__.py`：UI 包标记文件。
- `cli/`：命令行界面。
  - `__init__.py`：CLI 子包初始化。
  - `main.py`：CLI 主入口与命令注册。
  - `chat.py`：启动/附着聊天会话。
  - `friends.py`：session 间通信/联系人相关命令。
  - `kanban.py`：任务板与 session 状态展示。
  - `new_agent.py`：创建新 agent/session。
  - `repo_skill.py`：仓库级 skill 管理命令。
  - `review_updates.py`：审阅待批准的 entity 更新提案。
  - `visit.py`：浏览/附着已有 session。
- `web/`：Web UI 与 HTTP 接口。
  - `__init__.py`：Web 子包初始化。
  - `app.py`：FastAPI 应用入口。
  - `index.html`：Web 前端页面。
  - `sessions.py`：session 相关 HTTP API；创建、列出、切换、发送消息等。
  - `weixin.py`：WeChat 桥接；把微信消息转发到 active session。

## 关键设计 / 架构说明
- UI 层尽量薄：主要做参数解析、展示和协议转换，不重新实现 agent/runtime 逻辑。
- CLI 与 Web 共用同一套 session 目录和 runtime 机制，因此行为一致、状态共享。
- Web 端通过 HTTP API + runtime 文件协议操作 session，不直接持有复杂会话状态；展示内容来自 session 文件、context/events 与显式参数。
- `weixin.py` 作为外部渠道桥接，复用 session/send/wait reply 机制，而不是单独实现聊天系统。
- 命令按职责拆分为多个 CLI 模块，避免单一入口文件膨胀。

## 主要对外接口
### CLI 入口
```bash
python -m ui.cli.main
# 或项目安装后的 nutshell ... 子命令
```
常见命令实现位于：
- `ui.cli.chat`：进入聊天。
- `ui.cli.new_agent`：创建 session。
- `ui.cli.visit`：访问已有 session。
- `ui.cli.kanban`：查看任务板/状态。

### Web 应用
```python
from ui.web.app import app
```
- `app`：FastAPI application，可由 uvicorn 启动。

### Session HTTP API
主要在 `ui/web/sessions.py` 中，对外暴露创建/列出/发送消息等接口，供前端和桥接渠道调用。

### `class WeixinBridge`
```python
from ui.web.weixin import WeixinBridge
bridge = WeixinBridge(sessions_dir, system_sessions_dir)
```
作用：把微信消息映射到当前 active nutshell session。

## 与其他模块的依赖关系
- 强依赖 `nutshell.runtime`：创建 session、读写状态、发送 IPC 事件、等待回复。
- 间接依赖 `entity/`：用户在 UI 中选择的 entity 会被 runtime 实例化。
- 部分 CLI 命令依赖 `nutshell.core` / `tool_engine` 提供的能力信息展示。
- 被测试覆盖于 `tests/test_cli_*`、`tests/test_friends.py`、`tests/test_new_agent.py` 等。
