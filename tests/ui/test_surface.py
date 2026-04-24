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

    def test_ui_subdir_design_docs_exist(self) -> None:
        # Phase 10 kept design.md per subdir; impl.md/todo.md were purged.
        self.assertTrue((DOCS_UI / "cli" / "design.md").exists())
        self.assertTrue((DOCS_UI / "web" / "design.md").exists())


if __name__ == "__main__":
    unittest.main()
