"""Tests for the automatic model evaluation system.

Covers:
  - model_eval.py: evaluate_task_complexity() heuristics
  - model_eval.py: suggest_model() provider mappings
  - params.py: auto_model default field
  - session.py tick(): auto_model override integration
"""

import json
import pytest
from pathlib import Path

from nutshell.runtime.model_eval import evaluate_task_complexity, suggest_model
from nutshell.runtime.params import DEFAULT_PARAMS, read_session_params, write_session_params
from nutshell.core.agent import Agent
from nutshell.core.types import AgentResult, TokenUsage, ToolCall


# ── Helpers ────────────────────────────────────────────────────────


class MockProvider:
    """A mock provider that returns pre-configured responses."""

    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


# ── evaluate_task_complexity ───────────────────────────────────────


def test_empty_input_is_simple():
    """Empty or whitespace-only input → simple."""
    assert evaluate_task_complexity("") == "simple"
    assert evaluate_task_complexity("   ") == "simple"
    assert evaluate_task_complexity(None) == "simple"


def test_short_text_no_keywords_is_simple():
    """Short text (<80 words) without complex keywords → simple."""
    assert evaluate_task_complexity("do the thing") == "simple"


def test_short_text_with_simple_keyword():
    """Short text with a simple keyword → simple."""
    assert evaluate_task_complexity("check the server status") == "simple"
    assert evaluate_task_complexity("list all files") == "simple"
    assert evaluate_task_complexity("show me a summary") == "simple"


def test_complex_keyword_overrides_short_length():
    """Complex keyword present → complex, even if text is short."""
    assert evaluate_task_complexity("implement the feature") == "complex"
    assert evaluate_task_complexity("debug this crash") == "complex"
    assert evaluate_task_complexity("refactor the module") == "complex"


def test_all_complex_keywords():
    """Each complex keyword triggers complex classification."""
    keywords = [
        "implement", "architect", "design", "refactor", "migrate",
        "debug", "analyse", "analyze", "investigate", "build",
    ]
    for kw in keywords:
        assert evaluate_task_complexity(f"please {kw} this") == "complex", f"keyword '{kw}' should trigger complex"


def test_all_simple_keywords():
    """Each simple keyword in short text triggers simple classification."""
    keywords = ["check", "list", "status", "ping", "remind", "note", "log", "summary"]
    for kw in keywords:
        assert evaluate_task_complexity(kw) == "simple", f"keyword '{kw}' should trigger simple"


def test_long_text_is_complex():
    """Text with >300 words → complex regardless of keywords."""
    long_text = " ".join(["word"] * 301)
    assert evaluate_task_complexity(long_text) == "complex"


def test_medium_length_no_keywords():
    """80-300 words, no special keywords → medium."""
    medium_text = " ".join(["update"] * 100)
    assert evaluate_task_complexity(medium_text) == "medium"


def test_complex_keyword_case_insensitive():
    """Complex keywords are matched case-insensitively."""
    assert evaluate_task_complexity("IMPLEMENT the feature") == "complex"
    assert evaluate_task_complexity("Design a system") == "complex"
    assert evaluate_task_complexity("Investigate the issue") == "complex"


def test_keyword_requires_word_boundary():
    """Keywords must be whole words, not substrings."""
    # 'analyst' contains 'analyse' prefix but not as a word boundary match
    assert evaluate_task_complexity("ping") == "simple"
    # 'building' should not match 'build' — actually 'build' IS a substring
    # but regex uses \b so 'building' won't match \bbuild\b
    assert evaluate_task_complexity("the building is tall") == "simple"


# ── suggest_model ──────────────────────────────────────────────────


def test_suggest_model_anthropic():
    """Anthropic provider maps to correct models."""
    assert suggest_model("simple", "anthropic") == "claude-haiku-4-5-20251001"
    assert suggest_model("medium", "anthropic") == "claude-sonnet-4-6"
    assert suggest_model("complex", "anthropic") == "claude-opus-4-6"


def test_suggest_model_openai():
    """OpenAI provider maps to correct models."""
    assert suggest_model("simple", "openai") == "gpt-4o-mini"
    assert suggest_model("medium", "openai") == "gpt-4o"
    assert suggest_model("complex", "openai") == "o3"


def test_suggest_model_unknown_provider():
    """Unknown provider returns None (no override)."""
    assert suggest_model("simple", "kimi") is None
    assert suggest_model("complex", "deepseek") is None


def test_suggest_model_none_provider_defaults_anthropic():
    """None provider defaults to anthropic."""
    assert suggest_model("medium", None) == "claude-sonnet-4-6"


def test_suggest_model_case_insensitive_provider():
    """Provider name is case-insensitive."""
    assert suggest_model("simple", "Anthropic") == "claude-haiku-4-5-20251001"
    assert suggest_model("complex", "OpenAI") == "o3"


# ── params.py — auto_model field ──────────────────────────────────


def test_default_params_has_auto_model():
    """DEFAULT_PARAMS includes auto_model=False."""
    assert "auto_model" in DEFAULT_PARAMS
    assert DEFAULT_PARAMS["auto_model"] is False


def test_read_params_auto_model_defaults(tmp_path):
    """read_session_params returns auto_model=False when params.json has no auto_model key."""
    session_dir = tmp_path / "session"
    core = session_dir / "core"
    core.mkdir(parents=True)
    (core / "params.json").write_text("{}", encoding="utf-8")
    params = read_session_params(session_dir)
    assert params["auto_model"] is False


def test_write_params_auto_model(tmp_path):
    """write_session_params can set auto_model=True."""
    session_dir = tmp_path / "session"
    core = session_dir / "core"
    core.mkdir(parents=True)
    (core / "params.json").write_text("{}", encoding="utf-8")
    write_session_params(session_dir, auto_model=True)
    params = read_session_params(session_dir)
    assert params["auto_model"] is True


# ── session.py tick() — auto_model integration ────────────────────


@pytest.mark.asyncio
async def test_tick_auto_model_overrides_to_opus(tmp_path):
    """tick() with auto_model=True overrides model for complex tasks."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("done", [])])
    agent = Agent(provider=provider, model="claude-sonnet-4-6")

    session = Session(
        agent,
        session_id="test-auto-model",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    # Enable auto_model
    write_session_params(session.session_dir, auto_model=True)

    # Write a complex task
    session.tasks_path.write_text("implement a full authentication system with OAuth2", encoding="utf-8")

    result = await session.tick()
    assert result is not None

    # Model should be restored to original after tick
    assert agent.model == "claude-sonnet-4-6"

    # Harness should record the override
    harness = (session.core_dir / "memory" / "harness.md").read_text()
    assert "auto_model_used" in harness
    assert "claude-opus-4-6" in harness


@pytest.mark.asyncio
async def test_tick_auto_model_overrides_to_haiku(tmp_path):
    """tick() with auto_model=True overrides model for simple tasks."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider, model="claude-sonnet-4-6")

    session = Session(
        agent,
        session_id="test-auto-haiku",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, auto_model=True)
    session.tasks_path.write_text("check status", encoding="utf-8")

    result = await session.tick()
    assert result is not None

    # Model restored after tick
    assert agent.model == "claude-sonnet-4-6"

    # Harness records override to haiku
    harness = (session.core_dir / "memory" / "harness.md").read_text()
    assert "auto_model_used" in harness
    assert "claude-haiku-4-5-20251001" in harness


@pytest.mark.asyncio
async def test_tick_auto_model_no_override_when_same(tmp_path):
    """tick() with auto_model=True does NOT override when suggested == current."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("ok", [])])
    # Agent already using sonnet — medium task should suggest sonnet → no override
    agent = Agent(provider=provider, model="claude-sonnet-4-6")

    session = Session(
        agent,
        session_id="test-auto-same",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, auto_model=True)
    # Medium-length task, no complex/simple keywords → medium → sonnet (same as current)
    session.tasks_path.write_text(" ".join(["update"] * 100), encoding="utf-8")

    result = await session.tick()
    assert result is not None
    assert agent.model == "claude-sonnet-4-6"

    # Harness should NOT have auto_model_override
    harness = (session.core_dir / "memory" / "harness.md").read_text()
    assert "auto_model_used" not in harness


@pytest.mark.asyncio
async def test_tick_auto_model_disabled_by_default(tmp_path):
    """tick() does NOT override model when auto_model is False (default)."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider, model="claude-sonnet-4-6")

    session = Session(
        agent,
        session_id="test-auto-off",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    # auto_model defaults to False — complex task should NOT trigger override
    session.tasks_path.write_text("implement everything from scratch", encoding="utf-8")

    result = await session.tick()
    assert result is not None
    assert agent.model == "claude-sonnet-4-6"

    harness = (session.core_dir / "memory" / "harness.md").read_text()
    assert "auto_model_used" not in harness


@pytest.mark.asyncio
async def test_tick_auto_model_restores_on_exception(tmp_path):
    """Model is restored even if tick() raises an exception."""
    from nutshell.runtime.session import Session

    class FailProvider:
        async def complete(self, messages, tools, system_prompt, model, **kwargs):
            raise RuntimeError("API failure")

    agent = Agent(provider=FailProvider(), model="claude-sonnet-4-6")

    session = Session(
        agent,
        session_id="test-auto-exc",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, auto_model=True)
    session.tasks_path.write_text("implement a complex system", encoding="utf-8")

    with pytest.raises(RuntimeError, match="API failure"):
        await session.tick()

    # Model is restored even when an exception occurs
    assert agent.model == "claude-sonnet-4-6"
