"""Live-traffic regressions surfaced against the fix branch (PR #20).

Both bugs fixed in the follow-up commit; the tests are no longer xfail and
stand as ongoing regression coverage.

- NEW-5: ``max_output_tokens`` was always sent to the Codex backend after the
         Bug 7 fix, but the ChatGPT-OAuth endpoint rejects that parameter
         with HTTP 400. Fixed by making the field opt-in: the default
         ``CodexProvider()`` uses ``max_tokens=None``, which skips emission.

- NEW-6: ``KimiForCodingProvider`` silently constructed with ``api_key=None``
         when ``KIMI_FOR_CODING_API_KEY`` was not set. Fixed: the ctor now
         raises ``AuthError`` up-front. (As of v2.0.7 only that one env var
         is honored — the previous ``KIMI_API_KEY`` fallback was removed.)
"""
from __future__ import annotations

import pytest

from butterfly.core.types import Message


def test_codex_request_body_does_not_send_max_output_tokens_by_default():
    """The default `CodexProvider()` (no explicit max_tokens) should not emit
    a `max_output_tokens` field that the ChatGPT-OAuth backend rejects."""
    from butterfly.llm_engine.providers.codex import CodexProvider, _build_request_body

    prov = CodexProvider()  # default max_tokens=None
    body = _build_request_body(
        "gpt-5.4",
        "sys",
        [Message(role="user", content="hi")],
        [],
        thinking=False,
        max_output_tokens=prov.max_tokens,  # matches what .complete() does
    )
    assert "max_output_tokens" not in body, (
        f"body still sends max_output_tokens={body.get('max_output_tokens')!r}; "
        "ChatGPT-OAuth rejects this with 400"
    )


def test_codex_explicit_max_tokens_is_forwarded():
    """When the caller explicitly sets max_tokens, the field is forwarded."""
    from butterfly.llm_engine.providers.codex import CodexProvider, _build_request_body

    prov = CodexProvider(max_tokens=4096)
    body = _build_request_body(
        "gpt-5.4",
        "sys",
        [Message(role="user", content="hi")],
        [],
        thinking=False,
        max_output_tokens=prov.max_tokens,
    )
    assert body["max_output_tokens"] == 4096


def test_kimi_ctor_fails_fast_when_no_api_key_available(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    # Legacy fallback env vars must NOT be honored: setting them should not
    # rescue the ctor. Pin that contract here.
    monkeypatch.setenv("KIMI_API_KEY", "should-be-ignored")
    from butterfly.llm_engine.providers.kimi import KimiForCodingProvider
    from butterfly.llm_engine.errors import AuthError

    with pytest.raises(AuthError) as exc_info:
        KimiForCodingProvider()
    # Message should be actionable — point the user at the one supported env var.
    assert "KIMI_FOR_CODING_API_KEY" in str(exc_info.value)


def test_kimi_ctor_accepts_explicit_api_key(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    from butterfly.llm_engine.providers.kimi import KimiForCodingProvider

    prov = KimiForCodingProvider(api_key="explicit-key")
    assert prov is not None
