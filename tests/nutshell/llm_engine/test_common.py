from __future__ import annotations

import unittest

from nutshell.llm_engine.providers._common import _parse_json_args


class CommonProviderTests(unittest.TestCase):
    def test_parse_json_args_accepts_objects(self) -> None:
        self.assertEqual(_parse_json_args('{"x": 1}'), {"x": 1})

    def test_parse_json_args_rejects_non_objects(self) -> None:
        self.assertEqual(_parse_json_args('["x"]'), {})
        self.assertEqual(_parse_json_args('"x"'), {})
        self.assertEqual(_parse_json_args("1"), {})

