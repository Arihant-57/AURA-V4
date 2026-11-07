"""
Unit tests for AURA V4 Git MCP Server:
- Git repository detection in subdirectories (rev-parse --is-inside-work-tree).
- Typing import verification (`from typing import Any`).
- Proper error reporting for non-git directories.
"""

import os
import subprocess
import tempfile
import pytest
from pathlib import Path

import aura.mcp.git_server as git_server
from aura.mcp.git_server import (
    git_status,
    git_branch,
    git_log,
    git_diff,
    git_ls_files,
    run_git,
)


@pytest.fixture
def temp_git_repo_with_subdir(tmp_path: Path):
    """Create a temporary git repository with a committed file and subdirectories."""
    repo = tmp_path / "main_repo"
    repo.mkdir()

    # Initialize Git repository
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@aura.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AURA Test"], cwd=repo, check=True, capture_output=True)

    # Create root file and commit
    (repo / "root.txt").write_text("root file\n", encoding="utf-8")

    # Create a nested subdirectory with files
    subdir = repo / "packages" / "backend"
    subdir.mkdir(parents=True)
    (subdir / "server.py").write_text("print('server')\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with subdir"], cwd=repo, check=True, capture_output=True)

    return repo, subdir


@pytest.fixture
def non_git_dir(tmp_path: Path):
    """Create an isolated directory outside of any git repository."""
    isolated = tmp_path / "plain_dir"
    isolated.mkdir()
    (isolated / "file.txt").write_text("plain\n", encoding="utf-8")
    return isolated


class TestGitServerTypingAndExports:
    """Verify typing import and dynamic attribute access in git_server."""

    def test_typing_import_and_getattr(self, temp_git_repo_with_subdir):
        repo, _ = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(repo)
            assert git_server.REPOSITORY == repo
            assert hasattr(git_server, "__getattr__")
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)


class TestGitSubdirectoryDetection:
    """Verify Git operations when AURA_WORKSPACE is a subdirectory of a git repository."""

    def test_git_status_in_subdirectory(self, temp_git_repo_with_subdir):
        repo, subdir = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            # Set workspace directly to the subdirectory (no .git in subdir)
            os.environ["AURA_WORKSPACE"] = str(subdir)
            assert not (subdir / ".git").exists()

            status = git_status()
            assert not status.startswith("ERROR: Not a Git repository")
            # Should show branch info
            assert "##" in status or "master" in status.lower() or "main" in status.lower()
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)

    def test_git_branch_in_subdirectory(self, temp_git_repo_with_subdir):
        repo, subdir = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(subdir)
            branch = git_branch()
            assert not branch.startswith("ERROR: Not a Git repository")
            assert "master" in branch.lower() or "main" in branch.lower()
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)

    def test_git_log_in_subdirectory(self, temp_git_repo_with_subdir):
        repo, subdir = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(subdir)
            log = git_log(limit=5)
            assert not log.startswith("ERROR: Not a Git repository")
            assert "Initial commit with subdir" in log
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)

    def test_git_diff_in_subdirectory(self, temp_git_repo_with_subdir):
        repo, subdir = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(subdir)
            # Modify a file inside the subdirectory
            (subdir / "server.py").write_text("print('modified server')\n", encoding="utf-8")
            diff = git_diff()
            assert not diff.startswith("ERROR: Not a Git repository")
            assert "modified server" in diff
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)

    def test_git_ls_files_in_subdirectory(self, temp_git_repo_with_subdir):
        repo, subdir = temp_git_repo_with_subdir
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(subdir)
            res = git_ls_files("server.py")
            assert not res.startswith("ERROR: Not a Git repository")
            assert "TRACKED IN GIT INDEX: server.py" in res
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)


class TestNonGitRepositoryDetection:
    """Verify that a directory that is not inside any Git repository returns clean error."""

    def test_git_status_on_non_git_dir_returns_error(self, non_git_dir):
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            os.environ["AURA_WORKSPACE"] = str(non_git_dir)
            status = git_status()
            assert status.startswith("ERROR: Not a Git repository:")
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env
            else:
                os.environ.pop("AURA_WORKSPACE", None)
