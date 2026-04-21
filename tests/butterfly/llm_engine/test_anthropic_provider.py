from types import SimpleNamespace

import pytest

from butterfly.core.types import Message
from butterfly.llm_engine.model_catalog import ModelSpec
from butterfly.llm_engine.providers import anthropic as anthropic_mod
from butterfly.llm_engine.providers.anthropic import AnthropicProvider


class _FakeStream:
    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def get_final_message(self):
        return self._final_message


@pytest.mark.asyncio
async def test_complete_streams_thinking_via_thinking_hooks_not_text_chunks():
    """Thinking deltas MUST NOT leak into the main on_text_chunk stream.

    v2.0.9 redesign: provider emits on_thinking_start()/on_thinking_end(body)
    around the thinking block. on_text_chunk only receives assistant text.
    """
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="final answer"),
        ]
    )
    events = [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="thinking")),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="reasoning..."),
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="final answer"),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    thinking_starts: list[None] = []
    thinking_bodies: list[str] = []
    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
        on_text_chunk=chunks.append,
        on_thinking_start=lambda: thinking_starts.append(None),
        on_thinking_end=thinking_bodies.append,
    )

    # Assistant-text channel stays clean
    assert chunks == ["final answer"]
    # Thinking lifecycle landed on the dedicated hooks
    assert len(thinking_starts) == 1
    assert thinking_bodies == ["reasoning..."]
    assert content == "final answer"
    assert tool_calls == []


@pytest.mark.asyncio
async def test_complete_emits_thinking_lifecycle_when_stream_has_no_thinking_delta():
    """Non-stream fallback path: final message has a thinking block but the
    stream never yielded one. We still synthesize on_thinking_start +
    on_thinking_end from the final message, and the text chunk is clean.
    """
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="final answer"),
        ]
    )
    events = [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="final answer"),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    thinking_starts: list[None] = []
    thinking_bodies: list[str] = []
    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
        on_text_chunk=chunks.append,
        on_thinking_start=lambda: thinking_starts.append(None),
        on_thinking_end=thinking_bodies.append,
    )

    assert chunks == ["final answer"]
    assert len(thinking_starts) == 1
    assert thinking_bodies == ["reasoning..."]
    assert content == "final answer"
    assert tool_calls == []


@pytest.mark.asyncio
async def test_complete_collects_tool_calls_from_final_message():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text=""),
            SimpleNamespace(type="tool_use", id="tool-1", name="search", input={"q": "abc"}),
        ]
    )
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            create=_async_return(final_message),
        )
    )

    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
    )

    assert content == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tool-1"
    assert tool_calls[0].name == "search"
    assert tool_calls[0].input == {"q": "abc"}


def _async_return(value):
    async def _inner(**kwargs):
        return value

    return _inner


# ---------------------------------------------------------------------------
# Adaptive thinking + cache strategy tests (Phase 1B)
# ---------------------------------------------------------------------------
#
# These exercise the YAML-spec-driven branches added on top of the legacy
# ``enabled`` + single-breakpoint path. Each test injects a handcrafted
# ``ModelSpec`` via monkeypatching ``get_model_spec`` so we never depend on
# ``models.yaml`` contents — the goal is to assert the request payload the
# provider emits given a particular spec, not to validate the YAML parsing
# (which is covered by ``test_model_catalog.py``).


class _KwargsCapture:
    """Stand-in for ``self._client.messages``/``beta.messages`` that records
    whatever kwargs ``.create`` / ``.stream`` was called with.

    Both call paths (``on_text_chunk=None`` → ``.create``, callbacks set →
    ``.stream``) stash into the same ``kwargs`` slot so tests can assert on a
    single dict regardless of which branch the provider took.
    """

    def __init__(self, final_message):
        self.kwargs: dict | None = None
        self._final = final_message

    def _capture_create(self, **kwargs):
        self.kwargs = kwargs

        async def _inner():
            return self._final

        return _inner()

    def _capture_stream(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream([], self._final)

    @property
    def create(self):
        return self._capture_create

    @property
    def stream(self):
        return self._capture_stream


def _build_final_message(text: str = "ok"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _make_spec(**overrides) -> ModelSpec:
    defaults = dict(
        model="claude-test",
        provider="anthropic",
        max_context_tokens=200_000,
        exposes_reasoning_tokens=False,
        default=True,
    )
    defaults.update(overrides)
    return ModelSpec(**defaults)


def _install_provider(monkeypatch, spec):
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 1000
    capture = _KwargsCapture(_build_final_message())
    provider._client = SimpleNamespace(
        messages=capture,
        beta=SimpleNamespace(messages=capture),
    )
    monkeypatch.setattr(
        anthropic_mod, "get_model_spec", lambda m: spec
    )
    return provider, capture


@pytest.mark.asyncio
async def test_adaptive_thinking_emits_adaptive_shape_and_output_config(monkeypatch):
    """Spec with ``thinking_mode=adaptive`` → ``thinking: {type: "adaptive",
    display: "summarized"}`` + ``output_config: {effort: "high"}``, no
    ``betas``, no ``budget_tokens``, no inflated ``max_tokens``.
    """
    spec = _make_spec(
        thinking_mode="adaptive",
        thinking_effort="high",
        thinking_display="summarized",
        interleaved_thinking_beta=False,
    )
    provider, capture = _install_provider(monkeypatch, spec)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        thinking=True,
    )

    kw = capture.kwargs
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kw["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in kw.get("thinking", {})
    assert "betas" not in kw
    # Adaptive path must NOT bump max_tokens (no budget semantics).
    assert kw["max_tokens"] == 1000


@pytest.mark.asyncio
async def test_adaptive_thinking_respects_display_omitted(monkeypatch):
    spec = _make_spec(
        thinking_mode="adaptive",
        thinking_effort="max",
        thinking_display="omitted",
        interleaved_thinking_beta=False,
    )
    provider, capture = _install_provider(monkeypatch, spec)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        thinking=True,
    )

    kw = capture.kwargs
    assert kw["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert kw["output_config"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_legacy_thinking_uses_budget_tokens_and_bumps_max_tokens(monkeypatch):
    """Spec with ``thinking_mode=enabled`` → classic ``{type:"enabled",
    budget_tokens:N}`` shape; ``max_tokens`` bumped to at least budget+1000.
    """
    spec = _make_spec(
        thinking_mode="enabled",
        thinking_budget_tokens=12000,
        interleaved_thinking_beta=True,
    )
    provider, capture = _install_provider(monkeypatch, spec)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        thinking=True,
    )

    kw = capture.kwargs
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 12000}
    assert kw["max_tokens"] >= 13000
    # interleaved_thinking_beta=True → the interleaved-thinking beta header
    # rides along via the ``betas`` kwarg. The provider pops it off internally
    # to pick the ``client.beta.messages`` namespace, then re-merges it into
    # the SDK call — so capture sees it again.
    assert kw.get("betas") == ["interleaved-thinking-2025-05-14"]


@pytest.mark.asyncio
async def test_no_spec_keeps_legacy_behavior(monkeypatch):
    """No YAML spec (unknown model) → existing behavior: enabled + budget +
    interleaved beta header, bumped max_tokens. Nothing adaptive leaks.
    """
    provider, capture = _install_provider(monkeypatch, spec=None)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="unknown-model",
        thinking=True,
        thinking_budget=5000,
    )

    kw = capture.kwargs
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert "output_config" not in kw
    assert kw["max_tokens"] == 6000  # 5000 + 1000
    # Class default ``_thinking_uses_betas=True`` → legacy behavior emits the
    # interleaved-thinking beta header for back-compat with older models.
    assert kw.get("betas") == ["interleaved-thinking-2025-05-14"]


@pytest.mark.asyncio
async def test_cache_strategy_single_with_ttl_1h(monkeypatch):
    """Single strategy + ``cache_ttl=1h`` → one breakpoint on the last
    user/assistant msg carrying ``ttl=1h``.
    """
    spec = _make_spec(cache_strategy="single", cache_ttl="1h")
    provider, capture = _install_provider(monkeypatch, spec)

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="follow-up"),
    ]
    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="sys",
        model="claude-test",
        cache_last_human_turn=True,
    )

    api_msgs = capture.kwargs["messages"]
    # find_cache_breakpoint picks len-2 = index 1 (assistant "reply").
    first_block = api_msgs[1]["content"][0]
    assert first_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # The last user message isn't the anchor (it's the fresh tail), so no
    # cache_control leaks onto it.
    assert not any(
        isinstance(blk, dict) and "cache_control" in blk
        for blk in (api_msgs[2]["content"] if isinstance(api_msgs[2]["content"], list) else [])
    )


@pytest.mark.asyncio
async def test_cache_strategy_two_plus_two_emits_four_breakpoints(monkeypatch):
    """two_plus_two + system prefix → 2 cache_control on system blocks + 2
    on the last two user/assistant messages.
    """
    spec = _make_spec(cache_strategy="two_plus_two", cache_ttl="5m")
    provider, capture = _install_provider(monkeypatch, spec)

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply1"),
        Message(role="user", content="second"),
        Message(role="assistant", content="reply2"),
    ]
    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="dynamic-sys",
        model="claude-test",
        cache_system_prefix="shared-prefix",
        cache_last_human_turn=True,
    )

    kw = capture.kwargs
    # System param is a 2-block list, both marked.
    sys_param = kw["system"]
    assert isinstance(sys_param, list) and len(sys_param) == 2
    assert sys_param[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert sys_param[1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    # Last two user/assistant messages (indices 2 + 3) get breakpoints.
    api_msgs = kw["messages"]
    idx_2_blk = api_msgs[2]["content"][-1]
    idx_3_blk = api_msgs[3]["content"][-1]
    assert idx_2_blk["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert idx_3_blk["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    # Earlier messages unmarked.
    for i in (0, 1):
        content = api_msgs[i]["content"]
        if isinstance(content, list):
            for blk in content:
                assert "cache_control" not in (blk if isinstance(blk, dict) else {})


@pytest.mark.asyncio
async def test_cache_strategy_two_plus_two_falls_back_with_single_message(monkeypatch):
    """Fewer than 2 user/assistant messages → two_plus_two degrades to single
    (1 breakpoint on the lone message — or none if the single strategy can't
    find one).
    """
    spec = _make_spec(cache_strategy="two_plus_two", cache_ttl="5m")
    provider, capture = _install_provider(monkeypatch, spec)

    # Single message → single strategy picks len<2 → returns None → zero
    # per-block breakpoints. Verify no cache_control sneaks onto the message.
    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="claude-test",
        cache_last_human_turn=True,
    )

    api_msgs = capture.kwargs["messages"]
    content = api_msgs[0]["content"]
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                assert "cache_control" not in blk
    # System side still gets a breakpoint (legacy single-with-prefix shape).


@pytest.mark.asyncio
async def test_cache_strategy_auto_emits_top_level_cache_control(monkeypatch):
    """auto → ``cache_control`` at the request top level; zero per-block
    breakpoints anywhere.
    """
    spec = _make_spec(cache_strategy="auto", cache_ttl="5m")
    provider, capture = _install_provider(monkeypatch, spec)

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="tail"),
    ]
    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="sys",
        model="claude-test",
        cache_system_prefix="prefix",
        cache_last_human_turn=True,
    )

    kw = capture.kwargs
    # Top-level cache_control present and TTL-carrying.
    assert kw["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    # No per-block cache_control on messages.
    for api_msg in kw["messages"]:
        content = api_msg["content"]
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    assert "cache_control" not in blk, (
                        f"auto strategy leaked a per-block breakpoint on {api_msg['role']!r}"
                    )

    # No per-block cache_control on system param either.
    if isinstance(kw["system"], list):
        for blk in kw["system"]:
            assert "cache_control" not in blk, (
                "auto strategy leaked a per-block breakpoint on system"
            )


@pytest.mark.asyncio
async def test_no_spec_emits_legacy_cache_control_shape(monkeypatch):
    """Regression pin: with no YAML spec, the single-strategy fallback must
    emit the exact ``{type: "ephemeral"}`` dict — no ``ttl`` key leaks
    through. Guards against accidental default-ttl insertion in the refactor.
    """
    provider, capture = _install_provider(monkeypatch, spec=None)

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="tail"),
    ]
    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="sys",
        model="unknown-model",
        cache_system_prefix="prefix",
        cache_last_human_turn=True,
    )

    kw = capture.kwargs
    # System prefix keeps the legacy exact-shape dict.
    sys_param = kw["system"]
    assert isinstance(sys_param, list)
    assert sys_param[0]["cache_control"] == {"type": "ephemeral"}

    # Last user/assistant anchor (index 1) keeps the legacy exact-shape.
    api_msgs = kw["messages"]
    anchor_blk = api_msgs[1]["content"][0]
    assert anchor_blk["cache_control"] == {"type": "ephemeral"}

    # No top-level cache_control kwarg (auto is not the default).
    assert "cache_control" not in kw
