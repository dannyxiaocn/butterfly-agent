# Guardian — write boundary for sandboxed sub-agents

`butterfly/core/guardian.py` defines `Guardian`, a single-rooted write
boundary used by sub-agent's **explorer** mode. It is plumbed through
`ToolLoader` into the Write, Edit, and Bash executors.

## Contract

`Guardian(root)` resolves `root` once at construction. Three methods:

- `resolve_target(path)` — joins relative paths to root, then `.resolve()`s.
- `is_allowed(path)` — True iff the resolved target is under root.
- `check_write(path)` — raises `PermissionError` if the target is outside.

Symlink escape attempts are blocked because the target is resolved
(`Path.resolve()`) before the `relative_to(root)` check.

## Wiring

When a session's `manifest.json` carries `mode == "explorer"`, `Session`
constructs `Guardian(playground_dir)` in `__init__` and passes it to
`ToolLoader`. The loader then injects it into the `WriteExecutor`,
`EditExecutor`, and `BashExecutor` constructors.

- **Write / Edit**: call `guardian.check_write(resolved_path)` before
  writing. On `PermissionError`, return `Error: guardian: …` to the agent
  so the failure shows up as a normal tool result.
- **Bash**: when a guardian is set, the executor pins the subprocess `cwd`
  to `guardian.root` and exports `BUTTERFLY_GUARDIAN_ROOT` so the mode
  prompt can reference it. Command text is **not** parsed — bash escape
  routes (`cd ..`, heredocs, sub-shells) are too many to enumerate; the
  contract is enforced by the Write/Edit hard blocks plus the prompt rule.

## What this is NOT

- It is not a syscall sandbox (no chroot, landlock, seccomp). A determined
  bash command running as the user can read anything; the boundary is on
  **writes** done through Butterfly tools.
- It is not a per-tool allowlist. Tools that don't take paths
  (`web_search`, `task_create`, etc.) run unchanged.
- It is not a sub-agent isolation primitive on its own — the explorer
  mode prompt (`toolhub/sub_agent/explorer.md`) is the agent-side half of
  the contract.

## See also

- `toolhub/sub_agent/explorer.md` — the prompt that establishes the
  agent-visible side of the boundary.
- `docs/butterfly/tool_engine/design.md` §"Sub-agent tool" — how the
  guardian fits into the broader sub-agent flow.
