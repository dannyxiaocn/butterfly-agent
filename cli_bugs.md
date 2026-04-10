# Nutshell CLI Bug Backlog

*Updated: 2026-04-10*

只保留未解决问题，以及待验证的修复方向。

---

## Bug 3: `nutshell log/tasks/visit/prompt-stats` 默认 session 可能落到 meta session，而不是最近聊天 session [低危]

### 现象
不显式传 `session_id` 时，默认 session 来自 `_sort_sessions()[0]`。
当前排序会把运行中的 meta session 和普通聊天 session 混排，因此某些情况下：

```bash
nutshell log
```

看到的是 `<entity>_meta`，而不是用户刚刚聊天的那个 session。

### 根因
`ui/web/sessions.py` 的 `_sort_sessions()` 只按状态优先级和 `last_run_at/created_at` 排序，没有把 meta session 从“默认聊天目标”候选里排除。

### 待验证修复方向
- 给这些“默认取最近 session”的 CLI 命令增加 `exclude_meta=True` 的选择逻辑。
- 或把“最近 session”拆成两套语义：
  `sessions/friends` 保留全量排序，`log/tasks/visit/prompt-stats` 默认优先 non-meta session。

---

## Bug 5: `nutshell entity new -n NAME` 仍会触发交互式 parent 询问 [低危]

### 现象
```bash
nutshell entity new -n my-agent
```

仍会继续询问 `Extend which entity?`；在非交互环境下会直接失败。

### 根因
`-n/--name` 只绕过了名称输入，没有绕过 parent 选择。若未显式给 `--extends` 或 `--standalone`，流程仍会进入 `_ask_parent()`。

### 待验证修复方向
- 增加 `--parent NAME`。
- 或在 `-n NAME` 且未指定 `--extends/--standalone` 时，默认采用 `--extends agent`。

---

## Bug 7: `new` 子命令仍会把 `--session` / `--sess` 静默误解为 `--sessions-base` [高危]

### 现象
当前提交虽然在 top-level parser 上加了 `allow_abbrev=False`，但这不会自动作用到子 parser。
因此：

```bash
nutshell new --session foo
nutshell new --sess foo
```

不会报“未知参数”，而是被解析成：

```text
--sessions-base foo
```

最终 `session_id` 仍为空，CLI 会在错误目录下建 session 或以非常误导的方式失败。

### 已确认复现
在当前代码上直接构造 `new` 子 parser，可得到：

```text
Namespace(..., session_id=None, sessions_base=PosixPath('foo'))
```

### 根因
`ui/cli/main.py` 只在 `main()` 的顶层 `ArgumentParser` 上设置了 `allow_abbrev=False`。
`subparsers.add_parser(...)` 新建的各个子 parser 仍然使用 `allow_abbrev=True` 默认值。

### 待验证修复方向
- 给所有 `subparsers.add_parser(...)` 显式传 `allow_abbrev=False`。
- 或自定义 `parser_class`，统一关闭子命令缩写匹配。

---

## Bug 8: session ID 唯一化只修了 CLI，新建 session 的其他入口仍可能发生同秒碰撞 [高危]

### 现象
当前提交给 `nutshell new` 和 `nutshell chat` 加了 `-<uuid4[:4]>` 后缀，但系统其他入口仍使用秒级时间戳：

- `ui/web/app.py` `POST /api/sessions`
- `ui/web/weixin.py` `/new`
- `nutshell/session_engine/session.py` 默认构造

这意味着“同秒双创建”在系统层面仍然存在。

### 已确认复现
固定 `ui.web.app.datetime.now()` 后，两次 `POST /api/sessions` 返回相同 ID：

```text
status 200 200
ids 2026-04-10_23-30-00 2026-04-10_23-30-00
same True
```

第二次调用不会报冲突，而是复用/覆盖同一个 session。

### 根因
session ID 生成策略没有收敛到统一入口；本次修复只覆盖了 CLI 的两个路径。

### 系统影响
- Web/UI 创建 session 仍可能触发目录碰撞、manifest 覆写、上下文串写。
- 这次 commit 提到的 `.venv` race 风险，并没有在系统层面被彻底消除。

### 待验证修复方向
- 提供统一的 `generate_session_id()` 帮助函数，所有入口共用。
- Web/API 层对重复 `session_id` 返回冲突错误，而不是静默复用。

---

## Bug 9: `_create_session_venv()` 会把真实的 venv 创建失败误判为“并发竞争已成功” [高危]

### 现象
当前提交在 `nutshell/session_engine/session_init.py` 中加入：

- `python -m venv ...` 失败后，
- 只要 `.venv/` 目录已经存在，
- 就直接返回该路径并吞掉异常。

这会把“半途失败留下的半成品目录”误当成成功创建。

### 已确认复现
mock `subprocess.run()` 为：

1. 先创建 `.venv/`
2. 再抛出 `CalledProcessError`

当前函数会返回 `.venv` 路径，同时：

```text
pyvenv.cfg == False
```

说明返回的是损坏环境，而不是完整 venv。

### 根因
异常分支只检查目录存在，不检查 venv 完整性，也不区分“并发创建完成”与“本进程创建失败留下残骸”。

### 系统影响
- `init_session()` 可能在 venv 实际损坏时仍然报告成功。
- 后续 terminal/tool 执行会把损坏 `.venv` 注入 `PATH` / `VIRTUAL_ENV`，导致运行环境不确定。
- 这是本次提交引入的新回归；之前这里会直接失败并暴露错误。

### 待验证修复方向
- 仅在 `pyvenv.cfg` 和解释器文件存在时才接受“竞争者已创建完成”。
- 更稳妥的做法是使用文件锁或临时目录 + 原子 rename，而不是靠异常后目录存在判断成功。
