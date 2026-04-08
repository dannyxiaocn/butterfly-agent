from __future__ import annotations

import unittest
from pathlib import Path

import ui
from porter_test_support import repo_root_from


REPO_ROOT = repo_root_from(Path(__file__))


class UiSurfaceTest(unittest.TestCase):
    def test_ui_package_has_descriptive_docstring(self) -> None:
        self.assertIn("Web", ui.__doc__ or "")
        self.assertIn("CLI", ui.__doc__ or "")

    def test_ui_readme_describes_cli_and_web(self) -> None:
        text = (REPO_ROOT / "ui" / "README.md").read_text(encoding="utf-8")
        self.assertIn("cli/", text)
        self.assertIn("web/", text)

    def test_ui_subdir_readmes_exist(self) -> None:
        self.assertTrue((REPO_ROOT / "ui" / "cli" / "README.md").exists())
        self.assertTrue((REPO_ROOT / "ui" / "web" / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
