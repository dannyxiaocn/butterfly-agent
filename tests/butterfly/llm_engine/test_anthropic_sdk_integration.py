"""Real-SDK Pydantic validation tests for the Anthropic provider.

The rest of ``test_anthropic_provider.py`` uses a ``_KwargsCapture`` fake that
accepts ``**kwargs``, so any drift between the shapes we emit and the shapes
the real ``anthropic`` SDK's Pydantic request model accepts goes unnoticed
until the first live call crashes. This module fills that gap.

Approach
--------
We instantiate a real :class:`anthropic.AsyncAnthropic` client wired to an
``httpx.MockTransport`` that returns a minimal-but-valid ``Message`` JSON stub.
Because the SDK still validates the full request body through its Pydantic
model before handing it to the transport, any unknown kwarg / wrong-shape
field raises at the client boundary — never reaches the mock. So a successful
round-trip through the mock is proof that the kwargs are SDK-accepted.

Invariant: if the SDK stops accepting any of these shapes (kwarg removal,
field rename, Pydantic-model tightening), this test fails loudly on the
pre-commit run. The unit tests built on ``_KwargsCapture`` stay untouched.

Covered request shapes:
  * adaptive thinking kwargs (``_apply_thinking_kwargs`` — ``output_config``
    first-class, ``thinking={"type":"adaptive",...}``)
  * ``cache_strategy="auto"`` (top-level ``cache_control``)
  * ``cache_strategy="two_plus_two"`` (4 per-block breakpoints + 2 system
    breakpoints)
  * legacy ``thinking={"type":"enabled",...}`` + ``budget_tokens``
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from butterfly.core.types import Message
from butterfly.llm_engine.model_catalog import ModelSpec
from butterfly.llm_engine.providers import anthropic as anthropic_mod
from butterfly.llm_engine.providers.anthropic import (
    AnthropicProvider,
    _apply_cache_strategy,
    _apply_thinking_kwargs,
)

import anthropic as _anthropic_sdk


_MINIMAL_MESSAGE_STUB: dict[str, Any] = {
    "id": "msg_test_integration",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}


def _build_mock_client_and_captured_body() -> tuple[_anthropic_sdk.AsyncAnthropic, dict]:
    """Return an ``AsyncAnthropic`` wired to a MockTransport + a dict that the
    handler populates with the JSON body of the outgoing request.

    The transport always returns ``_MINIMAL_MESSAGE_STUB``. Validation happens
    inside the SDK's Pydantic model BEFORE the handler is invoked — if the
    kwargs are malformed, the handler never runs and the exception surfaces
    to the test.
    """
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_MINIMAL_MESSAGE_STUB)

    transport = httpx.MockTransport(_handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = _anthropic_sdk.AsyncAnthropic(api_key="sk-test-integration", http_client=http_client)
    return client, captured


def _make_spec(**overrides) -> ModelSpec:
    defaults = dict(
        model="claude-test-integration",
        provider="anthropic",
        max_context_tokens=200_000,
        exposes_reasoning_tokens=False,
        default=True,
    )
    defaults.update(overrides)
    return ModelSpec(**defaults)


def _build_thinking_kwargs(
    spec: ModelSpec,
    *,
    thinking_uses_betas: bool = True,
) -> dict[str, Any]:
    """Reproduce the shape ``AnthropicProvider`` would emit — exercising the
    production ``_apply_thinking_kwargs`` helper so we catch drift in that
    helper, not just the shape I type into the test.

    ``thinking_uses_betas`` mirrors the class attr; the legacy-enabled branch
    only emits the first-class ``thinking`` kwarg when this is True (else it
    stuffs the hint into ``extra_body`` for Kimi's gateway).
    """
    kwargs: dict[str, Any] = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": "you are a helpful assistant",
        "messages": [{"role": "user", "content": "hi"}],
    }
    _apply_thinking_kwargs(
        kwargs,
        spec=spec,
        thinking_effort="high",
        thinking_budget=8000,
        supports_adaptive=True,
        thinking_uses_betas=thinking_uses_betas,
        max_tokens_floor=1000,
    )
    return kwargs


def _build_cache_kwargs(spec: ModelSpec, messages: list[Message]) -> dict[str, Any]:
    """Run the cache-strategy dispatcher the same way ``complete`` does."""
    api_messages, system_param, cache_extra = _apply_cache_strategy(
        spec,
        messages=messages,
        cache_system_prefix="shared-prefix-long-and-stable",
        cache_last_human_turn=True,
        supports_cache=True,
        system_prompt="dynamic-system-body",
    )
    kwargs: dict[str, Any] = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": system_param,
        "messages": api_messages,
    }
    kwargs.update(cache_extra)
    return kwargs


# ───────────────────────────────────────────────────────────────────────────
# Adaptive thinking — ``output_config`` + ``thinking: {"type":"adaptive"}``
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_accepts_adaptive_thinking_kwargs():
    """Pydantic round-trip: kwargs produced by ``_apply_thinking_kwargs`` with
    ``mode=adaptive`` land in the wire body without a TypeError.
    """
    spec = _make_spec(
        thinking_mode="adaptive",
        thinking_effort="high",
        thinking_display="summarized",
        interleaved_thinking_beta=False,
    )
    kwargs = _build_thinking_kwargs(spec)
    # Sanity: the helper produced the shapes we're testing for.
    assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kwargs["output_config"] == {"effort": "high"}

    client, captured = _build_mock_client_and_captured_body()
    resp = await client.messages.create(**kwargs)

    # Round-trip happened (no TypeError from the Pydantic model).
    assert type(resp).__name__ == "Message"
    body = captured["body"]
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Adaptive path does NOT carry budget_tokens on the wire.
    assert "budget_tokens" not in body.get("thinking", {})


@pytest.mark.asyncio
async def test_real_sdk_accepts_adaptive_with_display_omitted_and_effort_max():
    """``display=omitted`` + ``effort=max`` — both valid on Anthropic Opus 4.7.
    Guards against the Pydantic enum for ``output_config.effort`` tightening
    and rejecting ``"max"``.
    """
    spec = _make_spec(
        thinking_mode="adaptive",
        thinking_effort="max",
        thinking_display="omitted",
        interleaved_thinking_beta=False,
    )
    kwargs = _build_thinking_kwargs(spec)
    assert kwargs["output_config"] == {"effort": "max"}

    client, captured = _build_mock_client_and_captured_body()
    await client.messages.create(**kwargs)
    body = captured["body"]
    assert body["output_config"] == {"effort": "max"}
    assert body["thinking"]["display"] == "omitted"


# ───────────────────────────────────────────────────────────────────────────
# Legacy thinking — ``{"type":"enabled", "budget_tokens": N}``
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_accepts_legacy_enabled_thinking_kwargs():
    """Legacy shape must still work — guards against an SDK upgrade that
    drops ``{type: "enabled", budget_tokens}`` in favour of ``adaptive`` only.
    """
    spec = _make_spec(
        thinking_mode="enabled",
        thinking_budget_tokens=8000,
        interleaved_thinking_beta=False,
    )
    # ``thinking_uses_betas=True`` routes the legacy branch to the first-class
    # ``thinking`` kwarg (vs. Kimi's ``extra_body`` fallback) — this is what
    # proper Anthropic providers do.
    kwargs = _build_thinking_kwargs(spec, thinking_uses_betas=True)
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8000}

    client, captured = _build_mock_client_and_captured_body()
    await client.messages.create(**kwargs)
    body = captured["body"]
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8000}


# ───────────────────────────────────────────────────────────────────────────
# Cache strategy — ``auto`` (top-level ``cache_control``)
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_accepts_cache_strategy_auto_top_level_control():
    """``cache_strategy=auto`` emits ``cache_control`` at the REQUEST top
    level. This is the shape that crashes on anthropic<0.83 (Pydantic model
    didn't have the field yet).
    """
    spec = _make_spec(cache_strategy="auto", cache_ttl="5m")
    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="follow-up"),
    ]
    kwargs = _build_cache_kwargs(spec, msgs)
    # Sanity: helper produced the shape we're validating.
    assert kwargs["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    client, captured = _build_mock_client_and_captured_body()
    await client.messages.create(**kwargs)
    body = captured["body"]
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


# ───────────────────────────────────────────────────────────────────────────
# Cache strategy — ``two_plus_two`` (4 per-block breakpoints + 2 system)
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_accepts_cache_strategy_two_plus_two_breakpoints():
    """Four per-block ``cache_control`` breakpoints + two on system blocks
    is the maximum Anthropic allows per request. Verifies the SDK's
    Pydantic model accepts ``cache_control`` on both the text-block level
    and the system-block level simultaneously.
    """
    spec = _make_spec(cache_strategy="two_plus_two", cache_ttl="5m")
    msgs = [
        Message(role="user", content="turn1-user"),
        Message(role="assistant", content="turn1-assist"),
        Message(role="user", content="turn2-user"),
        Message(role="assistant", content="turn2-assist"),
    ]
    kwargs = _build_cache_kwargs(spec, msgs)

    # Sanity pre-check: exactly two system-block breakpoints + two msg ones.
    sys_param = kwargs["system"]
    assert isinstance(sys_param, list) and len(sys_param) == 2
    assert all("cache_control" in b for b in sys_param)
    # Two msg-level breakpoints (last two user/assistant messages).
    msg_breakpoints = [
        m for m in kwargs["messages"]
        if isinstance(m["content"], list)
        and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])
    ]
    assert len(msg_breakpoints) == 2

    client, captured = _build_mock_client_and_captured_body()
    await client.messages.create(**kwargs)
    body = captured["body"]
    # Wire body should still carry both system breakpoints + two msg ones.
    assert isinstance(body["system"], list) and len(body["system"]) == 2
    assert all(b.get("cache_control") for b in body["system"])
    wire_msg_bps = [
        m for m in body["messages"]
        if isinstance(m["content"], list)
        and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])
    ]
    assert len(wire_msg_bps) == 2


# ───────────────────────────────────────────────────────────────────────────
# End-to-end: full provider plus real SDK (one test to pin them end-to-end).
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_provider_complete_adaptive_plus_auto_end_to_end(monkeypatch):
    """Full ``AnthropicProvider.complete`` call against a MockTransport'd real
    SDK client, with ``thinking_mode=adaptive`` AND ``cache_strategy=auto``
    active simultaneously — the exact config defaults-only Sonnet 4.6 agents
    were hitting on first call before this fix.
    """
    spec = _make_spec(
        thinking_mode="adaptive",
        thinking_effort="high",
        thinking_display="summarized",
        interleaved_thinking_beta=False,
        cache_strategy="auto",
        cache_ttl="5m",
    )
    monkeypatch.setattr(anthropic_mod, "get_model_spec", lambda m: spec)

    client, captured = _build_mock_client_and_captured_body()
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 1000
    provider._client = client

    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hello")],
        tools=[],
        system_prompt="sys-body",
        model="claude-sonnet-4-6",
        cache_system_prefix="stable-prefix",
        cache_last_human_turn=True,
        thinking=True,
    )

    # We reached the mock → the SDK's Pydantic model accepted the full
    # emitted kwargs payload. No TypeError, no ValidationError.
    assert content == "ok"
    body = captured["body"]
    # Both critical new kwargs made it onto the wire.
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
