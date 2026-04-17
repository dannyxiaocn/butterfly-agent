"""memory_update — edit a sub-memory file and sync its index line in main memory.

Design: v2.0.5 β pattern. Sub-memory under core/memory/*.md is no longer
auto-injected into the prompt; the agent discovers it via one-line index
entries in core/memory.md. This tool enforces that index stays in sync: every
write to a sub-memory also upserts its index line.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_INDEX_HEADER = "## Memory files"


def _sanitize_name(name: str) -> str | None:
    name = name.strip()
    if not name or not _SAFE_NAME_RE.match(name):
        return None
    return name


def _upsert_index_line(main_text: str, name: str, description: str) -> str:
    """Add or replace '- <name>: <description>' under the '## Memory files' section.

    If the section header is missing, append one at the end.
    """
    new_line = f"- {name}: {description}"
    lines = main_text.splitlines()

    # Find the section header line index.
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == _INDEX_HEADER), -1
    )

    if header_idx == -1:
        # Append a fresh section at the end.
        suffix_lines = []
        if lines and lines[-1].strip():
            suffix_lines.append("")
        suffix_lines.extend(["", _INDEX_HEADER, new_line])
        return "\n".join(lines + suffix_lines).rstrip() + "\n"

    # Find the section's end (next header of same or higher level, or EOF).
    section_end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("## ") or stripped.startswith("# "):
            section_end = j
            break

    # Look for existing entry for `name` within the section.
    entry_pattern = re.compile(rf"^\s*-\s+{re.escape(name)}\s*:")
    for j in range(header_idx + 1, section_end):
        if entry_pattern.match(lines[j]):
            lines[j] = new_line
            return "\n".join(lines).rstrip() + "\n"

    # No existing entry — insert just before section_end (keeping blank lines tidy).
    insert_at = section_end
    # Skip trailing blank lines at the end of the section so the new entry
    # sits right after the current last non-blank line.
    while insert_at - 1 > header_idx and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, new_line)
    return "\n".join(lines).rstrip() + "\n"


class MemoryUpdateExecutor:
    def __init__(
        self,
        memory_dir: str | Path | None = None,
        main_memory_path: str | Path | None = None,
    ) -> None:
        self._memory_dir = Path(memory_dir) if memory_dir else None
        self._main_memory_path = Path(main_memory_path) if main_memory_path else None

    async def execute(self, **kwargs: Any) -> str:
        if self._memory_dir is None or self._main_memory_path is None:
            return "Error: memory tools are not wired to this session."

        raw_name = kwargs.get("name", "")
        name = _sanitize_name(str(raw_name))
        if name is None:
            return (
                f"Error: invalid sub-memory name '{raw_name}'. "
                "Use letters, digits, underscore, or hyphen only."
            )

        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        if old_string is None or new_string is None:
            return "Error: old_string and new_string are both required."
        replace_all = bool(kwargs.get("replace_all", False))
        description = kwargs.get("description")
        description = (
            str(description).strip() if isinstance(description, str) and description.strip()
            else None
        )

        self._memory_dir.mkdir(parents=True, exist_ok=True)
        sub_path = self._memory_dir / f"{name}.md"
        is_new = not sub_path.exists()

        # Creating a new sub-memory file: require description.
        if is_new and description is None:
            return (
                f"Error: sub-memory '{name}' does not exist yet. "
                "Provide `description` so its index line can be written to main memory."
            )

        # Write / edit the sub-memory file.
        if is_new:
            if old_string != "":
                return (
                    f"Error: sub-memory '{name}' does not exist. "
                    "To create it, pass old_string='' and the full initial content in new_string."
                )
            sub_path.write_text(new_string, encoding="utf-8")
            edit_msg = f"Created sub-memory '{name}' ({len(new_string.encode())} bytes)."
        else:
            current = sub_path.read_text(encoding="utf-8")
            if old_string == new_string:
                edit_msg = f"No change: old_string and new_string are identical."
            elif old_string == "" and not replace_all:
                return (
                    f"Error: sub-memory '{name}' already exists. "
                    "Provide a non-empty old_string (and use `replace_all=true` to overwrite), "
                    "or pick a different name."
                )
            else:
                if replace_all:
                    count = current.count(old_string) if old_string else 0
                    if old_string == "":
                        # Full-file overwrite.
                        new_text = new_string
                        count = 1
                    elif count == 0:
                        return f"Error: old_string not found in sub-memory '{name}'."
                    else:
                        new_text = current.replace(old_string, new_string)
                    edit_msg = f"Replaced {count} occurrence(s) in sub-memory '{name}'."
                else:
                    count = current.count(old_string)
                    if count == 0:
                        return f"Error: old_string not found in sub-memory '{name}'."
                    if count > 1:
                        return (
                            f"Error: old_string appears {count} times in sub-memory '{name}'. "
                            "Pass `replace_all=true` or supply more context to make it unique."
                        )
                    new_text = current.replace(old_string, new_string, 1)
                    edit_msg = f"Replaced 1 occurrence in sub-memory '{name}'."
                sub_path.write_text(new_text, encoding="utf-8")

        # Upsert the index line in main memory if a description is provided,
        # OR always on first creation (where description is guaranteed).
        index_msg = ""
        if description is not None:
            main_text = (
                self._main_memory_path.read_text(encoding="utf-8")
                if self._main_memory_path.exists() else ""
            )
            updated = _upsert_index_line(main_text, name, description)
            if updated != main_text:
                self._main_memory_path.write_text(updated, encoding="utf-8")
                index_msg = f" Index line upserted in main memory."
            else:
                index_msg = " (index line unchanged)"

        return edit_msg + index_msg
