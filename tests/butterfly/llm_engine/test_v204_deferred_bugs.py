"""Regression tests for the 6 v2.0.4 deferred bugs (Bugs 1, 25, 26, 28, 29, 30).

These were initially out of scope for the v2.0.4 patch but folded in on
follow-up — see PR #20 thread.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from butterfly.core.types import Message


# ── Bug 1: SSE truncated tail recovery ─────────────────────────────────


@pytest.mark.asyncio
async def test_sse_parser_recovers_trailing_event_without_double_newline():
    """A final event sent without the trailing \\n\\n must still be parsed."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    payloads = [
        b'data: {"type":"response.output_text.delta","delta":"hello "}\n\n',
        # no trailing \n\n — connection drops mid-stream
        b'data: {"type":"response.output_text.delta","delta":"world"}',
    ]

    class _Resp:
        async def aiter_bytes(self):
            for p in payloads:
                yield p

    text, *_ = await _parse_sse_stream(_Resp(), None)
    assert text == "hello world", f"trailing event was lost: {text!r}"


@pytest.mark.asyncio
async def test_sse_parser_silently_drops_malformed_trailing_buffer():
    """If the trailing buffer isn't valid JSON, drop it — don't raise."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    payloads = [
        b'data: {"type":"response.output_text.delta","delta":"only this"}\n\n',
        b'data: {"type":"response.outpu',  # truncated JSON
    ]

    class _Resp:
        async def aiter_bytes(self):
            for p in payloads:
                yield p

    text, *_ = await _parse_sse_stream(_Resp(), None)
    assert text == "only this"


# ── Bug 25: Provider.aclose lifecycle ──────────────────────────────────


@pytest.mark.asyncio
async def test_provider_aclose_default_is_noop():
    from butterfly.core.provider import Provider
    from butterfly.core.types import Message as _Msg, TokenUsage, ToolCall  # noqa: F401

    class _Stub(Provider):
        async def complete(self, messages, tools, system_prompt, model, **kw):
            return "", [], TokenUsage()

    # Should not raise.
    await _Stub().aclose()


@pytest.mark.asyncio
async def test_anthropic_aclose_calls_underlying_client_close():
    """AnthropicProvider.aclose forwards to the SDK client's close()."""
    from butterfly.llm_engine.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100

    closed = {"called": False}

    class _FakeClient:
        async def close(self):
            closed["called"] = True

    provider._client = _FakeClient()
    await provider.aclose()
    assert closed["called"] is True


@pytest.mark.asyncio
async def test_agent_aclose_swallows_individual_provider_errors():
    """One provider's aclose blowing up shouldn't strand the other."""
    from butterfly.core.agent import Agent
    from butterfly.core.provider import Provider
    from butterfly.core.types import TokenUsage

    primary_closed = {"called": False}
    fallback_closed = {"called": False}

    class _BoomProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, **kw):
            return "", [], TokenUsage()

        async def aclose(self):
            primary_closed["called"] = True
            raise RuntimeError("close blew up")

    class _OkProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, **kw):
            return "", [], TokenUsage()

        async def aclose(self):
            fallback_closed["called"] = True

    agent = Agent(provider=_BoomProvider())
    agent._fallback_provider = _OkProvider()

    await agent.aclose()
    assert primary_closed["called"] is True
    assert fallback_closed["called"] is True


# ── Bug 26: streaming preserved across tool iterations ────────────────


@pytest.mark.asyncio
async def test_on_text_chunk_fires_in_every_iteration_of_tool_loop():
    from butterfly.core.agent import Agent
    from butterfly.core.provider import Provider
    from butterfly.core.tool import tool
    from butterfly.core.types import TokenUsage, ToolCall

    @tool(description="Echo")
    async def echo_tool(text: str) -> str:
        return text

    received: list[tuple[int, str]] = []

    class _StreamingProvider(Provider):
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, system_prompt, model, *,
                           on_text_chunk=None, **kw):
            self.calls += 1
            label = f"turn{self.calls}"
            if on_text_chunk:
                on_text_chunk(label)
                received.append((self.calls, label))
            if self.calls == 1:
                return "", [ToolCall(id="t1", name="echo_tool", input={"text": "x"})], TokenUsage()
            return "done", [], TokenUsage()

    agent = Agent(provider=_StreamingProvider(), tools=[echo_tool])
    await agent.run("go", on_text_chunk=lambda chunk: None)

    # Both turns must have observed an on_text_chunk callback.
    assert [c for c, _ in received] == [1, 2], (
        f"on_text_chunk only fired in turns: {[c for c, _ in received]}"
    )


# ── Bug 28: ProviderTimeoutError no longer shadows builtin ─────────────


def test_provider_timeout_error_distinct_from_builtin():
    from butterfly.llm_engine.errors import ProviderTimeoutError, ProviderError

    # The new name does NOT collide with the Python builtin.
    assert ProviderTimeoutError is not TimeoutError
    assert issubclass(ProviderTimeoutError, ProviderError)


def test_old_timeout_error_name_removed():
    """Importing the old class name should fail loudly."""
    import butterfly.llm_engine.errors as errs
    assert not hasattr(errs, "TimeoutError"), (
        "Old butterfly.llm_engine.errors.TimeoutError should have been renamed"
    )


# ── Bug 29: rich __str__/__repr__ on errors ───────────────────────────


def test_provider_error_str_includes_provider_and_status():
    from butterfly.llm_engine.errors import ProviderError

    err = ProviderError("boom", provider="codex-oauth", status=502)
    text = str(err)
    assert "boom" in text
    assert "codex-oauth" in text
    assert "502" in text


def test_provider_error_repr_includes_class_name_and_metadata():
    from butterfly.llm_engine.errors import AuthError

    err = AuthError("nope", provider="openai", status=401)
    r = repr(err)
    assert "AuthError" in r
    assert "openai" in r
    assert "401" in r


def test_rate_limit_error_str_includes_retry_after():
    from butterfly.llm_engine.errors import RateLimitError

    err = RateLimitError("slow down", provider="codex-oauth", retry_after=12.5)
    text = str(err)
    assert "slow down" in text
    assert "retry_after=12.5" in text


# ── Bug 30: unified tool-result block stringification ─────────────────


@pytest.mark.parametrize("provider_module, fn_name", [
    ("butterfly.llm_engine.providers.codex", "_convert_tool_result"),
    ("butterfly.llm_engine.providers.openai_responses", "_convert_tool_result"),
])
def test_convert_tool_result_uses_unified_renderer(provider_module: str, fn_name: str):
    """codex and openai_responses must agree on the output for the same input."""
    import importlib

    mod = importlib.import_module(provider_module)
    convert = getattr(mod, fn_name)

    msg = Message(role="tool", content=[{
        "type": "tool_result",
        "tool_use_id": "tc-z",
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image", "source": {"data": "OPAQUE"}},
            {"type": "text", "text": "b"},
        ],
    }])
    out = convert(msg)
    assert out and out[0].get("output") == "a[image block omitted]b"
    assert "OPAQUE" not in out[0]["output"]
    assert "{'type'" not in out[0]["output"]


def test_openai_chat_completions_stringify_matches_unified_renderer():
    from butterfly.llm_engine.providers._common import stringify_tool_result_content
    from butterfly.llm_engine.providers.openai_api import _stringify_tool_result

    payload = [
        {"type": "text", "text": "x"},
        {"type": "image", "source": {"data": "..."}},
    ]
    assert _stringify_tool_result(payload) == stringify_tool_result_content(payload)
