# tests

一句话定位：存放 nutshell 的自动化测试，覆盖核心抽象、runtime 生命周期、CLI/Web 入口、工具系统与实体配置。

## 文件/子目录列表
### 根目录测试
- `test_agent.py`：Agent 主循环与消息/工具交互测试。
- `test_agent_loader_inheritance.py`：entity 继承与配置展开测试。
- `test_anthropic_provider.py` / `test_openai_provider.py`：provider 适配测试。
- `test_app_notify.py`：app notification 工具测试。
- `test_bash_tool.py`：bash 工具执行测试。
- `test_caller_detection.py`：caller 类型识别与 agent collaboration 行为测试。
- `test_cap.py`：cap/裁剪逻辑测试。
- `test_cli_*.py`：CLI 各命令测试（chat、kanban、keepalive、log_since、main、os_cmd、prompt_stats、token_report、visit）。
- `test_entity_*.py`：内置 entity 行为与配置测试。
- `test_entity_catalog.py`：entity 目录发现/索引测试。
- `test_entity_update.py` / `test_parent_update.py`：entity 更新提案流程测试。
- `test_fetch_url.py`：URL 抓取工具测试。
- `test_friends.py`：session 间通信相关 CLI/逻辑测试。
- `test_git_checkpoint.py` / `test_git_coordinator.py`：git 集成测试。
- `test_agent_iterations.py`：`AgentResult.iterations` 与工具循环迭代计数测试。
- `test_ipc.py`：文件 IPC 测试。
- `test_new_agent.py`：新 session 创建测试。
- `test_persistent_agent.py`：persistent agent 行为测试。
- `test_prompt_cache.py`：prompt cache 行为测试。
- `test_qjbq_server.py`：QjbQ / session message relay 测试。
- `test_reload_tool.py`：能力热重载测试。
- `test_repo_dev.py` / `test_repo_skill.py`：仓库开发/skill 相关流程测试。
- `test_sandbox.py`：sandbox 行为测试。
- `test_send_to_session.py`：session 间消息发送测试。
- `test_session_capabilities.py` / `test_session_display.py` / `test_session_venv.py`：session 辅助能力测试。
- `test_spawn_session.py`：动态创建 session 测试。
- `test_state_diff.py`：状态 diff 工具测试。
- `test_text_chunk_flush.py`：流式文本 chunk 刷新测试。
- `test_thinking_config.py`：thinking 参数测试。
- `test_tools.py`：工具注册与执行总体验证。
- `test_watcher.py`：watcher 扫描/恢复测试。

### `runtime/`
- `test_gene.py`：session/entity 基因或模板复制相关测试。
- `test_meta_session.py`：meta session 对齐与共享层测试。
- `test_runtime_watcher.py`：runtime watcher 集成测试。
- `test_session_factory.py`：session 初始化工厂测试。

### `tool_engine/`
- `test_archive_session.py`：session 归档工具测试。
- `test_count_tokens.py`：token 计数工具测试。
- `test_meta_session_tools.py`：meta-session 相关内置工具测试。
- `test_sandbox_classes.py`：sandbox 类单元测试。
- `test_web_sandbox.py`：web sandbox 与 fetch_url 测试。

## 关键设计 / 架构说明
- 测试按模块边界组织：根目录放跨模块和顶层功能，`runtime/`、`tool_engine/` 放子系统专测。
- 以 pytest 为主，广泛使用 `tmp_path`、monkeypatch、asyncio 标记验证文件系统驱动的运行时行为。
- 大量测试围绕真实目录结构（`sessions/`、`_sessions/`、`entity/`）构造最小实例，保证设计契约不只停留在纯函数层。
- 对 CLI/Web/provider/tool 的测试覆盖表明该仓库高度依赖集成边界稳定性，而不只是内部类行为。

## 主要对外接口
本目录不提供运行时代码接口，主要通过 pytest 使用：
```bash
pytest tests/ -v --tb=short
pytest tests/runtime/ -v
pytest tests/tool_engine/ -v
```
新增模块时，通常应在对应子系统附近补一个 `test_*.py`。

## 与其他模块的依赖关系
- 覆盖 `nutshell/core`、`nutshell/runtime`、`nutshell/skill_engine`、`nutshell/tool_engine`、`ui/`、`entity/` 的公开行为。
- 依赖 pytest 及其 asyncio/tmp_path/monkeypatch 机制。
- 是验证目录结构契约、session 生命周期和工具安全边界的主要保障层。
