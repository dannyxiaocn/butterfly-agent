"""Regression tests for 4 new bugs surfaced while testing the v2.0.4 fix branch.

See ``bugs.md`` on ``test/llm-engine-v2.0.4-fix`` for the live reproductions
and full analysis. These tests document expected behavior *after* fix; each is
marked ``xfail(strict=True)`` so CI passes today and forces a follow-up removal
of the marker once the bug is fixed.

NEW-1  🔴 Critical  — cross-provider fallback leaks reasoning blocks into
                      non-reasoning providers; default entity config breaks.
NEW-2  🟠 Medium    — ``_is_codex_compatible_model`` is a blocklist of
                      Anthropic substrings, not a real allow-list.
NEW-3  🟡 Minor     — ``summary=None`` on replayed reasoning block is
                      forwarded to the server as ``null`` (schema expects list).
NEW-4  🟡 Minor     — ``_stringify_tool_result`` leaks ``dict.__repr__`` for
                      non-text blocks mixed into a tool_result payload.
"""
from __future__ import annotations

import pytest

from butterfly.core.types import Message


# ── NEW-1 ────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-1: anthropic._to_api_messages forwards Codex-produced `type=reasoning` "
        "blocks to the Anthropic/Kimi API verbatim; API rejects with 400. Default "
        "entity config (codex-oauth → kimi-coding-plan fallback) breaks whenever "
        "Codex produced a reasoning block before the failure. Fix: filter unknown "
        "block types in the converter and emit a placeholder text block when "
        "the filtered assistant message is otherwise empty."
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-1: openai_api._build_messages drops the reasoning block but emits "
        "assistant with content=None and no tool_calls — OpenAI API rejects "
        "such messages. Fix: either emit an empty-string content placeholder or "
        "skip the assistant message entirely."
    ),
)
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

@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-2: _is_codex_compatible_model is a denylist of 4 Anthropic-family "
        "substrings, so any model name not matching those is classified as "
        "Codex-compatible. Known-incompatible names (Kimi, Gemini, plain typos) "
        "slip through and reach the ChatGPT-OAuth backend → 400. "
        "Fix: convert to an explicit allow-list of `gpt-*` / `o\\d+` patterns."
    ),
)
@pytest.mark.parametrize(
    "model",
    [
        "kimi-for-coding",    # real Kimi model name
        "gemini-pro",         # different provider
        "typo-here",          # typo with no provider prefix
        "gpt-3.5-turbo-0301-deprecated",  # retired OpenAI name
    ],
)
def test_codex_compat_rejects_non_codex_models(model: str):
    from butterfly.llm_engine.providers.codex import _is_codex_compatible_model

    assert _is_codex_compatible_model(model) is False, (
        f"model {model!r} slips through Codex compat check → request reaches "
        "ChatGPT-OAuth backend and 400s"
    )


# ── NEW-3 ────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-3: Codex _convert_assistant uses `block.get('summary', [])` which "
        "returns None (not []) when the key is present with value None. Server "
        "schema expects an array. Fix: `block.get('summary') or []`."
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-3 (twin): openai_responses._convert_assistant has the same "
        "`block.get('summary', [])` bug."
    ),
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

@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-4: openai_api._stringify_tool_result uses str(dict) on non-text "
        "content blocks, leaking Python repr into the tool_result string. Fix: "
        "filter non-text blocks or emit a neutral placeholder."
    ),
)
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
