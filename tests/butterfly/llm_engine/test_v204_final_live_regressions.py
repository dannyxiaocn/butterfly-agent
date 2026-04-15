"""Live-traffic regressions surfaced against the fix branch (PR #20).

- NEW-5: ``max_output_tokens`` now always sent to the Codex backend after the
         Bug 7 fix — but the ChatGPT-OAuth endpoint rejects that parameter
         with HTTP 400. Every real Codex call against that endpoint 400s,
         which forces the agent loop into the fallback path on every turn
         and surfaces as the user's live screenshot error (auth method not
         resolvable) when the fallback provider has no credentials.

- NEW-6: ``KimiForCodingProvider`` silently constructs with ``api_key=None``
         when neither ``KIMI_FOR_CODING_API_KEY`` nor ``KIMI_API_KEY`` is
         set. The failure mode then shifts to first-request time, where
         the Anthropic SDK raises an opaque ``Could not resolve
         authentication method`` error. A fail-fast constructor check would
         give the user an actionable message before any request fires.
"""
from __future__ import annotations

import os

import pytest

from butterfly.core.types import Message


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-5 (Critical regression): the Bug 7 fix wires ``self.max_tokens`` "
        "into ``body['max_output_tokens']`` unconditionally, but the ChatGPT-OAuth "
        "Codex endpoint rejects that key with 400 'Unsupported parameter: "
        "max_output_tokens'. Reproduced live 3/3 on 2026-04-15. Fix: either "
        "drop ``max_output_tokens`` from the body when the target is the "
        "ChatGPT-OAuth backend, or make the field opt-in (only send when the "
        "caller explicitly sets max_tokens > 0 AND the endpoint is known to "
        "accept it)."
    ),
)
def test_codex_request_body_does_not_send_max_output_tokens_by_default():
    """The default `CodexProvider()` (no explicit max_tokens) should not emit
    a `max_output_tokens` field that the ChatGPT-OAuth backend rejects."""
    from butterfly.llm_engine.providers.codex import CodexProvider, _build_request_body

    prov = CodexProvider()  # default max_tokens=8096
    body = _build_request_body(
        "gpt-5.4",
        "sys",
        [Message(role="user", content="hi")],
        [],
        thinking=False,
        max_output_tokens=prov.max_tokens,  # matches what .complete() does
    )
    assert "max_output_tokens" not in body, (
        f"body sends max_output_tokens={body.get('max_output_tokens')!r}; "
        "ChatGPT-OAuth rejects this with 400"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NEW-6: KimiForCodingProvider() with no KIMI_FOR_CODING_API_KEY and no "
        "KIMI_API_KEY silently constructs with api_key=None; first request then "
        "fails with an opaque Anthropic-SDK error that surfaced in the user's "
        "live screenshot (turn 1 OK via primary, turn 2 fallback to Kimi with "
        "no key → 'Could not resolve authentication method'). Fix: raise a "
        "clear AuthError at __init__ when no credential is resolvable."
    ),
)
def test_kimi_ctor_fails_fast_when_no_api_key_available(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    from butterfly.llm_engine.providers.kimi import KimiForCodingProvider
    from butterfly.llm_engine.errors import AuthError

    with pytest.raises(AuthError):
        KimiForCodingProvider()
