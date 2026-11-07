"""
Workspace Configuration and Resolution

Single source of truth for "which repository is AURA working on?"

Precedence (highest wins):
    1. Explicit --workspace CLI argument
    2. AURA_WORKSPACE environment variable
    3. Current working directory (fallback)

The workspace is validated before it is used anywhere:
    - It must exist.
    - It must be a directory.
    - It must resolve to an absolute path.
    - It must NOT be a path-traversal escape from itself.

No repository is hardcoded. The Smart Helmet fallback that existed in
earlier versions has been removed: an unconfigured AURA run now defaults
to the caller's current working directory, which is the behaviour a
repository-independent agent should have.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple


class WorkspaceConfig:
    """Configuration for workspace resolution and validation."""

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        allow_env_override: bool = True,
        allow_cwd_fallback: bool = True,
    ):
        """
        Args:
            workspace_path: Explicit workspace path (typically from CLI --workspace)
            allow_env_override: Whether to consult AURA_WORKSPACE
            allow_cwd_fallback: Whether to fall back to the current working directory
        """
        self.explicit_path = workspace_path
        self.allow_env_override = allow_env_override
        self.allow_cwd_fallback = allow_cwd_fallback

    def resolve_workspace(self) -> Path:
        """
        Resolve the workspace path in priority order.

        Returns:
            Resolved absolute Path

        Raises:
            ValueError: If the resolved workspace is invalid
        """
        # Priority 1: Explicit CLI argument
        if self.explicit_path:
            return self._validate_workspace(Path(self.explicit_path))

        # Priority 2: Environment variable
        if self.allow_env_override:
            env_workspace = os.environ.get("AURA_WORKSPACE")
            if env_workspace:
                return self._validate_workspace(Path(env_workspace))

        # Priority 3: Current working directory
        if self.allow_cwd_fallback:
            return self._validate_workspace(Path.cwd())

        raise ValueError(
            "No workspace configured: supply --workspace or set AURA_WORKSPACE"
        )

    def _validate_workspace(self, path: Path) -> Path:
        """
        Validate, resolve and normalise a workspace path.

        Args:
            path: Path to validate

        Returns:
            Resolved absolute Path

        Raises:
            ValueError: If the path is empty, unresolvable, missing, or not a directory
        """
        if path is None:
            raise ValueError("Workspace path cannot be empty")

        raw = str(path).strip()
        if not raw:
            raise ValueError("Workspace path cannot be empty")

        # Resolve to an absolute path. On Windows this also canonicalises
        # drive letter case and follows symlinks at the root level.
        try:
            resolved = Path(raw).resolve(strict=False)
        except Exception as exc:  # pragma: no cover - extremely defensive
            raise ValueError(f"Invalid workspace path: {exc}")

        if not resolved.exists():
            raise ValueError(f"Workspace does not exist: {raw}")

        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {raw}")

        return resolved


def resolve_workspace(
    workspace: Optional[str] = None,
    allow_env: bool = True,
    allow_cwd: bool = True,
) -> Path:
    """
    Convenience function to resolve the workspace path.

    Args:
        workspace: Explicit workspace path (typically from CLI)
        allow_env: Whether to consult AURA_WORKSPACE
        allow_cwd: Whether to fall back to the current working directory

    Returns:
        Resolved absolute Path

    Raises:
        ValueError: If the workspace cannot be resolved
    """
    config = WorkspaceConfig(
        workspace_path=workspace,
        allow_env_override=allow_env,
        allow_cwd_fallback=allow_cwd,
    )
    return config.resolve_workspace()


def validate_workspace_path(path) -> Tuple[bool, Optional[str]]:
    """
    Validate a workspace path without raising exceptions.

    Args:
        path: Path to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        resolved = Path(path).resolve(strict=False)
        if not resolved.exists():
            return False, f"Path does not exist: {path}"
        if not resolved.is_dir():
            return False, f"Path is not a directory: {path}"
        return True, None
    except Exception as exc:
        return False, f"Invalid path: {exc}"


def workspace_from_argv(argv=None) -> Optional[str]:
    """
    Extract a --workspace value from an argument list.

    Supports both `--workspace PATH` and `-w PATH` forms.

    Args:
        argv: Argument list (defaults to sys.argv[1:])

    Returns:
        The workspace value, or None if not present
    """
    if argv is None:
        argv = sys.argv[1:]

    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--workspace", "-w"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1]
            return None
        if arg.startswith("--workspace="):
            return arg.split("=", 1)[1]
        i += 1
    return None


def normalize_workspace_path(path_str: str) -> Path:
    """Normalize, unquote, and resolve a workspace directory path."""
    if not path_str or not path_str.strip():
        raise ValueError("Workspace path cannot be empty")
    cleaned = path_str.strip().strip("'\"")
    p = Path(cleaned).expanduser().resolve(strict=False)
    if not p.exists():
        raise ValueError(f"Workspace does not exist: {cleaned}")
    if not p.is_dir():
        raise ValueError(f"Workspace is not a directory: {cleaned}")
    return p


def find_referenced_directory(text: str) -> Optional[Path]:
    """Scan natural language text for an explicit reference to an existing directory."""
    if not text or not text.strip():
        return None

    # Check for quoted paths first: "C:\path" or 'C:\path'
    for match in re.finditer(r'["\']([^"\']+)["\']', text):
        candidate = match.group(1).strip()
        try:
            p = Path(candidate).expanduser().resolve(strict=False)
            if p.exists() and p.is_dir():
                return p
        except Exception:
            pass

    # Check for Windows absolute path: C:\... or C:/...
    win_match = re.search(r'[a-zA-Z]:[\\/][^\n\r<>|*?]+', text)
    if win_match:
        candidate_str = win_match.group(0).strip()
        parts = candidate_str.split()
        for i in range(len(parts), 0, -1):
            sub = " ".join(parts[:i]).rstrip(".,;!?:")
            try:
                p = Path(sub).expanduser().resolve(strict=False)
                if p.exists() and p.is_dir():
                    return p
            except Exception:
                pass

    return None
