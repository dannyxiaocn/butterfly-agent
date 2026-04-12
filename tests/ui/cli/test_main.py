from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ui.cli.main import _parse_inject_memory, _write_inject_memory


class CliUnitTests(unittest.TestCase):
    def test_parse_inject_memory_supports_inline_values_and_files(self) -> None:
        with TemporaryDirectory() as td:
            temp = Path(td)
            memory_file = temp / "track.md"
            memory_file.write_text("file content", encoding="utf-8")

            result = _parse_inject_memory(["foo=bar", f"track=@{memory_file}"])

        self.assertEqual(result, {"foo": "bar", "track": "file content"})

    def test_parse_inject_memory_rejects_missing_file(self) -> None:
        with self.assertRaises(SystemExit):
            _parse_inject_memory(["track=@/definitely/missing.md"])

    def test_write_inject_memory_creates_layer_files(self) -> None:
        with TemporaryDirectory() as td:
            session_dir = Path(td)
            _write_inject_memory(session_dir, {"notes": "hello"})

            self.assertEqual((session_dir / "core" / "memory" / "notes.md").read_text(encoding="utf-8"), "hello")

