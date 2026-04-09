import pytest

from nutshell.session_engine.session_params import DEFAULT_PARAMS
from nutshell.llm_engine.providers.anthropic import AnthropicProvider


class DummyStream:
    def __init__(self, owner, kwargs):
        self._owner = owner
        self._kwargs = kwargs

    async def __aenter__(self):
        self._owner.stream_calls.append(self._kwargs)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        async def _gen():
            if False:
                yield None

        return _gen()

    async def get_final_message(self):
        class Usage:
            input_tokens = 1
            output_tokens = 1
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class Response:
            content = []
            usage = Usage()

        return Response()


class DummyMessagesAPI:
    def __init__(self):
        self.calls = []
        self.stream_calls = []

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

    def stream(self, **kwargs):
        return DummyStream(self, kwargs)


class DummyBetaNamespace:
    """Mirrors the client.beta.messages namespace used when betas=[...] is set."""
    def __init__(self):
        self.messages = DummyMessagesAPI()


class DummyClient:
    def __init__(self):
        self.messages = DummyMessagesAPI()
        self.beta = DummyBetaNamespace()


@pytest.mark.asyncio
async def test_anthropic_thinking_enabled_routes_to_beta_messages():
    """When thinking=True, calls go to client.beta.messages with betas kwarg."""
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

    # Must land on beta.messages, not regular messages
    assert provider._client.messages.calls == [], "regular messages should not be called"
    call = provider._client.beta.messages.calls[0]
    assert call["betas"] == ["interleaved-thinking-2025-05-14"]
    assert call["thinking"] == {"type": "enabled", "budget_tokens": 9000}
    assert call["max_tokens"] >= 10000


@pytest.mark.asyncio
async def test_anthropic_thinking_stream_routes_to_beta_messages():
    provider = AnthropicProvider(api_key="test")
    provider._client = DummyClient()

    streamed_chunks: list[str] = []
    await provider.complete(
        messages=[],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        thinking=True,
        thinking_budget=9000,
        on_text_chunk=streamed_chunks.append,
    )

    assert provider._client.messages.stream_calls == []
    stream_call = provider._client.beta.messages.stream_calls[0]
    assert stream_call["betas"] == ["interleaved-thinking-2025-05-14"]
    assert stream_call["thinking"] == {"type": "enabled", "budget_tokens": 9000}
    assert stream_call["max_tokens"] >= 10000


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
