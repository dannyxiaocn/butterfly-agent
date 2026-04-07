# nutshell/runtime

一句话定位：负责把静态 entity 配置实例化成可运行 session，并驱动 watcher、IPC、状态管理与会话生命周期。

## 文件列表
- `__init__.py`：导出运行时常用入口（如 `SessionWatcher`、`init_session`）。
- `agent_loader.py`：加载 entity 定义与继承链，生成扁平化 agent/session 配置。
- `bridge.py`：面向 UI / 外部入口的桥接层，封装 session 发送、等待回复等高层操作。
- `cap.py`：资源/上下文裁剪与显示相关辅助逻辑。
- `entity_updates.py`：生成对 entity 或 parent entity 的持久化更新提案。
- `env.py`：仓库、sessions、_sessions、entity 等路径/环境变量解析。
- `git_coordinator.py`：会话内 git 协调辅助，处理 checkpoint/工作树交互。
- `ipc.py`：基于文件的 IPC 实现；读写 `events.jsonl`、`context.jsonl` 等。
- `meta_session.py`：meta session 机制；校验 entity 与 meta 状态对齐、同步共享可变层。
- `params.py`：读取与写入 `core/params.json` 等显式运行参数。
- `server.py`：runtime server 入口，启动 watcher 等后台服务。
- `session.py`：`Session` 主体；负责加载 core/ 资源、驱动 Agent、持久化上下文/事件与 heartbeat。
- `session_factory.py`：初始化 `sessions/<id>/` 与 `_sessions/<id>/` 目录结构。
- `status.py`：`status.json` 读写与 pid 存活检测。
- `watcher.py`：轮询 `_sessions/`，自动发现并拉起 session daemon。

## 关键设计 / 架构说明
- runtime 将“entity 定义”和“session 实例”分离：entity 是模板，session 是落盘后的运行实例。
- 用户可编辑文件放在 `sessions/<id>/`，系统内部状态放在 `_sessions/<id>/`；避免职责混杂。
- `Session` 是运行时核心：每次激活前重新从 `core/` 读取 prompt、memory、skills、tools，实现热更新。
- provider/model/thinking 等运行参数由 `core/params.json` 与 entity 默认值显式决定，runtime 不做基于任务内容的自动切模型。
- `watcher.py` 采用轮询而非复杂文件监听，优先保证恢复能力与跨平台可用性。
- meta session 机制把继承展开后的共享记忆/可变 playground 提升到 entity 实例层，后续子 session 可复用。
- IPC 采用 JSONL 文件协议，便于 CLI、Web、测试和其他进程统一接入。

## 主要对外接口
### `init_session(...)` in `session_factory.py`
```python
from nutshell.runtime.session_factory import init_session

init_session(
    session_id='2026-01-01_12-00-00',
    entity_name='agent',
)
```
作用：创建 session 目录、复制 prompts/tools/skills/memory、写入 manifest/status/params。

### `class Session`
```python
from nutshell.runtime.session import Session

session = Session(agent, session_id='sid', base_dir=sessions_dir, system_base=system_base)
await session.run_daemon_loop(ipc)
```
关键方法：
- `_load_session_capabilities()`：从 `core/` 热加载 prompts、skills、tools。
- `load_history()`：从持久化上下文恢复消息历史。
- `run_daemon_loop(ipc)`：进入守护循环，处理 user input / heartbeat。

### `class SessionWatcher`
```python
watcher = SessionWatcher(sessions_dir, system_sessions_dir)
await watcher.run(stop_event)
```
作用：扫描 `_sessions/` 并启动/恢复 session task。

### `read_session_status()` / `write_session_status()`
```python
from nutshell.runtime.status import read_session_status, write_session_status
```
作用：维护 `status.json` 的动态状态字段。

### `FileIPC`
```python
from nutshell.runtime.ipc import FileIPC
ipc = FileIPC(system_dir)
ipc.append_event({...})
```
作用：运行时事件传递。

## 与其他模块的依赖关系
- 依赖 `nutshell.core`：创建并驱动 `Agent`、`Tool`、`Skill` 抽象。
- 依赖 `nutshell.skill_engine` / `nutshell.tool_engine`：从 session `core/skills`、`core/tools` 热加载能力。
- 依赖 `nutshell.llm_engine`：在创建 Agent 时解析 provider。
- 被 `ui/` 调用：CLI/Web 通过 bridge、session_factory、status、ipc 等接口操纵 session。
- 与 `entity/` 强相关：`agent_loader.py`、`meta_session.py` 直接消费 entity 配置树。
