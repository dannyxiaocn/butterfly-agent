from __future__ import annotations

import unittest
from pathlib import Path

import ui


from conftest import REPO_ROOT
DOCS_UI = REPO_ROOT / "docs" / "ui"


class UiSurfaceTest(unittest.TestCase):
    def test_ui_package_has_descriptive_docstring(self) -> None:
        self.assertIn("Web", ui.__doc__ or "")
        self.assertIn("CLI", ui.__doc__ or "")

    def test_ui_docs_describe_cli_and_web(self) -> None:
        text = (DOCS_UI / "impl.md").read_text(encoding="utf-8")
        self.assertIn("cli/", text)
        self.assertIn("web/", text)

    def test_ui_subdir_docs_exist(self) -> None:
        self.assertTrue((DOCS_UI / "cli" / "impl.md").exists())
        self.assertTrue((DOCS_UI / "web" / "impl.md").exists())


if __name__ == "__main__":
    unittest.main()
