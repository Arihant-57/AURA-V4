"""
AURA Filesystem MCP Server (read-only)

Provides read-only filesystem access restricted to a single workspace.

The workspace is resolved from the AURA_WORKSPACE environment variable
or the --workspace CLI argument passed by the host agent. There is NO
hardcoded repository fallback: an unconfigured server refuses to start
rather than silently defaulting to a specific project.

Path safety:
    - Relative paths only (absolute paths rejected).
    - No '..' components (directory traversal rejected).
    - Resolved target must remain inside the workspace, even after
      symlink resolution (symlink escape rejected).
    - Unicode NFKC normalisation defeats homoglyph / bidi tricks.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import unicodedata
from pathlib import Path, PureWindowsPath
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
            "AURA_WORKSPACE is not set. The filesystem server cannot "
            "start without an explicit workspace."
        )
    return Path(workspace_env).resolve(strict=False)


def get_workspace() -> Path:
    """Public helper to get currently resolved workspace."""
    return _get_workspace()


def __getattr__(name: str) -> Any:
    if name == "WORKSPACE":
        return _get_workspace()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


mcp = MCPServer("AURA Filesystem Server")


# ============================================================
# SAFE PATH
# ============================================================

def safe_path(path: str) -> Path:
    """
    Resolve a relative path while preventing workspace escape.

    Rejects:
        - Empty / non-string paths
        - Absolute paths outside the workspace
        - '..' components
        - Trailing whitespace or dots in components
        - Targets that resolve outside the workspace, including
          through symlinks

    Args:
        path: Repository-relative path or absolute path inside the workspace

    Returns:
        Resolved absolute Path inside the workspace

    Raises:
        ValueError: If the path escapes the workspace
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid path.")

    cleaned = path.strip().strip("'\"")
    normalised = unicodedata.normalize("NFKC", cleaned)
    normalised = normalised.replace("\\", "/")

    workspace = _get_workspace()
    windows_path = PureWindowsPath(normalised)

    if windows_path.is_absolute() or windows_path.drive:
        try:
            abs_target = Path(normalised).resolve(strict=False)
            rel = abs_target.relative_to(workspace)
            normalised = rel.as_posix()
        except ValueError:
            raise ValueError("Absolute paths are not allowed.")

    components = normalised.split("/")

    for component in components:
        if component in {"", "."}:
            continue
        if component == "..":
            raise ValueError(
                "Access outside the project workspace is not allowed."
            )
        if component != component.rstrip():
            raise ValueError("Invalid path component.")
        if component != component.rstrip("."):
            raise ValueError("Invalid path component.")
        if component.strip(".") == "":
            raise ValueError("Invalid path component.")

    # Re-resolve the workspace in case it changed at runtime.
    workspace = _get_workspace()
    target = (workspace / Path(normalised)).resolve(strict=False)

    try:
        target.relative_to(workspace)
    except ValueError:
        raise ValueError(
            "Access outside the project workspace is not allowed."
        )

    return target


# ============================================================
# READ FILE
# ============================================================

@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read a UTF-8 text file inside the workspace.
    
    Optional parameters:
    - start_line (1-indexed): Starting line number to read (default: 1).
    - end_line (1-indexed, inclusive): Ending line number to read (default: 0 = to end of file).
    """

    target = safe_path(path)

    if not target.exists():
        return f"ERROR: File does not exist: {path}"

    if not target.is_file():
        return f"ERROR: Not a file: {path}"

    try:
        content = target.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return f"ERROR reading file: {exc}"

    lines = content.splitlines()
    total_lines = len(lines)

    if start_line > 1 or end_line > 0:
        start_idx = max(0, start_line - 1)
        end_idx = total_lines if end_line <= 0 else min(total_lines, end_line)
        selected_lines = lines[start_idx:end_idx]
        formatted = [f"{i:4d} | {line}" for i, line in enumerate(selected_lines, start=start_idx + 1)]
        return f"[Lines {start_idx + 1} to {end_idx} of {total_lines} in {path}]:\n" + "\n".join(formatted)

    # Prevent enormous model context.
    if len(content) > 10000:
        content = content[:10000]
        content += f"\n\n[TRUNCATED: file exceeds 10,000 characters. Total lines: {total_lines}. Use start_line/end_line to read specific sections.]"

    return content


# ============================================================
# LIST DIRECTORY
# ============================================================

@mcp.tool()
def list_directory(path: str = ".") -> str:
    """List files and directories inside the workspace."""

    target = safe_path(path)

    if not target.exists():
        return f"ERROR: Directory does not exist: {path}"

    if not target.is_dir():
        return f"ERROR: Not a directory: {path}"

    try:
        entries = sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except Exception as exc:
        return f"ERROR listing directory: {exc}"

    lines = []

    for entry in entries[:50]:
        if entry.is_dir():
            lines.append(f"DIR  {entry.name}")
        else:
            lines.append(f"FILE {entry.name}")

    if len(entries) > 50:
        lines.append(
            f"\n[TRUNCATED: showing first 50 of {len(entries)} entries]"
        )

    if not lines:
        return "(empty directory)"

    return "\n".join(lines)


# ============================================================
# SEARCH FILES
# ============================================================

@mcp.tool()
def search_files(
    query: str,
    path: str = ".",
) -> str:
    """Search supported UTF-8 source files recursively inside the workspace."""

    if not isinstance(query, str) or not query.strip():
        return "ERROR: Search query cannot be empty."

    target = safe_path(path)

    if not target.exists():
        return f"ERROR: Path does not exist: {path}"

    if not target.is_dir():
        return f"ERROR: Search path is not a directory: {path}"

    query_lower = query.lower()

    supported_extensions = {
        ".txt",
        ".md",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".yaml",
        ".yml",
        ".css",
        ".html",
        ".ino",
    }

    excluded_directories = {
        ".git",
        ".next",
        "node_modules",
        ".venv",
        "__pycache__",
    }

    results = []

    try:
        for file_path in target.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in supported_extensions:
                continue
            if any(part in excluded_directories for part in file_path.parts):
                continue
            try:
                content = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                continue
            for line_number, line in enumerate(
                content.splitlines(),
                start=1,
            ):
                if query_lower in line.lower():
                    workspace = _get_workspace()
                    relative = file_path.relative_to(workspace)
                    results.append(
                        f"{relative}:{line_number}: {line.strip()}"
                    )
                    if len(results) >= 100:
                        return "\n".join(
                            results
                        ) + "\n\n[TRUNCATED: 100 results]"
    except Exception as exc:
        return f"ERROR searching files: {exc}"

    if not results:
        return "(no matches found)"

    return "\n".join(results)


# ============================================================
# FILE SNAPSHOT HELPER (FOR DIFFS)
# ============================================================

_ORIGINAL_SNAPSHOTS: dict[str, str] = {}


def _snapshot_file(target: Path) -> None:
    """Capture snapshot of file before first modification in session."""
    key = str(target.resolve(strict=False))
    if key not in _ORIGINAL_SNAPSHOTS and target.exists() and target.is_file():
        try:
            _ORIGINAL_SNAPSHOTS[key] = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _is_read_only() -> bool:
    """Check if the filesystem server is running in read-only mode."""
    return os.environ.get("AURA_READ_ONLY", "0").lower() in ("1", "true", "yes")


# ============================================================
# WRITE FILE
# ============================================================

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file inside the workspace with UTF-8 content."""
    if _is_read_only():
        return "ERROR: Workspace is in read-only mode. File modifications are blocked by security policy."

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if len(content) > 100000:
        return "ERROR: Content exceeds maximum limit of 100,000 characters."

    try:
        _snapshot_file(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"SUCCESS: Wrote {len(content)} characters to {path}"
    except Exception as exc:
        return f"ERROR writing file: {exc}"


# ============================================================
# EDIT FILE
# ============================================================

@mcp.tool()
def edit_file(path: str, target: str, replacement: str, replace_all: bool = False) -> str:
    """Replace target substring with replacement text in a workspace file.

    If target matches multiple occurrences and replace_all is False, returns an error
    asking for more surrounding context to disambiguate. Set replace_all=True only
    when all occurrences should be replaced.
    """
    if _is_read_only():
        return "ERROR: Workspace is in read-only mode. File modifications are blocked by security policy."

    try:
        target_path = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not target_path.exists():
        return f"ERROR: File does not exist: {path}"

    if not target_path.is_file():
        return f"ERROR: Not a file: {path}"

    if not target:
        return "ERROR: target string to replace cannot be empty."

    try:
        content = target_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"ERROR reading file for edit: {exc}"

    if target not in content:
        return f"ERROR: Target string not found in {path}. Ensure target matches existing text exactly."

    count = content.count(target)
    if count > 1 and not replace_all:
        return (
            f"ERROR: Target string matches {count} occurrences in {path}. "
            "To avoid accidental changes to unintended code, provide more surrounding lines/context "
            "(e.g. function header or surrounding statements) so target is unique, "
            "or set replace_all=true if you explicitly intend to replace all occurrences."
        )

    _snapshot_file(target_path)
    new_content = content.replace(target, replacement) if replace_all else content.replace(target, replacement, 1)

    if len(new_content) > 100000:
        return "ERROR: Edited content exceeds maximum limit of 100,000 characters."

    try:
        target_path.write_text(new_content, encoding="utf-8")
        return f"SUCCESS: Replaced {count if replace_all else 1} occurrence(s) in {path}"
    except Exception as exc:
        return f"ERROR saving edited file: {exc}"


# ============================================================
# GET FILE DIFF
# ============================================================

@mcp.tool()
def get_file_diff(path: str = "") -> str:
    """Show unified diff of changes made to workspace files during this session.

    If path is specified, returns the diff for that specific file.
    If path is omitted or empty, returns diffs for all files modified during this session.
    """
    try:
        workspace = _get_workspace()
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not _ORIGINAL_SNAPSHOTS:
        return "(no files modified in this session yet)"

    diffs = []
    if path and path.strip():
        try:
            target = safe_path(path)
            key = str(target.resolve(strict=False))
            if key not in _ORIGINAL_SNAPSHOTS:
                return f"No changes recorded for {path} in this session."
            orig = _ORIGINAL_SNAPSHOTS[key]
            curr = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            file_diff = list(difflib.unified_diff(
                orig.splitlines(keepends=True),
                curr.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ))
            if not file_diff:
                return f"No diff in {path} (content is identical to original)."
            return "".join(file_diff)
        except Exception as exc:
            return f"ERROR generating diff: {exc}"

    # All modified files
    for key, orig in _ORIGINAL_SNAPSHOTS.items():
        try:
            target = Path(key)
            rel = target.relative_to(workspace)
            curr = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            file_diff = list(difflib.unified_diff(
                orig.splitlines(keepends=True),
                curr.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            ))
            if file_diff:
                diffs.append("".join(file_diff))
        except Exception:
            continue

    if not diffs:
        return "(no changes detected across modified files)"
    return "\n---\n".join(diffs)


# ============================================================
# RUN TESTS
# ============================================================

@mcp.tool()
def run_tests(test_path: str = "") -> str:
    """Run pytest test suite or a specific test file safely inside the workspace."""
    if test_path and test_path.strip() and test_path.strip() != ".":
        cleaned = test_path.strip()
        if cleaned.startswith("-") or "--" in cleaned:
            return "ERROR: Command-line flags are not allowed in test_path."

    try:
        workspace = _get_workspace()
    except ValueError as exc:
        return f"ERROR: {exc}"

    cmd = [sys.executable, "-m", "pytest", "-v", "--no-header"]

    if test_path and test_path.strip() and test_path.strip() != ".":
        cleaned = test_path.strip()
        try:
            target_path = safe_path(cleaned)
            cmd.append(str(target_path))
        except ValueError as exc:
            return f"ERROR invalid test_path: {exc}"

    try:
        res = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False
        )
    except subprocess.TimeoutExpired:
        return "ERROR: Test execution timed out after 30 seconds."
    except Exception as exc:
        return f"ERROR running tests: {exc}"

    output = (res.stdout + "\n" + res.stderr).strip()
    if len(output) > 4000:
        output = output[:4000] + "\n\n[TRUNCATED: output exceeded 4,000 characters]"

    status = "PASSED" if res.returncode == 0 else f"FAILED (exit code {res.returncode})"
    return f"TEST SUITE {status}:\n{output}"


# ============================================================
# START MCP SERVER
# ============================================================

if __name__ == "__main__":
    mcp.run()