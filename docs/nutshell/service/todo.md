# Service — Todo

> 来源：PR #4 Round 3 review comment（`refactor/cli-service-layer`）

## Active — IPC 直写迁移

- [ ] **`ui/cli/chat.py:173,206`** — `cmd_chat` 还在 `FileIPC(session.system_dir)` 直接写入。交互命令，改造需要小心
- [ ] **`ui/web/app.py:222-223`** — `/api/sessions/{id}/events` SSE generator 直接 `BridgeSession(system_dir).async_iter_events(...)`，需要定义 streaming service 接口
- [ ] **`ui/web/weixin.py:252-270,377-378`** — 三处直接 `FileIPC(...).append_event()` + `write_session_status(...)` + `BridgeSession(sys_dir)`，事件文案和 service 层不一致

## Active — 架构 Gap

- [ ] `_parse_task_interval` / `_parse_task_timestamp` / `_validate_task_schedule` / `_normalize_task_name` / `_parse_task_status` 从 `ui/web/app.py` 搬入 `tasks_service`/`config_service`（业务校验不该在 HTTP 层）
- [ ] `send_message` 的 meta-session guard 从 route 层搬入 `messages_service`（CLI 接入 send 时要保持保护）
- [ ] `GET /api/sessions/{id}` 的 duplicate params.json 读取（`get_session` 含 `params` + 又调 `get_config`）
- [ ] `service/*` 里大量的 function-body `import` 做一次 hoist（能 hoist 的全 hoist，剩下的加短注释说明为什么要 lazy）
- [ ] 补缺失的 CLI 命令以达成真正的 1:1：`info`, `delete`, `interrupt`, `message send`, `config get/set`, `tasks add/rm`, `hud`
- [ ] 加 layering linter：`tests/test_ui_layering.py`，用 `ast` 静态扫描 `ui/**/*.py`，禁止 import `nutshell.runtime.ipc` / `nutshell.session_engine.session_status` / `nutshell.session_engine.session_params` / `nutshell.runtime.bridge`
- [ ] 前端：把 `_sse_format`、任务 interval 校验这类从 Python 搬到 TS，让 app.py 真的只剩 `route → service(args) → json`

## Future

- [ ] `nutshell/service/` → `nutshell/commands/`，每个文件 = 一个 CLI 命令，CLI 和 Web 都降级成纯分发器。需先完成上面所有 follow-up，独立 PR
