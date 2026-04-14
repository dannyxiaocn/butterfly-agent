"""Normalized error taxonomy for LLM providers.

Provider implementations should raise these when the underlying API returns
a recognizable error class. This gives callers (session loop, retry wrappers,
UI) a single `except` tree instead of distinguishing provider-specific types.
"""
from __future__ import annotations


class ProviderError(RuntimeError):
    """Base for all provider-side failures."""

    def __init__(self, message: str, *, provider: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


class AuthError(ProviderError):
    """401/403 — invalid or expired credentials."""


class RateLimitError(ProviderError):
    """429 — quota or rate limit exceeded. May carry a retry_after hint."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status=status)
        self.retry_after = retry_after


class ContextWindowExceededError(ProviderError):
    """Input (or input+output) exceeded the model's context window."""


class TimeoutError(ProviderError):  # noqa: A001 - shadowing builtin is intentional for taxonomy
    """Request timed out client-side or server-side."""


class ServerError(ProviderError):
    """5xx / transient server-side failure."""


class BadRequestError(ProviderError):
    """400 — malformed request. Not retryable."""


__all__ = [
    "ProviderError",
    "AuthError",
    "RateLimitError",
    "ContextWindowExceededError",
    "TimeoutError",
    "ServerError",
    "BadRequestError",
]
