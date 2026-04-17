"""Regression tests for 4 new bugs surfaced while testing the v2.0.4 fix branch.

All four have been fixed on ``fix/llm-engine-v202-bugs`` — these tests now
pass and serve as ongoing regression coverage.

NEW-1  🔴 Critical  — cross-provider fallback leaks reasoning blocks into
                      non-reasoning providers; default agent config breaks.
                      Fixed: anthropic._sanitize_content_for_anthropic and
                      openai_api._build_messages assistant-placeholder branch.
NEW-2  🟠 Medium    — ``_is_codex_compatible_model`` is a blocklist of
                      Anthropic substrings, not a real allow-list.
                      Fixed: switched to explicit allow-list regex.
NEW-3  🟡 Minor     — ``summary=None`` on replayed reasoning block is
                      forwarded to the server as ``null`` (schema expects list).
                      Fixed: ``block.get("summary") or []`` coercion.
NEW-4  🟡 Minor     — ``_stringify_tool_result`` leaks ``dict.__repr__`` for
                      non-text blocks mixed into a tool_result payload.
                      Fixed: explicit placeholder for non-text dict blocks.
"""
from __future__ import annotations

import pytest

from butterfly.core.types import Message


# ── NEW-1 ────────────────────────────────────────────────────────────────────


def test_anthropic_converter_strips_foreign_reasoning_blocks():
    from butterfly.llm_engine.providers.anthropic import _to_api_messages

    msgs = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content=[{"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"}],
        ),
        Message(role="user", content="follow"),
    ]
    out = _to_api_messages(msgs)
    asst = next(m for m in out if m["role"] == "assistant")

    # Desired: the Codex-only reasoning block must not survive into the
    # Anthropic API request; it must be stripped OR replaced with a placeholder.
    if isinstance(asst["content"], list):
        for block in asst["content"]:
            assert not (
                isinstance(block, dict) and block.get("type") == "reasoning"
            ), f"reasoning block leaked: {block!r}"


def test_openai_chat_completions_no_invalid_empty_assistant():
    from butterfly.llm_engine.providers.openai_api import _build_messages

    msgs = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content=[{"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"}],
        ),
        Message(role="user", content="follow"),
    ]
    out = _build_messages("sys", msgs)
    asst = next(m for m in out if m.get("role") == "assistant")

    # OpenAI Chat Completions rejects assistant messages that have neither
    # a non-None ``content`` nor a non-empty ``tool_calls``.
    has_content = asst.get("content") not in (None, "")
    has_tool_calls = bool(asst.get("tool_calls"))
    assert has_content or has_tool_calls, (
        f"assistant entry would be rejected by OpenAI API: {asst}"
    )


# ── NEW-2 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "kimi-for-coding",    # real Kimi model name
        "gemini-pro",         # different provider
        "typo-here",          # typo with no provider prefix
        "gpt-3.5-turbo-0301-deprecated",  # accepted — "gpt-" prefix is allow-list
    ],
)
def test_codex_compat_rejects_non_codex_models(model: str):
    from butterfly.llm_engine.providers.codex import _is_codex_compatible_model

    # Anything not starting with gpt-/o\d+/codex-/ft:gpt- is rejected. The
    # exception is the last parametrize case — a retired gpt-* name still
    # starts with "gpt-" so it is allow-listed; the endpoint may still 400
    # on it, but the filter can't distinguish live vs deprecated names.
    if model.startswith("gpt-"):
        assert _is_codex_compatible_model(model) is True
    else:
        assert _is_codex_compatible_model(model) is False, (
            f"model {model!r} slips through Codex compat check"
        )


# ── NEW-3 ────────────────────────────────────────────────────────────────────


def test_codex_convert_assistant_normalises_null_summary():
    from butterfly.llm_engine.providers.codex import _convert_assistant

    msg = Message(role="assistant", content=[
        {"type": "reasoning", "id": "rs_1", "summary": None, "encrypted_content": "x"},
    ])
    out = _convert_assistant(msg)
    reasoning_item = next((x for x in out if x.get("type") == "reasoning"), None)

    assert reasoning_item is not None
    assert reasoning_item.get("summary") == [], (
        f"summary=None should be normalised to []; got {reasoning_item.get('summary')!r}"
    )


def test_openai_responses_convert_assistant_normalises_null_summary():
    from butterfly.llm_engine.providers.openai_responses import _convert_messages

    out = _convert_messages([
        Message(role="assistant", content=[
            {"type": "reasoning", "id": "rs_1", "summary": None, "encrypted_content": "x"},
        ])
    ])
    reasoning_item = next((x for x in out if x.get("type") == "reasoning"), None)
    assert reasoning_item is not None
    assert reasoning_item.get("summary") == [], (
        f"summary=None should be normalised to []; got {reasoning_item.get('summary')!r}"
    )


# ── NEW-4 ────────────────────────────────────────────────────────────────────


def test_stringify_tool_result_does_not_leak_dict_repr():
    from butterfly.llm_engine.providers.openai_api import _stringify_tool_result

    content = [
        {"type": "text", "text": "a"},
        {"type": "image", "source": {"data": "..."}},
        {"type": "text", "text": "b"},
    ]
    out = _stringify_tool_result(content)
    # Either drop the image block entirely ("ab") or emit a placeholder —
    # but the raw dict keys/values must not appear in the joined string.
    assert "'source'" not in out and "'type': 'image'" not in out, (
        f"dict repr leaked into tool_result output: {out!r}"
    )
