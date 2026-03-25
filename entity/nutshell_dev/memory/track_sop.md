# track.md SOP

## Setup (always do first)

```bash
# Ensure workspace exists (local clone of the origin repo)
ls playground/nutshell 2>/dev/null || git clone /Users/xiaobocheng/agent_core/nutshell playground/nutshell
cd playground/nutshell
git pull origin main        # sync latest before starting work
```

All subsequent bash commands run from `playground/nutshell/`.

## Step 1 — Record task start in memory

```bash
# Update work_state layer so future activations know what's in progress
cat > core/memory/work_state.md << 'EOF'
# Work State

## Current Task
<paste task description here>

## Last Completed
<previous task if known>
EOF
```

## Step 2 — Read task

1. `cat track.md` to see current task board
2. Look for `[ ]` (unchecked) items
3. **Do not self-select.** Claude Code dispatches a specific task — implement exactly that

## Step 3 — Implement

Work entirely inside `playground/nutshell/`.

## Step 4 — Verify

```bash
pytest tests/ -q          # must pass
```

## Step 5 — Version + commit

1. Update `README.md`: section + Changelog entry
2. Bump version in `pyproject.toml` AND `README.md` heading
3. `git commit -m "vX.Y.Z: summary\n\n- bullets\nCo-Authored-By: ..."`

## Step 6 — Update track.md

1. Mark item: `[ ]` → `[x]` with `<!-- COMMIT_ID vX.Y.Z -->`
2. Add new `[ ]` items for sub-tasks / missing features found
3. `git add track.md && git commit -m "track: mark <task> done"`

## Step 7 — Update entity memory (cross-session persistence)

Update the entity's memory files so future sessions inherit the knowledge:

```bash
# In playground/nutshell/
# 1. Update entity memory.md version + recent changes
# (edit entity/nutshell_dev/memory.md to reflect new version and last change)

# 2. Update work_state layer
cat > entity/nutshell_dev/memory/work_state.md << 'EOF'
# Work State

## Current Task
(none — waiting for dispatch)

## Last Completed
vX.Y.Z: <task description> (commit: COMMIT_ID)
EOF

git add entity/nutshell_dev/memory/ && git commit -m "nutshell_dev: update entity memory after vX.Y.Z"
```

## Step 8 — Push + report

```bash
git push origin main
```

Report the feature commit ID to Claude Code.

## Step 9 — Update session memory

After pushing, update your session's own memory so this activation ends clean:

```bash
# Core memory is at sessions/<id>/core/memory.md (relative: core/memory.md from session dir)
# Update it to reflect completed state
```
