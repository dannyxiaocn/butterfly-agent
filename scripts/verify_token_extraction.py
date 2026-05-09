"""Verify provider token extraction against live APIs.

Runs Codex (gpt-5.5) and Kimi (kimi-for-coding) against a fixed short prompt,
monkey-patches the extraction helpers to log the raw usage dict, and prints
both raw-provider-shape and our TokenUsage-shape side-by-side so we can
confirm input / output / cache_read / reasoning fields are extracted correctly.

Runs 3 identical calls per provider to exercise prompt-cache behavior.

Not part of the test suite — ad-hoc verification. Delete after confirmation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.providers import codex as codex_mod
from butterfly.llm_engine.providers import kimi as kimi_mod
from butterfly.llm_engine.providers import openai_api as openai_mod

# ---------------------------------------------------------------------------
# Monkey-patch extraction helpers to capture raw usage for side-by-side print
# ---------------------------------------------------------------------------

_captured: list[dict[str, Any]] = []

_orig_codex_extract = codex_mod._extract_usage
_orig_openai_extract = openai_mod._extract_usage_from_obj


def _capture_codex(u: dict[str, Any]) -> TokenUsage:
    _captured.append({"provider": "codex", "raw": dict(u)})
    return _orig_codex_extract(u)


def _capture_openai(u: Any) -> TokenUsage:
    raw = {}
    for attr in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
    ):
        v = getattr(u, attr, None)
        if v is not None:
            raw[attr] = v
    pd = getattr(u, "prompt_tokens_details", None)
    if pd:
        raw["prompt_tokens_details"] = {
            "cached_tokens": getattr(pd, "cached_tokens", None),
        }
    cd = getattr(u, "completion_tokens_details", None)
    if cd:
        raw["completion_tokens_details"] = {
            "reasoning_tokens": getattr(cd, "reasoning_tokens", None),
        }
    _captured.append({"provider": "openai-compat", "raw": raw})
    return _orig_openai_extract(u)


codex_mod._extract_usage = _capture_codex
openai_mod._extract_usage_from_obj = _capture_openai
# Kimi imports the extractor at module-load time, so the binding in kimi_mod
# is a frozen reference — rebind it too or Kimi calls skip capture.
kimi_mod._extract_usage_from_obj = _capture_openai


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_one(provider, model: str, label: str, n: int = 3) -> None:
    print(f"\n{'=' * 70}")
    print(f"### {label} ({model})")
    print(f"{'=' * 70}")

    prompt = (
        "You are a test assistant. Respond with exactly the single word: "
        "ACKNOWLEDGED. No punctuation, no explanation."
    )
    messages = [Message(role="user", content=prompt)]

    for i in range(1, n + 1):
        _captured.clear()
        try:
            text, tool_calls, usage = await provider.complete(
                messages=messages,
                tools=[],
                system_prompt="You follow instructions exactly.",
                model=model,
                cache_system_prefix="verify-token-extraction-v1",
                cache_last_human_turn=True,
                thinking=False,
            )
        except Exception as e:
            print(f"call #{i}: FAILED — {type(e).__name__}: {e}")
            continue

        raw = _captured[-1] if _captured else {"raw": "<not captured>"}
        print(f"\ncall #{i}")
        print(f"  reply           : {text!r}")
        print(f"  raw usage       : {json.dumps(raw['raw'], indent=2, default=str)}")
        print(f"  TokenUsage      : input={usage.input_tokens} output={usage.output_tokens} "
              f"cache_read={usage.cache_read_tokens} cache_write={usage.cache_write_tokens} "
              f"reasoning={usage.reasoning_tokens}")
        print(f"  context_tokens  : {usage.input_tokens + usage.cache_read_tokens} "
              f"(= input + cache_read; total tokens sent as prompt)")
        print(f"  total_tokens    : {usage.total_tokens} (= input + output, billing surrogate)")


async def main() -> None:
    from butterfly.llm_engine.providers.codex import CodexProvider
    from butterfly.llm_engine.providers.kimi import KimiOpenAIProvider

    tasks = []

    try:
        codex_provider = CodexProvider()
        tasks.append(("codex-oauth", codex_provider, "gpt-5.5"))
    except Exception as e:
        print(f"[skip codex] {type(e).__name__}: {e}")

    try:
        kimi_provider = KimiOpenAIProvider()
        tasks.append(("kimi-coding-plan", kimi_provider, "kimi-for-coding"))
    except Exception as e:
        print(f"[skip kimi] {type(e).__name__}: {e}")

    if not tasks:
        print("No providers configured; aborting.")
        return

    for label, provider, model in tasks:
        await run_one(provider, model, label)


if __name__ == "__main__":
    asyncio.run(main())
