"""nutshell repo-skill — generate a SKILL.md codebase overview from any repo.

Pure filesystem operations, no LLM calls.  Fast and offline.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

# Directories to skip when building the tree
_SKIP_DIRS: set[str] = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs", "dist",
    "build", "egg-info", ".idea", ".vscode", ".DS_Store",
    "target",  # Rust
    "vendor",  # Go (optional, but often noisy)
    ".next",   # Next.js
}

# Well-known project manifests (filename → description)
_MANIFEST_FILES: dict[str, str] = {
    "pyproject.toml":    "Python project manifest",
    "setup.py":          "Python setup script",
    "setup.cfg":         "Python setup config",
    "package.json":      "Node.js project manifest",
    "Cargo.toml":        "Rust project manifest",
    "go.mod":            "Go module definition",
    "requirements.txt":  "Python dependencies",
    "Pipfile":           "Python (Pipenv) dependencies",
    "Makefile":          "Build automation",
    "Dockerfile":        "Container build definition",
    "docker-compose.yml": "Docker Compose config",
    "docker-compose.yaml": "Docker Compose config",
    ".env.example":      "Environment variable template",
    "tsconfig.json":     "TypeScript config",
}

# Well-known entry-point files
_ENTRY_FILES: dict[str, str] = {
    "main.py":    "Python entry point",
    "app.py":     "Python application entry",
    "main.go":    "Go entry point",
    "index.ts":   "TypeScript entry point",
    "index.js":   "JavaScript entry point",
    "main.rs":    "Rust entry point (src/)",
    "lib.rs":     "Rust library entry (src/)",
    "manage.py":  "Django management script",
}

# Well-known source directories
_KEY_DIRS: dict[str, str] = {
    "src":    "Source code",
    "lib":    "Library code",
    "tests":  "Test suite",
    "test":   "Test suite",
    "docs":   "Documentation",
    "scripts": "Utility scripts",
    "bin":    "Executables",
    "cmd":    "Go command packages",
    "pkg":    "Go packages",
    "internal": "Go internal packages",
}


# ── Tree builder ──────────────────────────────────────────────────────────────

def _build_tree(root: Path, *, max_depth: int = 3, max_entries: int = 50) -> str:
    """Build a directory-tree string (like `tree`), pure Python.

    Skips directories in _SKIP_DIRS.  Stops at *max_depth* levels and
    *max_entries* total lines.
    """
    lines: list[str] = []
    count = 0

    def _walk(dirpath: Path, prefix: str, depth: int) -> None:
        nonlocal count
        if count >= max_entries:
            return

        try:
            entries = sorted(dirpath.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return

        # Filter out skipped dirs and hidden files at top level readability
        visible: list[Path] = []
        for e in entries:
            if e.name in _SKIP_DIRS:
                continue
            if e.name.startswith(".") and e.name not in (".env.example",):
                continue
            visible.append(e)

        for i, entry in enumerate(visible):
            remaining = len(visible) - i
            if count >= max_entries or (count == max_entries - 1 and remaining > 1):
                if remaining > 0:
                    lines.append(f"{prefix}... ({remaining} more)")
                    count += 1
                return

            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            count += 1

            if entry.is_dir() and depth < max_depth:
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


# ── README extraction ─────────────────────────────────────────────────────────

def _extract_readme_summary(repo: Path, max_chars: int = 500) -> str:
    """Extract the first non-heading, non-empty paragraph from README.md.

    Returns empty string if no README is found.
    """
    for name in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        readme = repo / name
        if readme.exists():
            break
    else:
        return ""

    try:
        text = readme.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    # Walk lines, skip headings / badges / blank lines, collect first paragraph
    paragraph_lines: list[str] = []
    in_paragraph = False

    for line in text.splitlines():
        stripped = line.strip()

        # Skip headings
        if stripped.startswith("#"):
            if in_paragraph:
                break  # end of first paragraph
            continue

        # Skip badge lines (markdown images at top)
        if stripped.startswith("[![") or stripped.startswith("!["):
            continue

        # Skip HTML comments
        if stripped.startswith("<!--"):
            continue

        # Skip horizontal rules
        if stripped in ("---", "***", "___"):
            if in_paragraph:
                break
            continue

        # Blank line
        if not stripped:
            if in_paragraph:
                break  # end of paragraph
            continue

        # Content line
        in_paragraph = True
        paragraph_lines.append(stripped)

    result = " ".join(paragraph_lines)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0] + "…"
    return result


# ── Key files detection ───────────────────────────────────────────────────────

def _detect_key_files(repo: Path) -> list[tuple[str, str]]:
    """Return list of (relative_path, description) for key files found."""
    found: list[tuple[str, str]] = []

    # README
    for name in ("README.md", "readme.md", "README.rst", "README"):
        if (repo / name).exists():
            found.append((name, "Project documentation"))
            break

    # Manifests
    for name, desc in _MANIFEST_FILES.items():
        if (repo / name).exists():
            found.append((name, desc))

    # Entry points — search in root and src/
    for name, desc in _ENTRY_FILES.items():
        if (repo / name).exists():
            found.append((name, desc))
        elif (repo / "src" / name).exists():
            found.append((f"src/{name}", desc))

    # Key directories
    for name, desc in _KEY_DIRS.items():
        if (repo / name).is_dir():
            found.append((f"{name}/", desc))

    return found


# ── SKILL.md generation ───────────────────────────────────────────────────────

def generate_repo_skill(
    repo_path: str | Path,
    *,
    name: str | None = None,
    max_depth: int = 3,
    max_entries: int = 50,
) -> str:
    """Generate SKILL.md content for the given repository.

    Returns the full Markdown string.
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo}")

    repo_name = name or repo.name
    skill_name = f"{repo_name}-wiki"

    # Gather data
    summary = _extract_readme_summary(repo)
    tree = _build_tree(repo, max_depth=max_depth, max_entries=max_entries)
    key_files = _detect_key_files(repo)

    # Build SKILL.md
    parts: list[str] = []

    # Front matter
    parts.append("---")
    parts.append(f"name: {skill_name}")
    parts.append(f"description: Knowledge about the {repo_name} codebase — structure, key files, and purpose")
    parts.append("---")
    parts.append("")

    # Title
    parts.append(f"# {repo_name} — Codebase Overview")
    parts.append("")

    # Purpose
    parts.append("## Purpose")
    if summary:
        parts.append(summary)
    else:
        parts.append(f"*(No README found — explore `{repo_name}/` for details)*")
    parts.append("")

    # Structure
    parts.append("## Structure")
    parts.append("```")
    parts.append(f"{repo_name}/")
    parts.append(tree)
    parts.append("```")
    parts.append("")

    # Key Files
    if key_files:
        parts.append("## Key Files")
        for fpath, desc in key_files:
            parts.append(f"- `{fpath}` — {desc}")
        parts.append("")

    return "\n".join(parts)


# ── CLI entry point ───────────────────────────────────────────────────────────

def cmd_repo_skill(args) -> int:
    """CLI handler for `nutshell repo-skill`."""
    repo_path = Path(args.repo_path).resolve()
    if not repo_path.is_dir():
        import sys
        print(f"Error: not a directory: {repo_path}", file=sys.stderr)
        return 1

    name = args.name or repo_path.name

    try:
        content = generate_repo_skill(
            repo_path,
            name=name,
        )
    except Exception as exc:
        import sys
        print(f"Error generating repo skill: {exc}", file=sys.stderr)
        return 1

    # Determine output path
    if args.output:
        out_dir = Path(args.output)
    else:
        # Default: try to find current session's core/skills/ directory
        # Walk up from cwd looking for core/skills/
        cwd = Path.cwd()
        session_skills = None

        # Check if we're inside a session directory
        for parent in [cwd] + list(cwd.parents):
            candidate = parent / "core" / "skills"
            if candidate.is_dir():
                session_skills = candidate
                break

        if session_skills:
            out_dir = session_skills / f"{name}-wiki"
        else:
            out_dir = Path.cwd() / f"{name}-wiki"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "SKILL.md"
    out_file.write_text(content, encoding="utf-8")

    print(f"Generated: {out_file}")
    print(f"Skill name: {name}-wiki")
    return 0
