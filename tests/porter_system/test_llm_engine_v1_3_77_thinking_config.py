import pytest

from nutshell.session_engine.session_params import DEFAULT_PARAMS
from nutshell.llm_engine.providers.anthropic import AnthropicProvider


class DummyMessagesAPI:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        class Usage:
            input_tokens = 1
            output_tokens = 1
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class Response:
            content = []
            usage = Usage()

        return Response()


class DummyClient:
    def __init__(self):
        self.messages = DummyMessagesAPI()


@pytest.mark.asyncio
async def test_anthropic_thinking_enabled_adds_beta_and_thinking_block():
    provider = AnthropicProvider(api_key="test")
    provider._client = DummyClient()

    await provider.complete(
        messages=[],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        thinking=True,
        thinking_budget=9000,
    )

    call = provider._client.messages.calls[0]
    assert call["betas"] == ["interleaved-thinking-2025-05-14"]
    assert call["thinking"] == {"type": "enabled", "budget_tokens": 9000}
    assert call["max_tokens"] >= 10000


@pytest.mark.asyncio
async def test_anthropic_thinking_disabled_omits_beta_and_thinking_block():
    provider = AnthropicProvider(api_key="test")
    provider._client = DummyClient()

    await provider.complete(
        messages=[],
        tools=[],
        system_prompt="sys",
        model="claude-test",
    )

    call = provider._client.messages.calls[0]
    assert "betas" not in call
    assert "thinking" not in call


def test_default_params_include_thinking_fields():
    assert DEFAULT_PARAMS["thinking"] is False
    assert DEFAULT_PARAMS["thinking_budget"] == 8000


# ── Kimi thinking ─────────────────────────────────────────────────────────────

from nutshell.llm_engine.providers.kimi import KimiForCodingProvider


@pytest.mark.asyncio
async def test_kimi_thinking_enabled_uses_extra_body_not_betas():
    provider = KimiForCodingProvider(api_key="test")
    provider._client = DummyClient()

    await provider.complete(
        messages=[],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        thinking_budget=9000,
    )

    call = provider._client.messages.calls[0]
    # Kimi uses extra_body, not betas
    assert "betas" not in call
    assert "thinking" not in call
    assert call.get("extra_body") == {"thinking": {"type": "enabled"}}
    assert call["max_tokens"] >= 10000


@pytest.mark.asyncio
async def test_kimi_thinking_disabled_omits_extra_body():
    provider = KimiForCodingProvider(api_key="test")
    provider._client = DummyClient()

    await provider.complete(
        messages=[],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    call = provider._client.messages.calls[0]
    assert "betas" not in call
    assert "thinking" not in call
    assert "extra_body" not in call
