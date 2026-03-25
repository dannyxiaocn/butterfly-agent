"""Tests for nutshell repo-dev — dedicated dev-agent session for a repo."""
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from ui.cli.repo_skill import cmd_repo_dev


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Minimal repo with README + pyproject.toml."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    (repo / "README.md").write_text("# My Project\n\nA sample project.\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "my-project"\n')
    (repo / "main.py").write_text("print('hello')\n")
    return repo


def _make_args(repo_path: str, name: str | None = None, message: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(repo_path=repo_path, name=name, message=message)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRepoDev:
    @patch("ui.cli.repo_skill.subprocess.run")
    def test_creates_skill_file(self, mock_run: MagicMock, sample_repo: Path, tmp_path: Path):
        """repo-dev should write a SKILL.md into the new session's core/skills/."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Make `nutshell new` succeed and create the session dir skeleton
        def side_effect(cmd, **kwargs):
            # Find the session_id from the command args
            # cmd = [sys.executable, "-m", "ui.cli.main", "new", session_id, "--entity", ...]
            sid = cmd[4]
            sdir = sessions_dir / sid / "core" / "skills"
            sdir.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        with patch.dict("os.environ", {"NUTSHELL_SESSIONS_DIR": str(sessions_dir)}):
            args = _make_args(str(sample_repo))
            rc = cmd_repo_dev(args)

        assert rc == 0

        # Find the created session directory
        session_dirs = list(sessions_dir.iterdir())
        assert len(session_dirs) == 1
        skill_file = session_dirs[0] / "core" / "skills" / "my-project-wiki" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        assert "my-project-wiki" in content
        assert "A sample project." in content

    @patch("ui.cli.repo_skill.subprocess.run")
    def test_session_id_format(self, mock_run: MagicMock, sample_repo: Path, tmp_path: Path):
        """Session ID should contain 'repo-dev-' + project name."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        captured_session_id = None

        def side_effect(cmd, **kwargs):
            nonlocal captured_session_id
            captured_session_id = cmd[4]
            sdir = sessions_dir / captured_session_id / "core" / "skills"
            sdir.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        with patch.dict("os.environ", {"NUTSHELL_SESSIONS_DIR": str(sessions_dir)}):
            args = _make_args(str(sample_repo))
            cmd_repo_dev(args)

        assert captured_session_id is not None
        assert captured_session_id.startswith("repo-dev-my-project-")

    @patch("ui.cli.repo_skill.subprocess.run")
    def test_default_name_from_path(self, mock_run: MagicMock, sample_repo: Path, tmp_path: Path):
        """When --name is not given, use the directory name."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        captured_session_id = None

        def side_effect(cmd, **kwargs):
            nonlocal captured_session_id
            captured_session_id = cmd[4]
            sdir = sessions_dir / captured_session_id / "core" / "skills"
            sdir.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        with patch.dict("os.environ", {"NUTSHELL_SESSIONS_DIR": str(sessions_dir)}):
            args = _make_args(str(sample_repo))  # name=None → default from path
            cmd_repo_dev(args)

        # Default name is "my-project" (directory name)
        assert "repo-dev-my-project-" in captured_session_id

        # Skill dir should also use default name
        session_dirs = list(sessions_dir.iterdir())
        skill_file = session_dirs[0] / "core" / "skills" / "my-project-wiki" / "SKILL.md"
        assert skill_file.exists()

    @patch("ui.cli.repo_skill.subprocess.run")
    def test_custom_name(self, mock_run: MagicMock, sample_repo: Path, tmp_path: Path):
        """--name flag should override the default project name."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        captured_session_id = None

        def side_effect(cmd, **kwargs):
            nonlocal captured_session_id
            captured_session_id = cmd[4]
            sdir = sessions_dir / captured_session_id / "core" / "skills"
            sdir.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        with patch.dict("os.environ", {"NUTSHELL_SESSIONS_DIR": str(sessions_dir)}):
            args = _make_args(str(sample_repo), name="cool-proj")
            cmd_repo_dev(args)

        assert "repo-dev-cool-proj-" in captured_session_id

        session_dirs = list(sessions_dir.iterdir())
        skill_file = session_dirs[0] / "core" / "skills" / "cool-proj-wiki" / "SKILL.md"
        assert skill_file.exists()

    def test_bad_path_returns_1(self, tmp_path: Path):
        """Non-existent repo path should return exit code 1."""
        args = _make_args(str(tmp_path / "does-not-exist"))
        rc = cmd_repo_dev(args)
        assert rc == 1

    @patch("ui.cli.repo_skill.subprocess.run")
    def test_message_triggers_chat(self, mock_run: MagicMock, sample_repo: Path, tmp_path: Path):
        """When --message is given, a second subprocess.run call sends the chat message."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: nutshell new
                sid = cmd[4]
                sdir = sessions_dir / sid / "core" / "skills"
                sdir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stderr="")
            else:
                # Second call: nutshell chat
                return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        with patch.dict("os.environ", {"NUTSHELL_SESSIONS_DIR": str(sessions_dir)}):
            args = _make_args(str(sample_repo), message="please add tests")
            rc = cmd_repo_dev(args)

        assert rc == 0
        assert call_count == 2

        # Second call should be a chat command with the message
        second_call = mock_run.call_args_list[1]
        cmd_args = second_call[0][0]  # positional arg 0
        assert "chat" in cmd_args
        assert "please add tests" in cmd_args
