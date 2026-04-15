from __future__ import annotations

import pytest
from butterfly.llm_engine import errors
from butterfly.llm_engine.errors import (
    ProviderError,
    AuthError,
    RateLimitError,
    ContextWindowExceededError,
    ProviderTimeoutError,
    ServerError,
    BadRequestError,
)


# ── 4. Direct instantiation and behavior of all error taxonomy classes ───────


def test_provider_error_base() -> None:
    e = ProviderError("base error", provider="anthropic", status=500)
    assert "base error" in str(e)
    assert e.provider == "anthropic"
    assert e.status == 500
    assert isinstance(e, RuntimeError)


def test_auth_error_defaults() -> None:
    e = AuthError("bad credentials", provider="openai")
    assert isinstance(e, ProviderError)
    assert "bad credentials" in str(e)
    assert e.status is None
    assert e.provider == "openai"


def test_rate_limit_error_defaults_and_retry_after() -> None:
    e = RateLimitError("rate limited", provider="openai", retry_after=42.5)
    assert isinstance(e, ProviderError)
    assert "rate limited" in str(e)
    assert e.status == 429
    assert e.retry_after == 42.5
    assert e.provider == "openai"


def test_rate_limit_error_custom_status() -> None:
    e = RateLimitError("rate limited", provider="google", status=503, retry_after=1.0)
    assert e.status == 503


def test_context_window_exceeded_error() -> None:
    e = ContextWindowExceededError("too long", status=413, provider="google")
    assert isinstance(e, ProviderError)
    assert "too long" in str(e)
    assert e.status == 413
    assert e.provider == "google"


def test_timeout_error() -> None:
    e = ProviderTimeoutError("timed out", provider="anthropic")
    assert isinstance(e, ProviderError)
    assert "timed out" in str(e)
    assert e.status is None
    assert e.provider == "anthropic"


def test_server_error() -> None:
    e = ServerError("internal error", status=503, provider="openai")
    assert isinstance(e, ProviderError)
    assert "internal error" in str(e)
    assert e.status == 503
    assert e.provider == "openai"


def test_bad_request_error() -> None:
    e = BadRequestError("bad request", status=400, provider="anthropic")
    assert isinstance(e, ProviderError)
    assert "bad request" in str(e)
    assert e.status == 400
    assert e.provider == "anthropic"


def test_all_errors_exported() -> None:
    for name in errors.__all__:
        assert hasattr(errors, name)
        cls = getattr(errors, name)
        assert issubclass(cls, ProviderError)
