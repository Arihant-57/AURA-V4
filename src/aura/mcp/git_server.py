"""
AURA Git MCP Server (read-only)

Provides read-only Git operations restricted to a single workspace.

The workspace is resolved from the AURA_WORKSPACE environment variable
or the --workspace CLI argument passed by the host agent. There is NO
hardcoded repository fallback: an unconfigured server refuses to start
rather than silently defaulting to a specific project.

All Git commands run with `git -C <workspace>` so they can never touch
a repository outside the selected workspace.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server import MCPServer


# ============================================================
# WORKSPACE CONFIGURATION
# ============================================================

def _get_workspace() -> Path:
    """
    Resolve the workspace from the environment.

    Raises ValueError if no workspace is configured. There is no
    hardcoded repository fallback.
    """
    workspace_env = os.environ.get("AURA_WORKSPACE")
    if not workspace_env:
        raise ValueError(
            "AURA_WORKSPACE is not set. The Git server cannot "
            "start without an explicit workspace."
        )
    return Path(workspace_env).resolve(strict=False)


def get_repository() -> Path:
    """Public helper to get currently resolved repository Path."""
    return _get_workspace()


def __getattr__(name: str) -> Any:
    if name == "REPOSITORY":
        return _get_workspace()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


mcp = MCPServer("AURA Git Server")


# ============================================================
# READ-ONLY GIT COMMAND
# ============================================================

def run_git(*args: str) -> str:
    """Run a read-only Git command against the workspace repository."""
    try:
        repo = _get_workspace()
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not repo.is_dir():
        return f"ERROR: Repository does not exist: {repo}"

    is_git_repo = (repo / ".git").exists()
    if not is_git_repo:
        try:
            is_git_repo = (
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                    capture_output=True,
                ).returncode == 0
            )
        except Exception:
            is_git_repo = False

    if not is_git_repo:
        return f"ERROR: Not a Git repository: {repo}"

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return "ERROR: Git command timed out."

    except FileNotFoundError:
        return "ERROR: Git executable was not found."

    except Exception as exc:
        return f"ERROR running Git: {exc}"

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        if stderr:
            return f"GIT ERROR:\n{stderr}"

        return (
            f"GIT ERROR: command exited with code "
            f"{result.returncode}"
        )

    if stdout:
        return stdout

    return "(no output)"


# ============================================================
# GIT STATUS
# ============================================================

@mcp.tool()
def git_status() -> str:
    """Show the current Git branch and working-tree status."""
    return run_git(
        "status",
        "--short",
        "--branch",
    )


# ============================================================
# GIT BRANCH
# ============================================================

@mcp.tool()
def git_branch() -> str:
    """Show local Git branches for the workspace repository."""

    result = run_git(
        "branch",
        "--list",
    )

    if result == "(no output)":
        current = run_git(
            "symbolic-ref",
            "--short",
            "HEAD",
        )

        if current.startswith("GIT ERROR"):
            return "No branches exist yet. HEAD is unborn."

        return f"Current branch: {current}"

    return result


# ============================================================
# GIT LOG
# ============================================================

@mcp.tool()
def git_log(limit: int = 10) -> str:
    """Show recent Git commits."""

    if limit < 1:
        return "ERROR: limit must be at least 1."

    if limit > 50:
        limit = 50

    result = run_git(
        "log",
        f"-{limit}",
        "--oneline",
        "--decorate",
    )

    if result.startswith("GIT ERROR"):
        lower = result.lower()

        if (
            "does not have any commits yet" in lower
            or "does not have any commits" in lower
            or "your current branch" in lower
        ):
            return "(repository has no commits yet)"

    return result


# ============================================================
# GIT DIFF
# ============================================================

@mcp.tool()
def git_diff() -> str:
    """Show unstaged changes in tracked files."""
    return run_git(
        "diff",
        "--",
    )


# ============================================================
# GIT LS-FILES
# ============================================================

@mcp.tool()
def git_ls_files(path: str) -> str:
    """
    Determine whether a repository-relative path is tracked in Git's index.

    This answers whether Git tracks the path. It does NOT mean the file
    currently exists on disk.
    """

    if not path or not path.strip():
        return "ERROR: path must not be empty."

    normalized_path = path.strip().replace("\\", "/")

    candidate = Path(normalized_path)

    if candidate.is_absolute():
        return "ERROR: path must be repository-relative."

    if any(part == ".." for part in candidate.parts):
        return "ERROR: path must not contain '..'."

    result = run_git(
        "ls-files",
        "--error-unmatch",
        "--",
        normalized_path,
    )

    if result.startswith("GIT ERROR"):
        return f"NOT TRACKED IN GIT INDEX: {normalized_path}"

    if result == "(no output)":
        return f"NOT TRACKED IN GIT INDEX: {normalized_path}"

    return f"TRACKED IN GIT INDEX: {result}"


# ============================================================
# START MCP SERVER
# ============================================================

if __name__ == "__main__":
    mcp.run()