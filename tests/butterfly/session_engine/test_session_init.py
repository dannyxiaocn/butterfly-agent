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
        unique_agent = f"unit_test_agent_{uuid.uuid4().hex}"
        leaked_meta_dir = _repo_root() / "sessions" / f"{unique_agent}_meta"
        try:
            with TemporaryDirectory() as td, patch(
                "butterfly.session_engine.agent_state._create_meta_venv",
                side_effect=lambda p: p / ".venv",
            ), patch(
                "butterfly.session_engine.session_init._create_session_venv",
                side_effect=lambda p: p / ".venv",
            ), patch("butterfly.session_engine.agent_state.start_meta_agent"):
                root = Path(td)
                agent_base = root / "agenthub"
                sessions_base = root / "sessions"
                system_base = root / "_sessions"
                agent_dir = agent_base / unique_agent
                (agent_dir / "prompts").mkdir(parents=True)
                (agent_dir / "tools.md").write_text("", encoding="utf-8")
                (agent_dir / "skills.md").write_text("", encoding="utf-8")
                (agent_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
                (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
                (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
                (agent_dir / "config.yaml").write_text(
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
                    agent_name=unique_agent,
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    agent_base=agent_base,
                )

                self.assertTrue((sessions_base / "demo").exists())
                self.assertTrue((sessions_base / f"{unique_agent}_meta").exists())
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
    the real agent/meta config.yaml, init_session's `if not exists()` guard
    skips the copy, leaving model=null persisted on disk.

    Reproduced by simulating the watcher-side write: we pre-create an empty/
    defaults-only config.yaml in the session core/ before init_session runs.
    After init_session, the session's config.yaml MUST carry the agent's
    model and provider, not DEFAULT_CONFIG's null values.
    """
    unique_agent = f"unit_test_agent_{uuid.uuid4().hex}"
    with TemporaryDirectory() as td, patch(
        "butterfly.session_engine.agent_state._create_meta_venv",
        side_effect=lambda p: p / ".venv",
    ), patch(
        "butterfly.session_engine.session_init._create_session_venv",
        side_effect=lambda p: p / ".venv",
    ), patch("butterfly.session_engine.agent_state.start_meta_agent"):
        root = Path(td)
        agent_base = root / "agenthub"
        sessions_base = root / "sessions"
        system_base = root / "_sessions"

        agent_dir = agent_base / unique_agent
        (agent_dir / "prompts").mkdir(parents=True)
        (agent_dir / "tools.md").write_text("", encoding="utf-8")
        (agent_dir / "skills.md").write_text("", encoding="utf-8")
        (agent_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
        (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
        (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
        (agent_dir / "config.yaml").write_text(
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
            agent_name=unique_agent,
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            agent_base=agent_base,
        )

        cfg = read_config(sessions_base / session_id)
        assert cfg["model"] == "gpt-5.4", (
            f"first-run session must inherit agent model, got {cfg['model']!r}"
        )
        assert cfg["provider"] == "codex-oauth", (
            f"first-run session must inherit agent provider, got {cfg['provider']!r}"
        )
        assert cfg["fallback_model"] == "kimi-for-coding"
        assert cfg["fallback_provider"] == "kimi-coding-plan"


def test_init_session_writes_manifest_after_config_populated(tmp_path):
    """The manifest.json file is the watcher's discovery signal — it must NOT
    be written until sessions/<id>/core/config.yaml carries a real model.
    Otherwise the watcher spawns a Session whose ensure_config() races and
    clobbers the config with DEFAULT_CONFIG (model=None).
    """
    unique_agent = f"unit_test_agent_{uuid.uuid4().hex}"
    with TemporaryDirectory() as td, patch(
        "butterfly.session_engine.agent_state._create_meta_venv",
        side_effect=lambda p: p / ".venv",
    ), patch(
        "butterfly.session_engine.session_init._create_session_venv",
        side_effect=lambda p: p / ".venv",
    ), patch("butterfly.session_engine.agent_state.start_meta_agent"):
        root = Path(td)
        agent_base = root / "agenthub"
        sessions_base = root / "sessions"
        system_base = root / "_sessions"

        agent_dir = agent_base / unique_agent
        (agent_dir / "prompts").mkdir(parents=True)
        (agent_dir / "tools.md").write_text("", encoding="utf-8")
        (agent_dir / "skills.md").write_text("", encoding="utf-8")
        (agent_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
        (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
        (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
        (agent_dir / "config.yaml").write_text(
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
                agent_name=unique_agent,
                sessions_base=sessions_base,
                system_sessions_base=system_base,
                agent_base=agent_base,
            )

        assert observed["seen"], "spy didn't see the session's manifest.json write"
        assert observed["config_model"] == "claude-demo", (
            "manifest.json was written before sessions/<id>/core/config.yaml "
            f"had a real model (observed model: {observed['config_model']!r}) — "
            "watcher would race with init_session."
        )


# ── Regression: _needs_seed hardened against non-mapping YAML ──────────────────

def _run_init_with_stub_config(tmp_path, *, stub_content: str) -> dict:
    """Run init_session with a pre-seeded stub config.yaml, return the
    resulting session config as a dict.

    The stub content simulates various ways a racing ensure_config() or a
    hand-edited file could leave the session config.yaml in a non-mapping /
    malformed / empty shape. The fix under test is `_needs_seed`: regardless
    of stub content, init_session should re-seed from the agent config.
    """
    unique_agent = f"unit_test_agent_{uuid.uuid4().hex}"
    with TemporaryDirectory() as td, patch(
        "butterfly.session_engine.agent_state._create_meta_venv",
        side_effect=lambda p: p / ".venv",
    ), patch(
        "butterfly.session_engine.session_init._create_session_venv",
        side_effect=lambda p: p / ".venv",
    ), patch("butterfly.session_engine.agent_state.start_meta_agent"):
        root = Path(td)
        agent_base = root / "agenthub"
        sessions_base = root / "sessions"
        system_base = root / "_sessions"

        agent_dir = agent_base / unique_agent
        (agent_dir / "prompts").mkdir(parents=True)
        (agent_dir / "tools.md").write_text("", encoding="utf-8")
        (agent_dir / "skills.md").write_text("", encoding="utf-8")
        (agent_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
        (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
        (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")
        (agent_dir / "config.yaml").write_text(
            "provider: anthropic\nmodel: claude-demo\n",
            encoding="utf-8",
        )

        session_id = "stub-check"
        core_dir = sessions_base / session_id / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "config.yaml").write_text(stub_content, encoding="utf-8")

        init_session(
            session_id=session_id,
            agent_name=unique_agent,
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            agent_base=agent_base,
        )

        from butterfly.session_engine.session_config import read_config
        return read_config(sessions_base / session_id)


def test_needs_seed_reseeds_when_stub_is_empty(tmp_path):
    """Empty file → safe_load returns None → treat as needs_seed."""
    cfg = _run_init_with_stub_config(tmp_path, stub_content="")
    assert cfg["model"] == "claude-demo"
    assert cfg["provider"] == "anthropic"


def test_needs_seed_reseeds_when_stub_is_yaml_list(tmp_path):
    """YAML list → safe_load returns a list (not a dict) → needs_seed."""
    cfg = _run_init_with_stub_config(tmp_path, stub_content="[]\n")
    assert cfg["model"] == "claude-demo"
    assert cfg["provider"] == "anthropic"


def test_needs_seed_reseeds_when_stub_is_scalar(tmp_path):
    """YAML scalar → safe_load returns a string → needs_seed."""
    cfg = _run_init_with_stub_config(tmp_path, stub_content="just a string\n")
    assert cfg["model"] == "claude-demo"
    assert cfg["provider"] == "anthropic"


def test_needs_seed_reseeds_when_stub_is_invalid_yaml(tmp_path):
    """Genuinely broken YAML → safe_load raises YAMLError → needs_seed."""
    # Unclosed flow mapping raises yaml.YAMLError.
    cfg = _run_init_with_stub_config(tmp_path, stub_content="{unclosed: \n")
    assert cfg["model"] == "claude-demo"
    assert cfg["provider"] == "anthropic"


def test_needs_seed_reseeds_when_stub_has_model_but_no_provider(tmp_path):
    """Stub with model set but provider=None (hypothetical hand-edit) — the
    widened predicate should still trigger a reseed from the agent.
    """
    cfg = _run_init_with_stub_config(
        tmp_path,
        stub_content="model: foo\nprovider: null\n",
    )
    assert cfg["model"] == "claude-demo"
    assert cfg["provider"] == "anthropic"


def test_needs_seed_keeps_populated_stub(tmp_path):
    """Positive control: when the existing config already has both model AND
    provider, init_session must NOT clobber it — this is the idempotency
    guarantee the original `if not exists()` guard was protecting.
    """
    cfg = _run_init_with_stub_config(
        tmp_path,
        stub_content="model: preserved-model\nprovider: preserved-provider\n",
    )
    assert cfg["model"] == "preserved-model"
    assert cfg["provider"] == "preserved-provider"
