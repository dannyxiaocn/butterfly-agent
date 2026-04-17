# Butterfly — Todo

> 开发看板。Commit 格式：`v{version} [{tag}] {description}`，tags: `[impl]` `[debug]` `[clean]` `[docs]` `[chore]`

---

## Active

### Module 12 · v2.0.8 First-run fixes

- [x] **首装 quick-start 会话 `config.yaml` 的 `model: null`** (v2.0.8 [debug])：
  `init_session()` 把 `_sessions/<id>/manifest.json` 写在前面，触发 server 端 watcher
  抢跑 `Session.__init__` → `ensure_config()` 把 `DEFAULT_CONFIG`（`model=None`,
  `provider=None`）落到 `sessions/<id>/core/config.yaml`，之后 `init_session` 的
  `if not session_config_path.exists()` 拷贝分支被跳过，session 永远只带 null
  model。修复：(a) 把 manifest.json 移到函数末尾，只有当 session core/ 里 config
  准备好之后 watcher 才能看到；(b) 把拷贝条件从 "文件不存在" 放宽到 "文件缺
  model"，让即使抢跑产生的 stub config 也会被真正的 agent config 覆盖。
  回归测试：`tests/butterfly/session_engine/test_session_init.py::test_init_session_config_not_clobbered_by_concurrent_ensure_config`
  以及 `::test_init_session_writes_manifest_after_config_populated`。

### Module 11 · v2.0.5 Tool Engine Evolution

Big redesign. Full spec in `docs/butterfly/tool_engine/design.md` and `docs/butterfly/session_engine/design.md` (Memory layers — on-demand recall section).

**Foundation**
- [ ] Add `Tool.backgroundable` field + ToolLoader auto-injection of `run_in_background` / `polling_interval` schema fields + description suffix
- [ ] Create `butterfly/session_engine/panel.py` (Panel entry schemas, panel dir lifecycle)
- [ ] `sessions/<id>/core/panel/` dir created at session init alongside `core/tasks/`

**Tool rewrites / additions**
- [ ] Unify bash: drop PTY; delete shell_terminal.py; add `stdin` param; structured output; backgroundable=true
- [ ] New tool `session_shell` (persistent bash, sentinel protocol, auto-restart, single-command timeout → SIGINT→SIGKILL→restart)
- [ ] New tools `read` / `write` / `edit` (exact-string, uniqueness-enforced)
- [ ] New tools `glob` / `grep` (ripgrep-preferred, Python `re` fallback)
- [ ] New tool `web_fetch` (provider-abstracted like web_search; default httpx+trafilatura)
- [ ] Split `manage_task` → `task_create` / `task_update` / `task_finish` / `task_pause` / `task_resume` / `task_list`
- [x] Rename + behavior change: `memory_recall` (β, fetch-only) + new `memory_update` (patch sub-memory + upsert main memory index line)
- [ ] New tool `tool_output(task_id, delta?)` — fetch backgrounded-tool output from panel
- [ ] Remove `reload_capabilities` injection from `session.py` + delete `butterfly/tool_engine/reload.py`

**Non-blocking infrastructure**
- [ ] `butterfly/tool_engine/background.py` — `BackgroundTaskManager`: spawn tracked subprocess, delta-output polling, stall watchdog (5m silence + interactive-prompt regex), completion hook
- [ ] `Agent._execute_tools`: route `run_in_background=true` calls to BackgroundTaskManager, return placeholder `tool_result` with `task_id`; mixed gather preserves call ordering
- [ ] Daemon loop: background completion / stall / progress → append-once user-role message to `context.jsonl` + wake agent; mirror `panel_update` event to `events.jsonl`
- [ ] Server restart: mark all `running` panel entries `killed_by_restart` + append notification

**UI**
- [ ] Web UI: new **Panel** tab next to Tasks tab (right sidebar). One row per entry; click-through detail; Kill / Fetch actions
- [ ] CLI: `butterfly panel` (one-line list) + `butterfly panel --tid <tid>` (detail + `--kill` / `--output`)

**Sync-up**
- [ ] Rewrite `agenthub/agent/tools.md` and `agenthub/butterfly_dev/tools.md` for the new catalog (drop `shell`, `manage_task`, `reload_capabilities`; add new ones)
- [ ] Migrate meta sessions: either delete `sessions/<name>_meta/` + `_sessions/<name>_meta/` for re-bootstrap, or mirror tool list into existing metas
- [ ] `CLAUDE_*` env vars → `BUTTERFLY_*` (notably `${CLAUDE_SKILL_DIR}` in skill_tool.py); no alias
- [ ] Memory (β) wiring: drop `memory_layers` from prompt build; verify main-memory index flow end-to-end
- [ ] Regenerate `docs/butterfly/tool_engine/executor/*/design.md` + `impl.md` + `todo.md` for each tool subdir
- [ ] Tests: per-tool unit tests + integration tests for non-blocking flow + panel lifecycle
- [ ] Bump pyproject.toml to 2.0.5 (rebased onto main's v2.0.4 LLM hotfix; branch version shifted +1)

### Module 10 · v2.0.2 Follow-ups (from PR #17 review)

- [ ] **验证 Codex `gpt-5-codex` 默认模型是否在 ChatGPT-OAuth 端点可用**：PR #17 把 `CodexProvider.DEFAULT_MODEL` 从 `gpt-5.4` 改成 `gpt-5-codex`（对齐 openai/codex-rs CLI），但 PR description 中 `Manual end-to-end call against Codex (codex-oauth) with thinking=True` 的 checkbox 未勾选 —— 没有人真实跑过这个组合。同时 `agenthub/agent/config.yaml`、`agenthub/butterfly_dev/config.yaml`、`README.md`、`docs/agent/agent/impl.md`、`ui/web/frontend/src/components/chat.ts` 里的 token 上限表仍是 `gpt-5.4`，处于不一致状态。需要：
  - 手动跑一次 `codex-oauth` + `thinking=True` + 无显式 model 的会话，确认 `gpt-5-codex` 被服务端接受
  - 确认后要么统一 agenthub/docs/frontend 都改成 `gpt-5-codex`，要么把 `DEFAULT_MODEL` 回退到 `gpt-5.4` 保持兼容
- [ ] **`agent.py` `consume_extra_blocks` 的 `getattr` fallback 已是死代码**：基类 `Provider` ABC 现在默认实现了 `consume_extra_blocks`（返回 `[]`），`getattr(active_provider, "consume_extra_blocks", lambda: [])()` 里的 `lambda: []` 永远不会命中。留着是防御性写法，但严格说冗余。可简化为 `active_provider.consume_extra_blocks()`
- [ ] **`openai_api._tc_map_to_list` 未过滤空 `name` 的条目**：Codex 和 OpenAI Responses 两个 provider 都有 `if tc["name"]` 过滤，只有 Chat Completions 这条没有。实际 OpenAI 第一个 tool_call delta 一定带 name，不会真触发，但三个 provider 的行为不对齐，属于一致性 drift。补一个 `if entry["name"]`
- [ ] **`_get_fallback_provider` 在只配 `fallback_model` 时静默失效**：`agent.py` 的 `_get_fallback_provider` 早期 return 要求 `fallback_provider` 和 `fallback_model` 两个都为空；但只配 `fallback_model`（不配 `fallback_provider`）时，`self._fallback_provider` 会留在 None，fallback 整条路径被跳过。要么只配 `fallback_model` 时报错提示，要么继承主 provider 类型

### Module 8 · Codebase Pruning (partial)

- [ ] **`runtime/` 中 session 内容移至 `session_engine/`**：与 llm_engine/tool_engine/skill_engine 命名对齐；更新约 30 处 import
- [ ] **`loader.py` 移至 `session_engine/`**：`AgentConfig.from_path()` 属文件 IO，不属于 core 纯计算层

### Module 9 · Skill Engine 深化实现

- [ ] **skill frontmatter 扩展到 Claude Code 兼容子集**：支持 `allowed_tools` / `arguments` / `argument-hint` / `when_to_use` / `context` / `model` 等字段，统一 schema 与解析层
- [ ] **skill tool 接入权限与上下文修改**：skill 加载后可临时追加 tool allowlist、thinking/model override，行为接近 Claude Code Skill tool 的 context modifier
- [ ] **skill 参数替换语义完善**：从当前 `$ARGUMENTS` + 简单 positional 替换，升级到具名参数、缺省值、引用转义和错误提示
- [ ] **skill 资源导入机制**：支持 skill 目录下 `agents/`、`prompts/`、`references/` 等附属文件的标准化发现与注入提示
- [ ] **skill prompt 导入与持久化策略**：解决 skill 被加载后在多轮对话、history compact、sub-agent/fork 场景中的保留与恢复问题
- [ ] **session / agent / user 三级 skill 源**：补用户级 skill 目录与优先级/去重逻辑
- [ ] **conditional skill activation**：支持按路径模式或工作区上下文激活 skill，减少大 skill catalog 的噪音
- [ ] **skill tool observability**：记录 skill load/use 事件到 runtime stats
- [ ] **skill engine 端到端测试补强**：新增 provider 真实交互模拟

### CLI-as-Authority Follow-ups

See `docs/butterfly/service/todo.md` for full detail.

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

- [x] **删除遗留 `butterfly chat` 旧接口和 dui 系列命令** (commit: 95c593f)
- [x] **CLI 接口审计** (commit: 95c593f)：27 个 subcommand 无重叠无废弃；entry point 已最小化

### Module 2 · Multi-agent 通信 & CAP

- [x] **QJBQ 移到 `cli_app/`** (commit: c917184)
- [x] **删除系统自带通信方式，统一用 QJBQ** (commit: 9c015a9)
- [x] **Cambridge Agent Protocol (CAP) 模块设计** (commit: b36eb75)：`butterfly/runtime/cap.py` — 已在清理中删除

### Module 3 · Meta-session 完善

- [x] **整理现有 agent** (commit: bc2cc6f)
- [x] **meta session 作为 agent 实例化单位** (commit: 9472524)：`sync_from_agent()` 引导 memory 与 playground
- [x] **给 meta session 配工具：子 session 管理** (commit: 6a1c5c4)：dream 机制 24h 审查子 session
- [x] **Bridge 子进程标注 + Persistent agent 标识** (commit: d773ed7)
- [x] **分层 memory 调整（单向流）** (commit: 5d895fc)

### Module 4 · Agent 继承系统改进

- [x] **meta session 完备，agent 保留继承标识** (commit: b4fbc50)：link/own/append 字段
- [x] **agent 可更新 parent agent 内容** (commit: 6bf957d)：`propose_parent_update` 接口

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
- [x] **Butterfly 专职 persistent agent 体系** (commit: 369fede)

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
- [x] butterfly_dev 自主领取 track.md 任务 (173e884 v1.3.11)
- [x] butterfly_dev memory 含 track.md 快照 + --inject-memory (37a04d2 v1.3.8)
- [x] butterfly_dev 自动标记 track.md + commit (31244b1)
- [x] butterfly chat 默认 timeout 修复 (95329bd v1.3.7)

### 用户交互

- [x] 任务板用户和 agent 都能看到 (2b907b1 v1.3.3)
- [x] butterfly log SESSION_ID [-n N] (5678b8e v1.3.4)

### Filesystem-as-everything & Tools

- [x] system prompt 过长优化 (7d45608 v1.1.6)
- [x] session memory 分层 (71f9c66 v1.1.7)
- [x] memory recall skill (5dd735b v1.2.3)
- [x] creator mode skill (d86c2b6)
- [x] agent 版本控制 + propose_agent_update review 流程 (3abfba4 v1.2.1 / d239374 v1.3.14)
- [x] agent 自己迭代 tool 和 skill（热插拔）
- [x] skills 分段式 load in + memory layer 60 行自动截断 (3c12fce v1.3.10)
- [x] web search tool（brave + tavily 多 provider）

### Multi-agent

- [x] receptionist agent — 接待 agent (79e89bc v1.3.26)
- [x] multi-agent runtime (1871749 v1.2.2 + 679e482 v1.2.4)

### Runtime Feedback & Token

- [x] 环境与反馈系统 (a26e603 v1.3.18)
- [x] 约束系统 sandbox (272c76b v1.3.19)
- [x] token 追踪 (8c4b494 v1.2.7)
- [x] butterfly token-report (2888655 v1.3.17)
- [x] memory cache 优化 (ee7d6eb v1.2.0)
- [x] task prompt 精简 (0bb3337 v1.2.5)
- [x] state_diff tool (f075de1 v1.3.13)
- [x] Prompt 优化 (693b3a8 v1.3.15)

### CLI Apps for Agent

- [x] 实时通信 — butterfly friends (cf83515 v1.3.22)
- [x] app notification system (59e4375 v1.3.24)
- [x] 看板 butterfly kanban (3902211 v1.3.25)
- [x] Repo as a skill / deepwiki (ffcb72d v1.3.20)
- [x] repo_dev agent (66cfbdf v1.3.21)
