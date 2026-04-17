"""Pin HUD endpoint behavior for v2.0.17.

Covers the new ``display_model`` resolution added in PR #33:
* explicit ``model`` in config wins.
* null model → provider's ``default_model`` from the catalog, surfaced
  as ``"<model> (provider default)"``.
* unknown provider / missing catalog entry falls back to ``None``
  rather than crashing the HUD endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

from butterfly.service.hud_service import get_hud


def _seed(tmp_path: Path, *, model: str | None, provider: str | None) -> tuple[Path, Path]:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    session_dir = sessions_base / "s1" / "core"
    sys_dir = sys_base / "s1"
    session_dir.mkdir(parents=True)
    sys_dir.mkdir(parents=True)
    (sys_dir / "manifest.json").write_text(
        json.dumps({"agent": "agent"}), encoding="utf-8"
    )
    cfg: dict = {"name": "t"}
    if model is not None:
        cfg["model"] = model
    if provider is not None:
        cfg["provider"] = provider
    (session_dir / "config.yaml").write_text(
        "\n".join(f"{k}: {v}" for k, v in cfg.items()), encoding="utf-8"
    )
    return sessions_base, sys_base


def test_hud_uses_explicit_model_without_provider_default_suffix(tmp_path: Path) -> None:
    sessions_base, sys_base = _seed(tmp_path, model="claude-opus-4-7", provider="anthropic")
    hud = get_hud("s1", sessions_base, sys_base)
    assert hud["model"] == "claude-opus-4-7"


def test_hud_resolves_provider_default_when_model_null(tmp_path: Path) -> None:
    """Null model + known provider → look up default_model from catalog."""
    sessions_base, sys_base = _seed(tmp_path, model=None, provider="codex-oauth")
    hud = get_hud("s1", sessions_base, sys_base)
    # codex-oauth catalog entry has default_model = "gpt-5.4"
    assert hud["model"] == "gpt-5.4 (provider default)"


def test_hud_handles_unknown_provider_gracefully(tmp_path: Path) -> None:
    sessions_base, sys_base = _seed(tmp_path, model=None, provider="imaginary-provider")
    hud = get_hud("s1", sessions_base, sys_base)
    assert hud["model"] is None


def test_hud_handles_null_model_and_null_provider(tmp_path: Path) -> None:
    sessions_base, sys_base = _seed(tmp_path, model=None, provider=None)
    hud = get_hud("s1", sessions_base, sys_base)
    assert hud["model"] is None
