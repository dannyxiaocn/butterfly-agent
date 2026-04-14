# track.md SOP

## Setup (always do first)

```bash
# Ensure workspace exists (local clone of the origin repo)
ls playground/butterfly 2>/dev/null || git clone /Users/xiaobocheng/agent_core/butterfly-agent playground/butterfly
cd playground/butterfly
git checkout main
git pull origin main        # sync latest before starting work
```

All subsequent bash commands run from `playground/butterfly/`.

## Branch naming policy

- Active implementation branch: `wip-<task-slug>`
- Ready-for-review branch: `ready-<task-slug>`

Start on `wip-...`, finish on `ready-...`. Do not push feature work directly to `main`.

## Step 1 — Record task start in memory

Claude Code includes the exact track.md item text in the dispatch message (look for the line starting with `- [ ]`).
Extract it and write it to `work_state.md` so you can find it in Step 6.

```bash
cat > core/memory/work_state.md << 'EOF'
# Work State

## Current Task
<exact task description as dispatched>

## track.md search keyword
<3-5 word fragment from the task description that uniquely identifies the track.md line>

## Last Completed
<previous task if known>
EOF
```

## Step 2 — Read task

1. `cat track.md` to confirm the matching `[ ]` item
2. **Do not self-select.** Claude Code dispatches a specific task — implement exactly that
3. Note the EXACT text of the `[ ]` line — you will need it in Step 6

## Step 3 — Implement

Work entirely inside `playground/butterfly/`.

Create or reset a task branch before editing:

```bash
BRANCH_SLUG="<task-slug>"
git checkout -B "wip-${BRANCH_SLUG}"
```

## Step 4 — Verify

```bash
pytest tests/ -q          # must pass
```

## Step 5 — Version + commit

1. Update `README.md`: section + Changelog entry
2. Bump version in `pyproject.toml` AND `README.md` heading
3. `git commit -m "vX.Y.Z: summary\n\n- bullets\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"`
   Save the commit hash (first 7 chars).

## Step 6 — Update track.md (REQUIRED — do not skip)

Use `sed` or Python to mark the exact matching line. The line text was recorded in Step 1.

```bash
# Method: find the matching [ ] line and replace it with [x] + commit annotation
COMMIT_ID=$(git rev-parse --short HEAD)
VERSION=$(python3 -c "import tomllib; print(tomllib.loads(open('pyproject.toml').read())['project']['version'])")

# Replace the first matching [ ] line — adjust the search string to match EXACTLY
python3 << 'PYEOF'
import re, pathlib

track = pathlib.Path("track.md").read_text()

# Search for the [ ] item that matches the task (use the keyword from work_state)
search = "<paste 3-5 word unique fragment from the task line>"
commit_id = "<COMMIT_ID>"
version = "<VERSION>"

# Find and replace
pattern = rf'(\- \[ \] (?:[^\n]*{re.escape(search)}[^\n]*))'
replacement = rf'\1 <!-- {commit_id} v{version} -->'
new_track, n = re.subn(pattern, lambda m: m.group(0).replace("- [ ]", "- [x]") + f" <!-- {commit_id} v{version} -->", track, count=1)

if n == 0:
    print("WARNING: no match found for search term — check keyword")
else:
    pathlib.Path("track.md").write_text(new_track)
    print(f"Marked done: {commit_id} v{version}")
PYEOF

git add track.md
git commit -m "track: mark <task short name> done (${COMMIT_ID} v${VERSION})"
```

Also add new `[ ]` items for any sub-tasks or missing features discovered during implementation.

## Step 7 — Update entity memory (cross-session persistence)

Update the entity's memory files so future sessions inherit the knowledge:

```bash
# In playground/butterfly/
# 1. Update entity memory.md: bump version in "Recent Changes" section

# 2. Update work_state layer
COMMIT_ID=$(git rev-parse --short HEAD~1)   # feature commit (before track commit)
cat > entity/butterfly_dev/memory/work_state.md << 'EOF'
# Work State

## Current Task
(none — waiting for dispatch)

## track.md search keyword
(none)

## Last Completed
vX.Y.Z: <task description> (commit: COMMIT_ID)
EOF

git add entity/butterfly_dev/memory/ entity/butterfly_dev/memory.md
git commit -m "butterfly_dev: update entity memory after vX.Y.Z"
```

## Step 8 — Push + report

```bash
git branch -M "ready-${BRANCH_SLUG}"
git push -u origin "ready-${BRANCH_SLUG}"
```

Report the feature commit ID (from Step 5) and the `ready-` branch name to Claude Code.

## Step 9 — Update session memory

After pushing, update your session's own core memory to reflect completed state:

```bash
# Write to sessions/<my-id>/core/memory/work_state.md  (path relative to session workdir)
cat > core/memory/work_state.md << 'EOF'
# Work State

## Current Task
(none — done)

## Last Completed
vX.Y.Z: <task> (commit: COMMIT_ID)
EOF
```
