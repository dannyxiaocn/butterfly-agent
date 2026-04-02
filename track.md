# Nutshell Track

> 驱动开发看板。实现类任务必须派发给 `nutshell_dev_codex`，Claude Code 只做维护类操作。  
> 每次完成一个任务后：更新 track.md（标记[x] + commit ID）、MEMORY.md、README.md，检查遗留代码。

---

## Module 0 · DEBUG（快速修）

- [x] **harness.md 模型字段错误** (commit: fada1b0)：`session.py:658` `self._agent.model` 写入的是 agent.yaml 里的 model 字符串（如 `openai-codex/gpt-5.4`），但有些 session 里 model 不一致（例如同一 entity 出现 `gpt-5.4` 和 `openai-codex/gpt-5.4` 两种写法）。需要 provider 在 `complete()` 后回填实际 model ID，或者从 provider 读取 `actual_model`，写入 harness 时加 `provider` 字段一并显示。

---

## Module 1 · CLI 清理

- [x] **删除遗留 `nutshell chat` 旧接口和 dui 系列命令** (commit: 95c593f)：删除 pyproject.toml 里 5 个遗留 entry points（nutshell-chat/server/web/new-agent/review-updates）；cmd_review 去掉 sys.argv hack 改为直接调用 review_updates()；new_agent.py/review_updates.py 各自删除 main()。
- [x] **CLI 接口审计** (commit: 95c593f)：审查后确认 27 个 subcommand 无重叠无废弃；entry point 清理后接口已最小化。

---

## Module 2 · Multi-agent 通信 & QJBQ 融合 + Cambridge Agent Protocol

- [x] **QJBQ 移到 `cli_app/`** (commit: c917184)：git mv qjbq/ → cli_app/qjbq/；创建 cli_app/__init__.py；更新 pyproject.toml packages + entry point；更新所有 import；762 tests pass。
- [x] **删除系统自带通信方式，统一用 QJBQ** (commit: 9c015a9)：`send_to_session` 改为经由 QJBQ `POST /api/session-message` 发送消息，QJBQ 统一写入目标 `_sessions/<id>/context.jsonl`；保留 relay 不可用时的 direct-write fallback 以兼容现有测试与迁移期场景。
- [x] **Cambridge Agent Protocol (CAP) 模块设计** (commit: b36eb75)：新增 `nutshell/runtime/cap.py`，定义 `handshake`、`lock`、`broadcast`、`heartbeat-sync` 四类协议原语，并将 `git_coordinator` 暴露为首个 CAP protocol adapter；新增 `tests/test_cap.py` 覆盖原语语义与 git adapter。

---

## Module 3 · Meta-session 完善

- [x] **整理现有 entity** (commit: bc2cc6f)：新增 `entity/README.md` 作为内建 entity catalog，并为 10 个活跃 entity 补齐 `README.md`，明确用途、状态与保留原因；新增 `tests/test_entity_catalog.py` 防止再次出现用途不明的 entity。
- [ ] **meta session 作为 entity 实例化单位**：确认 `sessions/<entity>_meta/` 完整继承 memory + playground；补全对应文档和 agent.yaml 里的说明字段。
- [ ] **给 meta session 配工具：子 session 管理**：  
  - `list_child_sessions(entity)` — 列出 entity 所有子 session + status  
  - `get_session_info(session_id)` — 获取 session manifest + 最近 turns + tasks  
  - `archive_session(session_id)` — 将 session 移到 `_archived/` 而非删除  
  - dream 机制：meta session 定期（24h）审查子 session，决定 keep / archive
- [ ] **Bridge 子进程标注 + Persistent agent 标识**：  
  - 已 link 到 bridge 的子进程：状态显示 `napping`（嫩绿色灯），而非普通 idle  
  - persistent agent（`params.json` 里 `persistent: true`）：单独颜色/图标，表示一直在运行  
  - 在 `nutshell friends` 和 web UI 中体现
- [x] **分层 memory 调整（单向流）** (commit: 5d895fc)：删除 update_meta_memory tool 及其测试/registry/entity json；meta_session.py prompt 改为说明 memory 由系统管理；README/CLAUDE.md/index.html 清理残留。761 passed。

---

## Module 4 · Entity 继承系统改进

- [ ] **meta session 完备，entity 保留继承标识**：  
  - meta session 展开后完全看不到继承关系（扁平化），对 agent 透明  
  - entity 定义里保留标识：`link`（指向 parent）、`own`（自己独有）、`append`（在 parent 基础上追加）  
  - meta session 的「自己部分」完全独立，entity 更新只更新继承的部分  
- [ ] **entity 可更新 parent entity 内容**：设计 `propose_parent_update` 接口（已有 `propose_entity_update`），支持更新 parent entity 的继承内容，走 review 流程

---

## Module 5 · Thinking 配置

- [x] **enable thinking 作为可配置项** (commit: 4978494)：params.json 新增 thinking/thinking_budget；AnthropicProvider 支持 betas+thinking block；KimiProvider _supports_thinking=False 保护；OpenAI/Codex 静默忽略；3 新测试，765 passed。

---

## Module 6 · 安全审查 / Sandbox 重设计

- [ ] **Tool-level sandbox 重设计**（参考 claude code tool sandbox）：  
  - 当前 sandbox 是 bash 命令级别（`BashExecutor` 过滤危险命令）  
  - 新设计：每个 tool 有独立 sandbox policy（pre-check + post-filter），agent 不可见 sandbox 逻辑，只看 tool 返回  
  - 跳出 playground 修改系统文件：在 bash tool sandbox 里加 path scope 限制  
  - 设计 `nutshell/tool_engine/sandbox.py`：`ToolSandbox` 基类，`BashSandbox` / `WebSandbox` / `FSSandbox` 实现

---

## Module 7 · 工具 Stats 系统

- [ ] **Harness 分层命名**：  
  - `harness`（保留）= agent 自我感知组件的总称  
  - 拆分为：`sys_harness`（系统基础，每 turn 自动写）+ `audit_harness`（工具/skill 使用审计，跨 session 聚合）  
  - 读读 harness 相关 blog，明确定义边界
- [ ] **Token 计算器 tool**：`count_tokens(text, model)` built-in tool，调用 tokenizer 精确计算 token 数，供 agent 在操作前评估 cost。
- [ ] **Tool manager persistent agent**：专职维护 tool stats，定期聚合 harness 数据，输出 tool 使用热力图 + 效率报告到 `_sessions/tool_stats/`。
- [ ] **Nutshell 专职 persistent agent 体系**：  
  - `dev_maintainer` — 保证无 bug + 最精简化每个功能（基于 nutshell_dev）  
  - `tool_craftsman` — 不断迭代打磨 tool/skill 质量  
  - 历史 audit 数据保留在 `_sessions/<entity>_meta/core/audit/`，与 meta session dream 频次 align  
  - **与 meta session 协调**：meta session「删除」子 session 时移到 `archived/`，audit 数据不丢失

---

## 完成记录

<!-- 格式：- [x] 任务名 (commit: abc1234) -->
