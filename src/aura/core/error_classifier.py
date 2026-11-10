"""
Error and Notice Classification for AURA V4.

Distinguishes:
1. Expected inspection outcomes (INSPECTION_NOTICE):
   - Missing candidate files during read/list discovery
   - Empty search results or optional files not found
   - Non-fatal exploratory existence checks
2. Genuine execution errors (EXECUTION_ERROR):
   - Path boundary violations & traversal attempts
   - Permission denials & read-only enforcement
   - MCP protocol / timeout / server crashes
   - File I/O exceptions & corrupted arguments
3. Actual task defects (TASK_DEFECT):
   - Test execution failures
   - Verification failures & rejected implementations
   - Missing required outputs or unfulfilled specifications
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ErrorCategory(str, Enum):
    """Classification categories for agent feedback and error recording."""
    INSPECTION_NOTICE = "inspection_notice"
    EXECUTION_ERROR = "execution_error"
    TASK_DEFECT = "task_defect"


@dataclass
class ClassifiedResult:
    """Detailed categorization of a tool outcome or error."""
    category: ErrorCategory
    message: str
    tool: str = ""
    path: str = ""
    is_turn_failure: bool = False
    is_useful_progress: bool = True


def is_tool_error_text(text: str) -> bool:
    """Check if tool return text indicates an error or notice condition."""
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    return (
        stripped.startswith("ERROR:")
        or stripped.startswith("ERROR ")
        or stripped.startswith("MCP ERROR:")
        or stripped.startswith("MCP TOOL ERROR:")
        or "Access outside the project workspace is not allowed" in stripped
        or "Workspace is in read-only mode" in stripped
    )


def classify_tool_result(
    tool_name: str,
    result_text: str,
    arguments: Optional[dict[str, Any]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> ClassifiedResult:
    """Classify a tool execution result into inspection notice, execution error, or defect."""
    args = arguments or {}
    metadata = meta or {}
    path = str(args.get("path", "") or args.get("file_path", "") or args.get("test_path", ""))
    lowered = result_text.lower()

    # 1. MCP Meta Checks (blocked or client error)
    if metadata.get("blocked"):
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=result_text.strip(),
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # 2. Workspace boundary & traversal violations (Genuine Execution Error)
    if (
        "outside the project workspace" in lowered
        or "absolute paths are not allowed" in lowered
        or "invalid path" in lowered
        or "traversal" in lowered
    ):
        clean_msg = result_text.replace("ERROR:", "").replace("MCP TOOL ERROR:", "").strip()
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=clean_msg,
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # 3. Security Policy & Read-Only Permissions (Genuine Execution Error)
    if (
        "read-only mode" in lowered
        or "blocked by security policy" in lowered
        or "blocked by policy" in lowered
        or "permission denied" in lowered
    ):
        clean_msg = result_text.replace("ERROR:", "").replace("MCP TOOL ERROR:", "").strip()
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=clean_msg,
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # 4. MCP Communication & Server Failures (Genuine Execution Error)
    if (
        "timed out" in lowered
        or "no client for tool" in lowered
        or "tool could not be" in lowered
    ):
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=result_text.strip(),
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # 5. Test Execution Failures (Task Defect)
    if tool_name == "run_tests":
        if "failed" in lowered or "errors" in lowered or "error running tests" in lowered:
            return ClassifiedResult(
                category=ErrorCategory.TASK_DEFECT,
                message=f"Test failure: {result_text[:200].strip()}",
                tool=tool_name,
                path=path,
                is_turn_failure=True,
                is_useful_progress=False,
            )

    # 6. Non-existent file/directory during exploratory read/list (Inspection Notice)
    # When reading or listing, a missing file/dir is an expected finding during discovery.
    is_missing_target = (
        "file does not exist" in lowered
        or "directory does not exist" in lowered
        or "not a file" in lowered
        or "not a directory" in lowered
        or "no such file or directory" in lowered
    )

    if is_missing_target:
        if tool_name in {"read_file", "list_directory", "search_files"}:
            clean_msg = result_text.replace("ERROR:", "").strip()
            return ClassifiedResult(
                category=ErrorCategory.INSPECTION_NOTICE,
                message=clean_msg,
                tool=tool_name,
                path=path,
                is_turn_failure=False,
                is_useful_progress=True,
            )
        else:
            # For mutating tools (e.g. edit_file), missing target is an execution error
            clean_msg = result_text.replace("ERROR:", "").strip()
            return ClassifiedResult(
                category=ErrorCategory.EXECUTION_ERROR,
                message=f"Cannot edit non-existent file: {clean_msg}",
                tool=tool_name,
                path=path,
                is_turn_failure=True,
                is_useful_progress=False,
            )

    # 7. I/O Exceptions during read/write (Genuine Execution Error)
    if (
        "error reading file" in lowered
        or "error writing file" in lowered
        or "error listing directory" in lowered
        or "error editing file" in lowered
    ):
        clean_msg = result_text.replace("ERROR:", "").strip()
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=clean_msg,
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # 8. Generic ERROR: prefix
    if result_text.strip().startswith("ERROR:"):
        clean_msg = result_text.replace("ERROR:", "").strip()
        return ClassifiedResult(
            category=ErrorCategory.EXECUTION_ERROR,
            message=clean_msg,
            tool=tool_name,
            path=path,
            is_turn_failure=True,
            is_useful_progress=False,
        )

    # Normal successful result
    return ClassifiedResult(
        category=ErrorCategory.INSPECTION_NOTICE,
        message=result_text[:120].strip(),
        tool=tool_name,
        path=path,
        is_turn_failure=False,
        is_useful_progress=True,
    )


def classify_error_message(message: str) -> tuple[ErrorCategory, str]:
    """Helper to classify a raw error message string."""
    lowered = message.lower()
    if (
        "file does not exist" in lowered
        or "directory does not exist" in lowered
        or "not a file" in lowered
        or "not a directory" in lowered
    ) and not ("edit_file" in lowered or "cannot edit" in lowered):
        return ErrorCategory.INSPECTION_NOTICE, message

    if "verdict: fail" in lowered or "defect" in lowered or "test failure" in lowered:
        return ErrorCategory.TASK_DEFECT, message

    return ErrorCategory.EXECUTION_ERROR, message
