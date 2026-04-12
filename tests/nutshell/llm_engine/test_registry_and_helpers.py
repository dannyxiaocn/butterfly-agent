from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from nutshell.core.provider import Provider
from nutshell.core.types import Message
from nutshell.llm_engine.providers.anthropic import _build_system_param
from nutshell.llm_engine.providers.openai_api import _build_messages
from nutshell.llm_engine.registry import provider_name, resolve_provider


class LlmEngineRegistryTest(unittest.TestCase):
    def test_provider_name_reverse_lookup_uses_class_name(self) -> None:
        FakeAnthropic = type(
            "AnthropicProvider",
            (Provider,),
            {
                "complete": lambda self, *args, **kwargs: None,
            },
        )
        self.assertEqual(provider_name(FakeAnthropic()), "anthropic")

    def test_resolve_provider_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            resolve_provider("missing-provider")

    def test_resolve_provider_uses_lazy_import(self) -> None:
        class FakeProvider(Provider):
            async def complete(self, *args, **kwargs):
                raise AssertionError("not used")

        module = types.SimpleNamespace(OpenAIProvider=FakeProvider)
        with patch("importlib.import_module", return_value=module):
            provider = resolve_provider("openai")
        self.assertIsInstance(provider, FakeProvider)

    def test_build_system_param_uses_cache_blocks_when_supported(self) -> None:
        payload = _build_system_param("prefix", "dynamic", True)
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["cache_control"]["type"], "ephemeral")

    def test_openai_message_builder_flattens_tool_messages(self) -> None:
        messages = [
            Message(role="user", content="hello"),
            Message(
                role="assistant",
                content=[
                    {"type": "tool_use", "id": "tc1", "name": "bash", "input": {"command": "pwd"}},
                    {"type": "text", "text": "working"},
                ],
            ),
            Message(
                role="tool",
                content=[{"type": "tool_result", "tool_use_id": "tc1", "content": "ok"}],
            ),
        ]
        built = _build_messages("sys", messages, "prefix")
        self.assertEqual(built[0]["role"], "system")
        self.assertIn("prefix", built[0]["content"])
        tool_entries = [entry for entry in built if entry["role"] == "tool"]
        self.assertEqual(len(tool_entries), 1)
        self.assertEqual(tool_entries[0]["tool_call_id"], "tc1")


if __name__ == "__main__":
    unittest.main()
