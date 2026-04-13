# Nutshell — Todo

> 开发看板。Commit 格式：`v{version} [{tag}] {description}`，tags: `[impl]` `[debug]` `[clean]` `[docs]` `[chore]`

---

## Active

### Module 8 · Codebase Pruning (partial)

- [ ] **`runtime/` 中 session 内容移至 `session_engine/`**：与 llm_engine/tool_engine/skill_engine 命名对齐；更新约 30 处 import
- [ ] **`loader.py` 移至 `session_engine/`**：`AgentConfig.from_path()` 属文件 IO，不属于 core 纯计算层

### Module 9 · Skill Engine 深化实现

- [ ] **skill frontmatter 扩展到 Claude Code 兼容子集**：支持 `allowed_tools` / `arguments` / `argument-hint` / `when_to_use` / `context` / `model` 等字段，统一 schema 与解析层
- [ ] **skill tool 接入权限与上下文修改**：skill 加载后可临时追加 tool allowlist、thinking/model override，行为接近 Claude Code Skill tool 的 context modifier
- [ ] **skill 参数替换语义完善**：从当前 `$ARGUMENTS` + 简单 positional 替换，升级到具名参数、缺省值、引用转义和错误提示
- [ ] **skill 资源导入机制**：支持 skill 目录下 `agents/`、`prompts/`、`references/` 等附属文件的标准化发现与注入提示
- [ ] **skill prompt 导入与持久化策略**：解决 skill 被加载后在多轮对话、history compact、sub-agent/fork 场景中的保留与恢复问题
- [ ] **session / entity / user 三级 skill 源**：补用户级 skill 目录与优先级/去重逻辑
- [ ] **conditional skill activation**：支持按路径模式或工作区上下文激活 skill，减少大 skill catalog 的噪音
- [ ] **skill tool observability**：记录 skill load/use 事件到 runtime stats
- [ ] **skill engine 端到端测试补强**：新增 provider 真实交互模拟

### CLI-as-Authority Follow-ups

See `docs/nutshell/service/todo.md` for full detail.

## Backlog

- [ ] CLI-started sessions: auto background server for task execution, auto-stop when no pending
- [ ] Agent room mode: enter agent room instead of online chat
- [ ] Agent-agent communication protocol
- [ ] Sub-agent spawning (call sub-agent / spawn_session)
- [ ] Sub-agent ACP to OpenClaw
- [ ] Auto cache system
---

## Completed

### Module 1 · CLI 清理

- [x] **删除遗留 `nutshell chat` 旧接口和 dui 系列命令** (commit: 95c593f)
- [x] **CLI 接口审计** (commit: 95c593f)：27 个 subcommand 无重叠无废弃；entry point 已最小化

### Module 2 · Multi-agent 通信 & CAP

- [x] **QJBQ 移到 `cli_app/`** (commit: c917184)
- [x] **删除系统自带通信方式，统一用 QJBQ** (commit: 9c015a9)
- [x] **Cambridge Agent Protocol (CAP) 模块设计** (commit: b36eb75)：`nutshell/runtime/cap.py` — 已在清理中删除

### Module 3 · Meta-session 完善

- [x] **整理现有 entity** (commit: bc2cc6f)
- [x] **meta session 作为 entity 实例化单位** (commit: 9472524)：`sync_from_entity()` 引导 memory 与 playground
- [x] **给 meta session 配工具：子 session 管理** (commit: 6a1c5c4)：dream 机制 24h 审查子 session
- [x] **Bridge 子进程标注 + Persistent agent 标识** (commit: d773ed7)
- [x] **分层 memory 调整（单向流）** (commit: 5d895fc)

### Module 4 · Entity 继承系统改进

- [x] **meta session 完备，entity 保留继承标识** (commit: b4fbc50)：link/own/append 字段
- [x] **entity 可更新 parent entity 内容** (commit: 6bf957d)：`propose_parent_update` 接口

### Module 5 · Thinking 配置

- [x] **enable thinking 作为可配置项** (commit: 4978494)：params.json 新增 thinking/thinking_budget
- [x] **Kimi thinking via extra_body** (llm_engine audit commits)
- [x] **CodexProvider 默认模型 gpt-5.4 + high thinking** (commit: bd5d01d)
- [x] **thinking_effort 可配置** (commit: 91762f5)

### Module 6 · 安全审查 / Sandbox 重设计

- [x] **Tool-level sandbox 重设计** (commit: 4a48ad3)：ToolSandbox 基类，BashSandbox / WebSandbox / FSSandbox
- [x] **WebSandbox 实现** (commit: ec90b69, 2d49425)：域名黑名单 + 响应截断

### Module 7 · 工具 Stats 系统

- [x] **Token 计算器 tool** (commit: cb43c19)
- [x] **Tool manager persistent agent** (commit: 3130b36)
- [x] **Nutshell 专职 persistent agent 体系** (commit: 369fede)

### Module 8 · Codebase Pruning (partial)

- [x] **`release_policy` 清除** (commit: 71db1b4)
- [x] **`core/hook.py` 接入 session_engine** (commit: 29f4996)：chat() 和 tick() 统一为 hook 传递
- [x] **`session_type` 三态替换 `persistent` bool**
- [x] **Task card 系统替换 tasks.md**

---

## 早期开发记录（v1.0 – v1.3.47）

### CLI & UI

- [x] 删除 tui，保留 web 端监控 (93312c7 v1.3.2)
- [x] 全面转向 cli (ee1dc63 v1.3.1)
- [x] cli+web parity — cli session 在 web 端实时可见 (8606176 v1.3.9)
- [x] bug: cli session 无法在 web 端实时显示 (9d6d156 v1.3.23)
- [x] 做一个 TUI (4420309 v1.3.12)（后因 v1.3.38 路线调整删除）
- [x] 添加 CUI，agent 可直接调用 (809efc0 v1.1.9 + ee1dc63 v1.3.1)
- [x] 系统兼容 openclaw skills/tools
- [x] 接口兼容 claude code → Anthropic SDK

### Agent 交互

- [x] git 工作区 / git_checkpoint tool (72d6418 v1.3.16)
- [x] nutshell_dev 自主领取 track.md 任务 (173e884 v1.3.11)
- [x] nutshell_dev memory 含 track.md 快照 + --inject-memory (37a04d2 v1.3.8)
- [x] nutshell_dev 自动标记 track.md + commit (31244b1)
- [x] nutshell chat 默认 timeout 修复 (95329bd v1.3.7)

### 用户交互

- [x] 任务板用户和 agent 都能看到 (2b907b1 v1.3.3)
- [x] nutshell log SESSION_ID [-n N] (5678b8e v1.3.4)

### Filesystem-as-everything & Tools

- [x] system prompt 过长优化 (7d45608 v1.1.6)
- [x] session memory 分层 (71f9c66 v1.1.7)
- [x] memory recall skill (5dd735b v1.2.3)
- [x] creator mode skill (d86c2b6)
- [x] entity 版本控制 + propose_entity_update review 流程 (3abfba4 v1.2.1 / d239374 v1.3.14)
- [x] agent 自己迭代 tool 和 skill（热插拔）
- [x] skills 分段式 load in + memory layer 60 行自动截断 (3c12fce v1.3.10)
- [x] web search tool（brave + tavily 多 provider）

### Multi-agent

- [x] receptionist entity — 接待 agent (79e89bc v1.3.26)
- [x] multi-agent runtime (1871749 v1.2.2 + 679e482 v1.2.4)

### Runtime Feedback & Token

- [x] 环境与反馈系统 (a26e603 v1.3.18)
- [x] 约束系统 sandbox (272c76b v1.3.19)
- [x] token 追踪 (8c4b494 v1.2.7)
- [x] nutshell token-report (2888655 v1.3.17)
- [x] memory cache 优化 (ee7d6eb v1.2.0)
- [x] task prompt 精简 (0bb3337 v1.2.5)
- [x] state_diff tool (f075de1 v1.3.13)
- [x] Prompt 优化 (693b3a8 v1.3.15)

### CLI Apps for Agent

- [x] 实时通信 — nutshell friends (cf83515 v1.3.22)
- [x] app notification system (59e4375 v1.3.24)
- [x] 看板 nutshell kanban (3902211 v1.3.25)
- [x] Repo as a skill / deepwiki (ffcb72d v1.3.20)
- [x] repo_dev agent (66cfbdf v1.3.21)
