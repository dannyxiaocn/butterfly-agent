# Design Notes

## Claude Code — `.claude` 目录设计

### 概览

Claude Code 的配置和状态分两个层级存储：**全局用户级**（`~/.claude/`）和**项目级**（`<project>/.claude/`）。两者都是纯文件，无数据库。

---

## 全局用户级：`~/.claude/`

跨所有项目生效，用户身份的一部分。

### 静态配置（值得跨机器同步）

| 路径 | 内容 |
|------|------|
| `settings.json` | 全局配置：模型、插件、statusline 等 |
| `settings.local.json` | 权限 allowlist（`permissions.allow` 数组），Claude Code 每次用户批准新命令后自动追加 |
| `CLAUDE.md` | 全局系统提示补充，每个 session 都注入 |
| `skills/` | 用户级 skill `.md` 文件，全局可用 |
| `plans/` | `/plan` 命令生成的计划文件 |
| `claude-hud.sh` / `statusline-command.sh` | statusline 脚本 |
| `plugins/installed_plugins.json` | 已安装插件清单 |

### 动态运行时数据（机器本地，不同步）

| 路径 | 内容 |
|------|------|
| `projects/<path>/` | 按项目路径命名的目录，每个 session 一个 UUID 子目录，存对话历史 `.jsonl` |
| `projects/<path>/memory/MEMORY.md` | **auto-memory**：Claude 自动维护的跨 session 记忆，每次对话时注入上下文 |
| `history.jsonl` | 全局对话日志 |
| `todos/` | 各 session 的 task 状态 |
| `sessions/` | 活跃 session 引用 |
| `debug/`, `telemetry/`, `usage-data/` | 调试日志、遥测、用量统计 |
| `backups/` | `~/.claude.json` 的滚动备份 |

### 关键设计点：auto-memory

`projects/<path>/memory/MEMORY.md` 是最有价值的数据之一：
- Claude Code 在每次对话开始时自动读取，注入 system prompt
- Claude 可以在对话中随时更新它（通过 Write 工具）
- 用于积累跨 session 的项目/用户上下文
- **按项目路径隔离**：路径变化则 memory 不同（如换机器用不同 username）

---

## 项目级：`<project>/.claude/`

只对当前项目的 Claude Code session 生效。

### 结构

| 路径 | 内容 |
|------|------|
| `settings.local.json` | 项目专属权限 allowlist，同样由用户批准命令时自动追加 |
| `CLAUDE.md` | 项目专属系统提示补充（优先级高于全局 `~/.claude/CLAUDE.md`） |

### 特点

- `settings.local.json` 是**会话驱动积累**的：没有手写，完全由用户批准行为生成。内容是具体的 Bash 命令模式，高度绑定该项目的工作流
- 可能包含敏感数据（API key 在权限字符串里硬编码是已知问题）
- 通常在项目 `.gitignore` 里，不进 repo

---

## 两级对比

| 维度 | `~/.claude/` | `<project>/.claude/` |
|------|-------------|----------------------|
| 作用范围 | 全局，所有项目 | 仅当前项目 |
| 核心价值 | memory、skills、settings | 项目专属权限 |
| 跨机器同步 | 有意义（可建私有 repo） | 通常不需要 |
| 敏感性 | 低（无 API key） | 高（可能有硬编码 key） |
| 手动维护 | 部分（skills、plans、CLAUDE.md） | 几乎全自动生成 |

---

## 对 nutshell 的启示

nutshell 的 session 数据（`sessions/<id>/`）与 Claude Code 的 `~/.claude/projects/` 在设计思路上类似：
- 都是文件系统 IPC，无数据库
- 都按 ID 隔离 session 数据
- 都有"静态实体定义"（nutshell 的 `entity/`）和"动态运行时状态"（nutshell 的 `sessions/`）分离

未来可以迭代的方向：
- nutshell agent 的 `sessions/<id>/prompts/memory.md` 类比 Claude Code 的 auto-memory 机制
- nutshell 的 `entity/<name>/` 类比 Claude Code 的 `skills/` + `CLAUDE.md` 组合
- Claude Code 的 `settings.local.json` 权限积累模式可以参考用于 nutshell 的动态能力授权设计
