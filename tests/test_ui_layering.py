from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
UI_ROOT = REPO_ROOT / "ui"
FORBIDDEN_MODULES = {
    "nutshell.runtime.ipc",
    "nutshell.runtime.bridge",
    "nutshell.session_engine.session_status",
    "nutshell.session_engine.session_params",
}


def _is_forbidden(module_name: str) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_MODULES
    )


def test_ui_layer_does_not_import_runtime_or_session_storage_modules() -> None:
    violations: list[str] = []

    for path in sorted(UI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(REPO_ROOT)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        violations.append(f"{rel_path}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if _is_forbidden(node.module):
                    imported = ", ".join(alias.name for alias in node.names)
                    violations.append(f"{rel_path}:{node.lineno} from {node.module} import {imported}")

    assert not violations, "UI layer must depend on nutshell.service instead of runtime/session storage modules:\n" + "\n".join(violations)
