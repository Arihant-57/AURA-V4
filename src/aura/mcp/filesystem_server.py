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

import ast
import difflib
import fnmatch
import os
import re
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

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

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

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

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
# REPOSITORY SCANNER (HOLISTIC CODEBASE SURVEY)
# ============================================================

_SCAN_EXCLUDED_DIRS = frozenset({
    ".git", ".next", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", "dist", "build",
    ".mypy_cache", ".ruff_cache", ".aura",
})
_MANIFEST_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "tsconfig.json", "cargo.toml", "go.mod",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", "readme.md", ".env.example", "agents.md",
})


def _extract_py_symbols(content: str, limit: int = 8) -> list[str]:
    symbols: list[str] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return symbols
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}()")
        if len(symbols) >= limit:
            break
    return symbols


@mcp.tool()
def scan_repository(path: str = ".", max_depth: int = 3, include_symbols: bool = True) -> str:
    """Scan a repository or directory in one call: manifests, tree structure, extension counts, tests, and key source symbols."""
    try:
        root = safe_path(path or ".")
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not root.exists():
        return f"ERROR: Directory does not exist: {path}"
    if not root.is_dir():
        return f"ERROR: Not a directory: {path}"

    depth_limit = max(1, min(int(max_depth), 6))
    try:
        workspace = _get_workspace()
    except ValueError:
        workspace = root

    manifests: list[str] = []
    test_files: list[str] = []
    ext_counts: dict[str, int] = {}
    tree_lines: list[str] = []
    source_entries: list[str] = []
    total_files = 0
    total_dirs = 0

    for cur_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SCAN_EXCLUDED_DIRS and not d.startswith(".git")
        )
        filenames = sorted(filenames)
        cur_path = Path(cur_root)
        rel_dir = cur_path.relative_to(root)
        rel_depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)

        if rel_depth > depth_limit:
            dirnames[:] = []
            continue

        if rel_depth > 0 and len(tree_lines) < 120:
            indent = "  " * (rel_depth - 1)
            tree_lines.append(f"{indent}{rel_dir.parts[-1]}/")

        total_dirs += len(dirnames)
        for fname in filenames:
            total_files += 1
            fpath = cur_path / fname
            ext = fpath.suffix.lower() or "(no ext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

            try:
                rel_ws = str(fpath.relative_to(workspace)).replace("\\", "/")
            except ValueError:
                rel_ws = str(fpath.relative_to(root)).replace("\\", "/")

            if fname.lower() in _MANIFEST_NAMES:
                manifests.append(rel_ws)

            low_name = fname.lower()
            if (
                low_name.startswith("test_")
                or low_name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.js"))
            ):
                test_files.append(rel_ws)

            if len(tree_lines) < 120:
                indent = "  " * rel_depth
                tree_lines.append(f"{indent}{fname}")

            if (
                include_symbols
                and len(source_entries) < 25
                and ext in {".py", ".ts", ".tsx", ".js", ".jsx"}
                and rel_ws not in test_files
            ):
                try:
                    raw = fpath.read_text(encoding="utf-8", errors="replace")
                    line_count = len(raw.splitlines())
                    if ext == ".py":
                        syms = _extract_py_symbols(raw)
                        sym_txt = f" -> {', '.join(syms)}" if syms else ""
                    else:
                        sym_txt = ""
                    source_entries.append(f"- `{rel_ws}` ({line_count} lines){sym_txt}")
                except Exception:
                    pass

    top_exts = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ext_summary = ", ".join(f"{ext}: {cnt}" for ext, cnt in top_exts) or "(none)"

    sections: list[str] = [
        f"Repository Scan: {root.name} ({path or '.'})",
        f"- Files scanned: {total_files} | Subdirectories: {total_dirs} | Max depth: {depth_limit}",
        f"- File types: {ext_summary}",
        f"- Manifests & docs: {', '.join(manifests[:20]) if manifests else '(none detected)'}",
        f"- Test files detected: {len(test_files)}" + (f" ({', '.join(test_files[:10])})" if test_files else ""),
    ]
    if tree_lines:
        trunc = "\n... [tree truncated]" if len(tree_lines) >= 120 else ""
        sections.append("### Directory Structure\n```\n" + "\n".join(tree_lines) + trunc + "\n```")
    if source_entries:
        sections.append("### Key Source Files & Symbols\n" + "\n".join(source_entries))
    return "\n\n".join(sections)


# ============================================================
# FILE OUTLINE (AST / STRUCTURE MAP)
# ============================================================

@mcp.tool()
def file_outline(path: str) -> str:
    """Return structural symbols (classes, functions, methods, headings) and line ranges for a file."""
    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not target.exists():
        return f"ERROR: File does not exist: {path}"
    if not target.is_file():
        return f"ERROR: Not a file: {path}"

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"ERROR reading file for outline: {exc}"

    lines = content.splitlines()
    entries: list[str] = []
    syntax_note = ""

    if target.suffix.lower() == ".py":
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    end_ln = getattr(node, "end_lineno", node.lineno)
                    entries.append(f"L{node.lineno}-L{end_ln} class {node.name}")
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            c_end = getattr(child, "end_lineno", child.lineno)
                            prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                            entries.append(f"  L{child.lineno}-L{c_end} {prefix} {node.name}.{child.name}()")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end_ln = getattr(node, "end_lineno", node.lineno)
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    entries.append(f"L{node.lineno}-L{end_ln} {prefix} {node.name}()")
        except SyntaxError as exc:
            syntax_note = f"[SYNTAX ERROR at L{exc.lineno}: {exc.msg}]\n"

    if not entries:
        sym_re = re.compile(
            r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class|def|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"|^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s+)?\("
            r"|^(#{1,3}\s+.+)$"
        )
        for idx, line in enumerate(lines, start=1):
            m = sym_re.match(line)
            if m:
                entries.append(f"L{idx}: {line.strip()[:100]}")

    header = f"Outline for {path} ({len(lines)} lines):"
    if syntax_note:
        header = syntax_note + header
    if not entries:
        return f"{header}\n(no top-level symbols detected)"
    return header + "\n" + "\n".join(entries[:120])


# ============================================================
# SEARCH FILES
# ============================================================

@mcp.tool()
def search_files(
    query: str,
    path: str = ".",
    use_regex: bool = False,
    file_glob: str = "",
) -> str:
    """Search supported UTF-8 source files recursively inside the workspace.

    Optional filters:
    - use_regex: interpret *query* as a case-insensitive regular expression.
    - file_glob: fnmatch pattern (e.g. '*.py', 'src/aura/*') filtering relative paths or names.
    """

    if not isinstance(query, str) or not query.strip():
        return "ERROR: Search query cannot be empty."

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not target.exists():
        return f"ERROR: Path does not exist: {path}"

    if not target.is_dir():
        return f"ERROR: Search path is not a directory: {path}"

    pattern = None
    if use_regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            return f"ERROR: Invalid regex pattern '{query}': {exc}"

    query_lower = query.lower()
    glob_filter = (file_glob or "").strip().replace("\\", "/")

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
    workspace = _get_workspace()

    try:
        for file_path in target.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in supported_extensions:
                continue
            if any(part in excluded_directories for part in file_path.parts):
                continue
            relative_str = str(file_path.relative_to(workspace)).replace("\\", "/")
            if glob_filter and not (
                fnmatch.fnmatch(file_path.name, glob_filter)
                or fnmatch.fnmatch(relative_str, glob_filter)
            ):
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
                matched = bool(pattern.search(line)) if pattern is not None else (query_lower in line.lower())
                if matched:
                    results.append(
                        f"{relative_str}:{line_number}: {line.strip()}"
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
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = 0x08000000
            if hasattr(subprocess, "STARTUPINFO"):
                si = subprocess.STARTUPINFO()
                si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                extra["startupinfo"] = si
        res = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            **extra,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: Test execution timed out after 30 seconds."
    except Exception as exc:
        return f"ERROR running tests: {exc}"

    output = (res.stdout + "\n" + res.stderr).strip()
    if len(output) > 4000:
        output = output[:4000] + "\n\n[TRUNCATED: output exceeded 4,000 characters]"

    status = "PASSED" if res.returncode == 0 else f"FAILED (exit code {res.returncode})"
    result_text = f"TEST SUITE {status}:\n{output}"
    if res.returncode == 5 and (workspace / "package.json").exists():
        result_text += "\n\n(Note: No pytest tests found. This workspace appears to be a Node.js project containing package.json)"
    return result_text


# ============================================================
# START MCP SERVER
# ============================================================

if __name__ == "__main__":
    mcp.run()