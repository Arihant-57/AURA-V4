"""
MCP Tool Helpers and Scoped Path Policy for AURA V4.

Handles tool-schema conversion, argument validation, path gating,
and result sanitisation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from mcp.types import TextContent

from ..config import get_settings
from .scope import TaskScope
from .evidence_constants import (
    NOISE_DIR_COMPONENTS, LOCK_FILES, FRONTEND_DIRS, BACKEND_DIRS,
    SENSITIVE_FILE_NAMES, SENSITIVE_SUFFIXES, SENSITIVE_DIR_NAMES
)


# ---------------------------------------------------------------------------
# Path & Scoping Policy
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/") or "."


def is_explicit_backend_scope(scope: TaskScope) -> bool:
    """True only when the prompt names the backend explicitly as a location."""
    explicit_backend_tokens = {"backend", "server", "api", "apis"}
    tokens = {t.strip(".") for t in re.findall(r"[a-z0-9]+", scope.prompt_lower)}
    return bool(explicit_backend_tokens & tokens)


def check_path_policy(name: str, arguments: dict, scope: TaskScope) -> Optional[str]:
    """
    Deterministic guardrail applied before any MCP call. Returns a blocking
    message or None.
    """
    if name not in {"read_file", "write_file", "edit_file", "list_directory", "search_files", "get_file_diff"}:
        return None

    path_arg = arguments.get("path", "")
    if not isinstance(path_arg, str):
        return None

    normalized = _normalize_path(path_arg).lower()
    parts = [p for p in normalized.split("/") if p and p != "."]
    base = parts[-1] if parts else ""

    # Sensitive / secret file protection (always active regardless of task)
    if (
        base in SENSITIVE_FILE_NAMES
        or any(base.endswith(sfx) for sfx in SENSITIVE_SUFFIXES)
        or any(part in SENSITIVE_DIR_NAMES for part in parts)
    ):
        return (
            f"MCP TOOL ERROR: access to '{path_arg}' is blocked by security policy "
            f"(sensitive credentials or secrets must never be accessed)."
        )

    # Modification safety checks
    if name in {"write_file", "edit_file"}:
        for part in parts:
            if part in NOISE_DIR_COMPONENTS:
                return (
                    f"MCP TOOL ERROR: modifying '{path_arg}' is blocked by safety policy "
                    f"('{part}' is protected/vendored directory)."
                )
        if base in LOCK_FILES:
            return (
                f"MCP TOOL ERROR: modifying lock file '{path_arg}' directly is blocked by safety policy."
            )

    for part in parts:
        if part in NOISE_DIR_COMPONENTS and part not in scope.prompt_lower:
            return (
                f"MCP TOOL ERROR: access to '{path_arg}' is blocked by policy "
                f"('{part}' is generated/vendored content and almost never relevant). "
                f"If you truly need it, ask the user to request it explicitly."
            )

    if base in LOCK_FILES and base not in scope.prompt_lower:
        return (
            f"MCP TOOL ERROR: reading lock file '{path_arg}' is blocked by policy. "
            f"The manifest (e.g. package.json / pyproject.toml) almost always answers "
            f"dependency questions with far fewer tokens."
        )

    if scope.backend and not scope.frontend:
        for part in parts:
            if part in FRONTEND_DIRS and part not in scope.prompt_lower:
                return (
                    f"MCP TOOL ERROR: access to '{path_arg}' is blocked by policy. "
                    f"This is a backend-focused task; '{part}' is frontend/asset content."
                )

    if scope.frontend and not scope.backend:
        for part in parts:
            if part in BACKEND_DIRS and part not in scope.prompt_lower:
                return (
                    f"MCP TOOL ERROR: access to '{path_arg}' is blocked by policy. "
                    f"This is a frontend-focused task; '{part}' is backend content."
                )

    return None


# ---------------------------------------------------------------------------
# Result Sanitisation
# ---------------------------------------------------------------------------

_NOISE_LINE_RES: list[tuple[str, re.Pattern]] = [
    (n, re.compile(r"(?<![\w.\-])" + re.escape(n) + r"(?![\w.\-])"))
    for n in list(NOISE_DIR_COMPONENTS) + list(LOCK_FILES)
]


def sanitize_tool_result(
    name: str,
    text: str,
    prompt_lower: str,
    scope: TaskScope | None = None,
    max_chars: Optional[int] = None,
) -> str:
    """Remove irrelevant entries from listings/searches, then truncate explicitly."""
    settings = get_settings()
    if max_chars is not None:
        limit = max_chars
    elif name == "read_file":
        # Filesystem server limits files to 10,000 characters
        limit = 10000
    elif name == "run_tests":
        limit = 4000
    elif name == "get_file_diff":
        limit = 6000
    else:
        limit = settings.agent.max_tool_result_chars

    if name in {"list_directory", "search_files"}:
        active = [(n, r) for n, r in _NOISE_LINE_RES if n not in prompt_lower]
        kept, hidden = [], 0
        backend_focused = bool(scope and scope.backend and not scope.frontend)
        for line in text.splitlines():
            lowered = line.lower()
            if any(r.search(lowered) for _n, r in active):
                hidden += 1
                continue
            if backend_focused and re.search(
                r"(?<![\w.\-/])(frontend|client|web|ui|public|static|assets)(?![\w.\-/])",
                lowered,
            ):
                hidden += 1
                continue
            kept.append(line)
        if hidden:
            kept.append(f"({hidden} irrelevant/noise entries hidden by task scope)")
        text = "\n".join(kept)
        if not text.strip():
            text = "(no relevant matches found outside excluded content)"

    if name in {"git_diff", "get_file_diff"}:
        filtered_lines = []
        skip_block = False
        for line in text.splitlines():
            if line.startswith("diff --git") or line.startswith("--- a/") or line.startswith("+++ b/"):
                lowered_line = line.lower()
                if any(sens in lowered_line for sens in SENSITIVE_FILE_NAMES) or any(lowered_line.endswith(sfx) for sfx in SENSITIVE_SUFFIXES):
                    skip_block = True
                    filtered_lines.append(f"[SENSITIVE DIFF REDACTED: {line}]")
                    continue
                else:
                    skip_block = False
            if skip_block:
                continue
            filtered_lines.append(line)
        text = "\n".join(filtered_lines)

    if len(text) <= limit:
        return text

    return (
        text[:limit]
        + f"\n\n[TRUNCATED: showing first {limit} of {len(text)} characters.]"
    )


# ---------------------------------------------------------------------------
# Schema Conversion & Validation
# ---------------------------------------------------------------------------

def mcp_tool_to_ollama(tool: Any, scope: TaskScope | None = None) -> dict:
    """Convert MCP tool schema to Ollama/OpenAI function calling format."""
    schema = (
        getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {"type": "object", "properties": {}}
    )
    description = tool.description or ""
    if scope and scope.backend and not scope.frontend:
        if tool.name == "list_directory":
            description += (
                ' BACKEND TASK RULE: start with path "backend"; '
                'do not list repository root or backend/public.'
            )
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": description,
            "parameters": schema,
        },
    }


def mcp_result_to_text(result: Any) -> str:
    """Convert MCP tool result content to plain text."""
    parts = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif hasattr(block, "model_dump"):
            try:
                parts.append(json.dumps(block.model_dump(), ensure_ascii=False, indent=2))
            except Exception:
                parts.append(str(block))
        else:
            parts.append(str(block))

    if not parts and getattr(result, "structured_content", None):
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, indent=2))

    text = "\n".join(parts) if parts else "(empty MCP result)"
    if getattr(result, "is_error", False):
        return f"MCP TOOL ERROR:\n{text}"
    return text


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def validate_arguments(schema: Any, arguments: dict) -> Optional[str]:
    """Validate tool arguments against its input schema."""
    if not isinstance(schema, dict):
        return None

    required = schema.get("required") or []
    properties = schema.get("properties") or {}

    missing = [
        key for key in required
        if arguments.get(key) is None
        or (isinstance(arguments.get(key), str) and not arguments.get(key).strip())
    ]
    if missing:
        return (
            f"MCP TOOL ERROR: missing required argument(s): {', '.join(missing)}. "
            f"This tool accepts: {', '.join(properties) or '(no arguments)'}."
        )

    type_errors = []
    for key, value in arguments.items():
        if key not in properties or value is None:
            continue
        spec = properties[key]
        if isinstance(spec, dict) and spec.get("type") and not _type_ok(spec["type"], value):
            type_errors.append(f"{key} (expected {spec['type']})")

    if type_errors:
        return f"MCP TOOL ERROR: invalid argument type(s): {', '.join(type_errors)}."
    return None


def normalize_tool_arguments(name: str, arguments: dict) -> dict:
    """Normalize common model argument aliases into canonical MCP schema parameter names."""
    if not isinstance(arguments, dict):
        return {}

    norm = dict(arguments)

    # Path parameter aliases across tools
    if "path" not in norm:
        for alias in ("file_path", "filepath", "filename", "file", "dir_path", "directory"):
            if alias in norm and isinstance(norm[alias], str):
                norm["path"] = norm.pop(alias)
                break

    # Write file content aliases
    if name == "write_file" and "content" not in norm:
        for alias in ("text", "code", "body", "data"):
            if alias in norm and isinstance(norm[alias], str):
                norm["content"] = norm.pop(alias)
                break

    # Edit file target & replacement aliases
    if name == "edit_file":
        if "target" not in norm:
            for alias in ("old_string", "old_text", "find", "pattern", "search"):
                if alias in norm and isinstance(norm[alias], str):
                    norm["target"] = norm.pop(alias)
                    break
        if "replacement" not in norm:
            for alias in ("new_string", "new_text", "replace", "replace_with"):
                if alias in norm and isinstance(norm[alias], str):
                    norm["replacement"] = norm.pop(alias)
                    break

    # Run tests test_path aliases
    if name == "run_tests" and "test_path" not in norm:
        for alias in ("path", "test", "file", "tests"):
            if alias in norm and isinstance(norm[alias], str):
                norm["test_path"] = norm.pop(alias)
                break

    # Search query aliases
    if name == "search_files" and "query" not in norm:
        for alias in ("pattern", "term", "text", "search"):
            if alias in norm and isinstance(norm[alias], str):
                norm["query"] = norm.pop(alias)
                break

    return norm

