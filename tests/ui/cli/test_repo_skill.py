"""Tests for nutshell repo-skill — codebase overview skill generation."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ui.cli.repo_skill import (
    generate_repo_skill,
    _build_tree,
    _extract_readme_summary,
    _detect_key_files,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a minimal sample repo with README + Python files."""
    repo = tmp_path / "my-project"
    repo.mkdir()

    # README
    (repo / "README.md").write_text(textwrap.dedent("""\
        # My Project

        A sample project for testing repo-skill generation.

        ## Installation

        pip install my-project
    """))

    # pyproject.toml
    (repo / "pyproject.toml").write_text('[project]\nname = "my-project"\n')

    # Source files
    src = repo / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "main.py").write_text("def main(): pass\n")
    (src / "utils.py").write_text("def helper(): pass\n")

    # Tests
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text("def test_main(): pass\n")

    # Noise dirs that should be filtered
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "foo.pyc").write_text("")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "express").mkdir()

    return repo


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """Repo with no README and minimal files."""
    repo = tmp_path / "bare-lib"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n")
    (repo / "Makefile").write_text("all:\n\tpython main.py\n")
    return repo


# ── Tests: generate_repo_skill ────────────────────────────────────────────────

class TestGenerateRepoSkill:
    def test_full_generation(self, sample_repo: Path):
        content = generate_repo_skill(sample_repo)

        # Front matter
        assert "name: my-project-wiki" in content
        assert "description: Knowledge about the my-project codebase" in content

        # Title
        assert "# my-project — Codebase Overview" in content

        # Purpose (from README)
        assert "A sample project for testing repo-skill generation." in content

        # Structure — contains source dirs but not noise
        assert "src/" in content
        assert "tests/" in content
        assert "__pycache__" not in content
        assert ".git" not in content
        assert "node_modules" not in content

        # Key files
        assert "`pyproject.toml`" in content
        assert "`README.md`" in content
        assert "`src/main.py`" in content

    def test_custom_name(self, sample_repo: Path):
        content = generate_repo_skill(sample_repo, name="cool-project")
        assert "name: cool-project-wiki" in content
        assert "# cool-project — Codebase Overview" in content

    def test_no_readme_graceful(self, bare_repo: Path):
        content = generate_repo_skill(bare_repo)

        # Should not crash
        assert "# bare-lib — Codebase Overview" in content
        # Fallback message for no README
        assert "No README found" in content
        # Still detects key files
        assert "`main.py`" in content
        assert "`Makefile`" in content

    def test_nonexistent_path_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            generate_repo_skill(tmp_path / "does-not-exist")


# ── Tests: _build_tree ────────────────────────────────────────────────────────

class TestBuildTree:
    def test_skips_hidden_and_noise(self, sample_repo: Path):
        tree = _build_tree(sample_repo)
        assert "__pycache__" not in tree
        assert ".git" not in tree
        assert "node_modules" not in tree

    def test_shows_files_and_dirs(self, sample_repo: Path):
        tree = _build_tree(sample_repo)
        assert "src/" in tree
        assert "tests/" in tree
        assert "pyproject.toml" in tree

    def test_max_depth(self, tmp_path: Path):
        """Deep nesting should be truncated at max_depth."""
        repo = tmp_path / "deep"
        repo.mkdir()
        d = repo
        for i in range(6):
            d = d / f"level{i}"
            d.mkdir()
            (d / "file.txt").write_text("x")

        tree = _build_tree(repo, max_depth=2)
        assert "level0/" in tree
        assert "level1/" in tree
        # level2 contents should not appear (depth=2 means 2 levels)
        lines = tree.splitlines()
        # Verify we don't see level2's children
        assert not any("level3" in l for l in lines), "Depth limit exceeded"

    def test_max_entries(self, tmp_path: Path):
        """Should cap at max_entries lines."""
        repo = tmp_path / "many"
        repo.mkdir()
        for i in range(100):
            (repo / f"file_{i:03d}.txt").write_text("x")

        tree = _build_tree(repo, max_entries=10)
        lines = tree.strip().splitlines()
        assert len(lines) <= 10


# ── Tests: _extract_readme_summary ────────────────────────────────────────────

class TestExtractReadmeSummary:
    def test_extracts_first_paragraph(self, sample_repo: Path):
        summary = _extract_readme_summary(sample_repo)
        assert "A sample project for testing" in summary
        # Should not include the "Installation" section
        assert "pip install" not in summary

    def test_no_readme(self, bare_repo: Path):
        summary = _extract_readme_summary(bare_repo)
        assert summary == ""

    def test_truncates_long_summary(self, tmp_path: Path):
        repo = tmp_path / "long"
        repo.mkdir()
        (repo / "README.md").write_text("# Title\n\n" + "word " * 200)
        summary = _extract_readme_summary(repo, max_chars=100)
        assert len(summary) <= 105  # 100 + a few chars for "…" and word boundary

    def test_skips_badges(self, tmp_path: Path):
        repo = tmp_path / "badges"
        repo.mkdir()
        (repo / "README.md").write_text(
            "# Title\n\n"
            "[![Build](https://img.shields.io/badge.svg)](https://ci.example.com)\n"
            "![Logo](logo.png)\n\n"
            "The actual description is here.\n"
        )
        summary = _extract_readme_summary(repo)
        assert "actual description" in summary
        assert "badge" not in summary.lower()


# ── Tests: _detect_key_files ──────────────────────────────────────────────────

class TestDetectKeyFiles:
    def test_finds_manifest_and_entry(self, sample_repo: Path):
        files = _detect_key_files(sample_repo)
        paths = [f[0] for f in files]
        assert "pyproject.toml" in paths
        assert "README.md" in paths
        assert "src/main.py" in paths
        assert "src/" in paths
        assert "tests/" in paths

    def test_bare_repo(self, bare_repo: Path):
        files = _detect_key_files(bare_repo)
        paths = [f[0] for f in files]
        assert "main.py" in paths
        assert "Makefile" in paths


# ── Tests: CLI integration ────────────────────────────────────────────────────

class TestCLIIntegration:
    def test_cmd_writes_file(self, sample_repo: Path, tmp_path: Path):
        """Test the cmd_repo_skill function writes SKILL.md."""
        from ui.cli.repo_skill import cmd_repo_skill
        from types import SimpleNamespace

        out_dir = tmp_path / "output"
        args = SimpleNamespace(
            repo_path=str(sample_repo),
            output=str(out_dir),
            name=None,
        )
        rc = cmd_repo_skill(args)
        assert rc == 0

        skill_file = out_dir / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        assert "my-project-wiki" in content

    def test_cmd_custom_name(self, sample_repo: Path, tmp_path: Path):
        from ui.cli.repo_skill import cmd_repo_skill
        from types import SimpleNamespace

        out_dir = tmp_path / "output2"
        args = SimpleNamespace(
            repo_path=str(sample_repo),
            output=str(out_dir),
            name="custom-proj",
        )
        rc = cmd_repo_skill(args)
        assert rc == 0

        content = (out_dir / "SKILL.md").read_text()
        assert "custom-proj-wiki" in content
        assert "# custom-proj — Codebase Overview" in content

    def test_cmd_bad_path(self, tmp_path: Path):
        from ui.cli.repo_skill import cmd_repo_skill
        from types import SimpleNamespace

        args = SimpleNamespace(
            repo_path=str(tmp_path / "nope"),
            output=str(tmp_path / "out"),
            name=None,
        )
        rc = cmd_repo_skill(args)
        assert rc == 1
