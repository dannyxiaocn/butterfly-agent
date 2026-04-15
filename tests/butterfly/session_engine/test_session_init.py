from __future__ import annotations

import subprocess
import shutil
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butterfly.session_engine.session_init import _create_session_venv, init_session


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError("repo root not found")


class SessionInitUnitTests(unittest.TestCase):
    def test_init_session_stays_inside_custom_bases(self) -> None:
        unique_entity = f"unit_test_entity_{uuid.uuid4().hex}"
        leaked_meta_dir = _repo_root() / "sessions" / f"{unique_entity}_meta"
        try:
            with TemporaryDirectory() as td, patch(
                "butterfly.session_engine.entity_state._create_meta_venv",
                side_effect=lambda p: p / ".venv",
            ), patch(
                "butterfly.session_engine.session_init._create_session_venv",
                side_effect=lambda p: p / ".venv",
            ), patch("butterfly.session_engine.entity_state.start_meta_agent"):
                root = Path(td)
                entity_base = root / "entity"
                sessions_base = root / "sessions"
                system_base = root / "_sessions"
                entity_dir = entity_base / unique_entity
                (entity_dir / "prompts").mkdir(parents=True)
                (entity_dir / "tools.md").write_text("", encoding="utf-8")
                (entity_dir / "skills.md").write_text("", encoding="utf-8")
                (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
                (entity_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
                (entity_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
                (entity_dir / "config.yaml").write_text(
                    "\n".join(
                        [
                            "prompts:",
                            "  system: prompts/system.md",
                            "  task: prompts/task.md",
                            "  env: prompts/env.md",
                            "provider: anthropic",
                            "model: demo",
                        ]
                    ),
                    encoding="utf-8",
                )

                init_session(
                    session_id="demo",
                    entity_name=unique_entity,
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    entity_base=entity_base,
                )

                self.assertTrue((sessions_base / "demo").exists())
                self.assertTrue((sessions_base / f"{unique_entity}_meta").exists())
                self.assertFalse(leaked_meta_dir.exists())
        finally:
            if leaked_meta_dir.exists():
                shutil.rmtree(leaked_meta_dir)


def test_create_session_venv_does_not_accept_incomplete_existing_directory(tmp_path):
    session_dir = tmp_path / "demo"
    session_dir.mkdir()
    venv_path = session_dir / ".venv"

    def fake_run(*args, **kwargs):
        venv_path.mkdir()
        raise subprocess.CalledProcessError(1, args[0])

    with patch("butterfly.session_engine.session_init.subprocess.run", side_effect=fake_run):
        try:
            result = _create_session_venv(session_dir)
        except subprocess.CalledProcessError:
            return

    assert (result / "pyvenv.cfg").exists()


# ── Regression: v2.0.8 first-run model=null race ──────────────────────────────

def test_init_session_config_not_clobbered_by_concurrent_ensure_config(tmp_path):
    """Regression for v2.0.8 bug: when a butterfly-server watcher concurrently
    starts a Session for a new session_id, Session.__init__ calls
    ensure_config() which writes DEFAULT_CONFIG (model=None) into
    sessions/<id>/core/config.yaml. If this happens BEFORE init_session copies
    the real entity/meta config.yaml, init_session's `if not exists()` guard
    skips the copy, leaving model=null persisted on disk.

    Reproduced by simulating the watcher-side write: we pre-create an empty/
    defaults-only config.yaml in the session core/ before init_session runs.
    After init_session, the session's config.yaml MUST carry the entity's
    model and provider, not DEFAULT_CONFIG's null values.
    """
    unique_entity = f"unit_test_entity_{uuid.uuid4().hex}"
    with TemporaryDirectory() as td, patch(
        "butterfly.session_engine.entity_state._create_meta_venv",
        side_effect=lambda p: p / ".venv",
    ), patch(
        "butterfly.session_engine.session_init._create_session_venv",
        side_effect=lambda p: p / ".venv",
    ), patch("butterfly.session_engine.entity_state.start_meta_agent"):
        root = Path(td)
        entity_base = root / "entity"
        sessions_base = root / "sessions"
        system_base = root / "_sessions"

        entity_dir = entity_base / unique_entity
        (entity_dir / "prompts").mkdir(parents=True)
        (entity_dir / "tools.md").write_text("", encoding="utf-8")
        (entity_dir / "skills.md").write_text("", encoding="utf-8")
        (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
        (entity_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
        (entity_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
        (entity_dir / "config.yaml").write_text(
            "\n".join(
                [
                    "prompts:",
                    "  system: prompts/system.md",
                    "  task: prompts/task.md",
                    "  env: prompts/env.md",
                    "provider: codex-oauth",
                    "model: gpt-5.4",
                    "fallback_provider: kimi-coding-plan",
                    "fallback_model: kimi-for-coding",
                ]
            ),
            encoding="utf-8",
        )

        # Simulate the watcher/Session race: a parallel Session.__init__
        # called ensure_config() ahead of init_session's config copy.
        session_id = "first-run-session"
        core_dir = sessions_base / session_id / "core"
        core_dir.mkdir(parents=True)
        from butterfly.session_engine.session_config import ensure_config, read_config
        ensure_config(sessions_base / session_id)  # writes null-model defaults

        init_session(
            session_id=session_id,
            entity_name=unique_entity,
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            entity_base=entity_base,
        )

        cfg = read_config(sessions_base / session_id)
        assert cfg["model"] == "gpt-5.4", (
            f"first-run session must inherit entity model, got {cfg['model']!r}"
        )
        assert cfg["provider"] == "codex-oauth", (
            f"first-run session must inherit entity provider, got {cfg['provider']!r}"
        )
        assert cfg["fallback_model"] == "kimi-for-coding"
        assert cfg["fallback_provider"] == "kimi-coding-plan"


def test_init_session_writes_manifest_after_config_populated(tmp_path):
    """The manifest.json file is the watcher's discovery signal — it must NOT
    be written until sessions/<id>/core/config.yaml carries a real model.
    Otherwise the watcher spawns a Session whose ensure_config() races and
    clobbers the config with DEFAULT_CONFIG (model=None).
    """
    unique_entity = f"unit_test_entity_{uuid.uuid4().hex}"
    with TemporaryDirectory() as td, patch(
        "butterfly.session_engine.entity_state._create_meta_venv",
        side_effect=lambda p: p / ".venv",
    ), patch(
        "butterfly.session_engine.session_init._create_session_venv",
        side_effect=lambda p: p / ".venv",
    ), patch("butterfly.session_engine.entity_state.start_meta_agent"):
        root = Path(td)
        entity_base = root / "entity"
        sessions_base = root / "sessions"
        system_base = root / "_sessions"

        entity_dir = entity_base / unique_entity
        (entity_dir / "prompts").mkdir(parents=True)
        (entity_dir / "tools.md").write_text("", encoding="utf-8")
        (entity_dir / "skills.md").write_text("", encoding="utf-8")
        (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
        (entity_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
        (entity_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
        (entity_dir / "config.yaml").write_text(
            "provider: anthropic\nmodel: claude-demo\n",
            encoding="utf-8",
        )

        real_write_text = Path.write_text
        session_id = "ordering-check"
        observed: dict[str, object] = {"seen": False, "config_model": None}

        def spy_write_text(self, data, *args, **kwargs):
            # Only care about the specific session's manifest, not the meta session's.
            if self.name == "manifest.json" and self.parent.name == session_id:
                sess_cfg = sessions_base / session_id / "core" / "config.yaml"
                observed["seen"] = True
                if sess_cfg.exists():
                    import yaml as _yaml
                    loaded = _yaml.safe_load(sess_cfg.read_text(encoding="utf-8")) or {}
                    observed["config_model"] = loaded.get("model")
            return real_write_text(self, data, *args, **kwargs)

        with patch.object(Path, "write_text", spy_write_text):
            init_session(
                session_id=session_id,
                entity_name=unique_entity,
                sessions_base=sessions_base,
                system_sessions_base=system_base,
                entity_base=entity_base,
            )

        assert observed["seen"], "spy didn't see the session's manifest.json write"
        assert observed["config_model"] == "claude-demo", (
            "manifest.json was written before sessions/<id>/core/config.yaml "
            f"had a real model (observed model: {observed['config_model']!r}) — "
            "watcher would race with init_session."
        )
