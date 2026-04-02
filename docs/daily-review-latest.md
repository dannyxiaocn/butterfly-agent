# Daily Review — 2026-04-02

## Recent Changes (last 5 commits)

- **v1.3.46**: Dream mechanism redesigned — meta session IS the dream. `start_meta_agent()` creates `_sessions/<entity>_meta/` so the watcher runs it as a persistent agent (6h heartbeat). `dream.py` deleted; `nutshell dream ENTITY` now simply sends a message to the meta session via FileIPC. `_META_SYSTEM_PROMPT` and `_META_HEARTBEAT_PROMPT` written to meta `core/` only when empty (entity prompts take precedence).
- **v1.3.45**: Meta session lifecycle foundations (later superseded by v1.3.46 redesign).
- **v1.3.44**: Gene feature — `gene:` field in `agent.yaml`; commands run in meta playground on first init with `core/.gene_initialized` marker; `nutshell meta ENTITY --init` re-runs.
- **662dda1**: Added `insights/` directory (6 deep-dive markdown docs from Claude Code learnings).
- **37a9a07**: Web UI refinements — meta session hidden from chat list; session UX improvements.

## Code Review Findings

### ✅ No issues

- **762 tests passing** (`pytest tests/ -q` — 61s)
- No `TODO`/`FIXME`/`HACK` comments in any source files
- No bare `except:` clauses in runtime code
- `_agent_lock` in `Session` correctly prevents heartbeat/user-message races
- `compute_meta_diffs()` correctly skips diffs where entity side is empty (prevents false positives from built-in meta prompts)
- `_resolve_entity_tools_dir()` correctly walks `extends` chain for inherited tools

### ⚠️ Minor observations (no code changes needed)

1. **`ensure_meta_session()` always creates `core/memory/` dir** (line 77 of `meta_session.py`), even when entity has no memory files. This dir is mostly harmless but could be removed since `init_session()` now conditionally creates it. Low priority.

2. **`sync_from_entity()` has redundant check** (lines 340–344): reads `meta_memory.read_text()` twice — once for `if` and once in the `not meta_memory.read_text()` guard. No bug, minor inefficiency.

3. **`start_meta_agent()` manifest.json only writes once** (idempotent via `if not manifest_path.exists()`), but `created_at` will be stale on restarts. Intentional design — manifest is considered immutable after creation.

4. **`_AUTO_EXPIRE_HOURS = 5`** in `watcher.py` auto-expires stopped sessions after 5h. Meta sessions are persistent (`persistent: True`) so they won't be stopped, but the constant could be documented.

## Actions Taken

- Generated `CLAUDE.md` at repo root (project context file for Claude Code)
- No code changes required — codebase is in good health
