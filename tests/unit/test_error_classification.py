"""
Regression tests for error and notice classification in AURA V4.

Covers:
1. Missing file during existence inspection.
2. Optional file missing.
3. Permission denied.
4. Workspace boundary violation.
5. Filesystem MCP server failure.
6. Unexpected filesystem exception.
7. Successful creation of a previously missing file.
8. Verification failure.
9. Successful task with inspection notices.
10. Final report correctly categorizing notices and defects.
"""

import pytest
from unittest.mock import MagicMock

from src.aura.core.error_classifier import (
    ErrorCategory,
    ClassifiedResult,
    classify_tool_result,
    classify_error_message,
    is_tool_error_text,
)
from src.aura.core.model_router import TaskState, TaskStage
from src.aura.core.orchestrator import _build_final_multi_stage_report
from src.aura.core.file_tracker import InspectionTracker


class TestErrorClassification:
    """Tests for classify_tool_result and category assignments."""

    def test_missing_file_during_existence_inspection(self):
        """Missing file in read_file is an inspection notice, not an execution error or defect."""
        res = classify_tool_result(
            "read_file",
            "ERROR: File does not exist: src/aura/missing.py",
            {"path": "src/aura/missing.py"},
        )
        assert res.category == ErrorCategory.INSPECTION_NOTICE
        assert res.is_turn_failure is False
        assert res.is_useful_progress is True
        assert "File does not exist" in res.message

        # State routing check
        state = TaskState(original_request="check file")
        state.add_error("read_file: ERROR: File does not exist: src/aura/missing.py")
        assert len(state.inspection_notices) == 1
        assert len(state.execution_errors) == 0
        assert len(state.defects) == 0

    def test_optional_file_missing(self):
        """Missing optional configuration or directory is categorized as an inspection notice."""
        res_file = classify_tool_result(
            "read_file",
            "ERROR: File does not exist: pyproject.toml",
            {"path": "pyproject.toml"},
        )
        res_dir = classify_tool_result(
            "list_directory",
            "ERROR: Directory does not exist: docs",
            {"path": "docs"},
        )
        assert res_file.category == ErrorCategory.INSPECTION_NOTICE
        assert res_dir.category == ErrorCategory.INSPECTION_NOTICE
        assert res_file.is_turn_failure is False
        assert res_dir.is_turn_failure is False

    def test_permission_denied(self):
        """Read-only permission blocking is a genuine execution error."""
        res = classify_tool_result(
            "write_file",
            "ERROR: Workspace is in read-only mode. File modifications are blocked by security policy.",
            {"path": "foo.py", "content": "print(1)"},
        )
        assert res.category == ErrorCategory.EXECUTION_ERROR
        assert res.is_turn_failure is True
        assert res.is_useful_progress is False

        state = TaskState(original_request="test write")
        state.add_error("write_file: ERROR: Workspace is in read-only mode.")
        assert len(state.execution_errors) == 1
        assert len(state.inspection_notices) == 0
        assert len(state.defects) == 0

    def test_workspace_boundary_violation(self):
        """Path traversal and absolute path escaping are execution errors."""
        res_traversal = classify_tool_result(
            "read_file",
            "ERROR: Access outside the project workspace is not allowed.",
            {"path": "../../etc/shadow"},
        )
        res_absolute = classify_tool_result(
            "read_file",
            "ERROR: Absolute paths are not allowed.",
            {"path": "C:/Windows/System32/drivers/etc/hosts"},
        )
        assert res_traversal.category == ErrorCategory.EXECUTION_ERROR
        assert res_traversal.is_turn_failure is True
        assert res_absolute.category == ErrorCategory.EXECUTION_ERROR
        assert res_absolute.is_turn_failure is True

    def test_filesystem_mcp_server_failure(self):
        """MCP server timeouts and communication failures are execution errors."""
        res_timeout = classify_tool_result(
            "read_file",
            "MCP TOOL ERROR: tool 'read_file' timed out after 30s.",
            {"path": "huge.log"},
            meta={"error": True},
        )
        res_noclient = classify_tool_result(
            "read_file",
            "MCP ERROR: no client for tool 'read_file'.",
            meta={"error": True},
        )
        assert res_timeout.category == ErrorCategory.EXECUTION_ERROR
        assert res_timeout.is_turn_failure is True
        assert res_noclient.category == ErrorCategory.EXECUTION_ERROR
        assert res_noclient.is_turn_failure is True

    def test_unexpected_filesystem_exception(self):
        """I/O read/write exceptions are execution errors, not inspection notices."""
        res_read = classify_tool_result(
            "read_file",
            "ERROR reading file: [Errno 13] Permission denied: 'locked.txt'",
            {"path": "locked.txt"},
        )
        res_write = classify_tool_result(
            "write_file",
            "ERROR writing file: [Errno 28] No space left on device",
            {"path": "out.txt"},
        )
        assert res_read.category == ErrorCategory.EXECUTION_ERROR
        assert res_read.is_turn_failure is True
        assert res_write.category == ErrorCategory.EXECUTION_ERROR
        assert res_write.is_turn_failure is True

    def test_edit_nonexistent_file_is_execution_error(self):
        """Attempting to edit_file on a non-existent file is an execution error, not an inspection notice."""
        res = classify_tool_result(
            "edit_file",
            "ERROR: File does not exist: missing.py",
            {"path": "missing.py"},
        )
        assert res.category == ErrorCategory.EXECUTION_ERROR
        assert res.is_turn_failure is True

    def test_verification_failure_is_task_defect(self):
        """Verification failure verdict is a task defect."""
        state = TaskState(original_request="implement feature")
        state.add_error("VERDICT: FAIL - missing unit tests")
        assert len(state.defects) == 1
        assert len(state.inspection_notices) == 0
        assert len(state.execution_errors) == 0

    def test_successful_creation_of_previously_missing_file(self):
        """Workflow: missing file inspected -> created -> verified: no defects in report."""
        state = TaskState(
            original_request="Create src/aura/version.py",
            workspace="/app",
            read_only=False,
            stage=TaskStage.COMPLETE,
        )
        # Step 1: Inspection noticed file was missing
        state.add_inspection_notice("read_file: File does not exist: src/aura/version.py")
        # Step 2: Implementation succeeded
        state.completed_work.append("Analysis completed with qwen3.5:4b")
        state.completed_work.append("Coding completed with qwen2.5-coder:7b")
        state.completed_work.append("Verification completed with qwen3.5:4b")

        tracker = InspectionTracker()
        tracker.record("src/aura/version.py", 1, 1, 1)

        results = [
            "## Analysis\nFINDINGS:\n- File does not exist: src/aura/version.py\nCHANGES:\n- Create version.py",
            "## Implementation\nIMPLEMENTED: Created src/aura/version.py",
            "## Verification\nVERDICT: PASS\nDETAILS: File created and verified",
        ]

        report = _build_final_multi_stage_report("Create src/aura/version.py", state, tracker, results)

        # Status must be COMPLETE, not FAILED
        assert "**Status:** COMPLETE" in report
        # Inspection notice must be present
        assert "### Inspection Notices" in report
        assert "File does not exist: src/aura/version.py" in report
        # Defects / Errors sections must NOT be present
        assert "### Task Defects" not in report
        assert "### Execution Errors" not in report
        assert "### Defects / Errors" not in report

    def test_successful_task_with_inspection_notices(self):
        """Final report correctly renders inspection notices without labeling them as defects."""
        state = TaskState(
            original_request="Search repo",
            stage=TaskStage.COMPLETE,
        )
        state.add_inspection_notice("read_file: File does not exist: setup.cfg")
        state.completed_work.append("Analysis completed with qwen3.5:4b")

        tracker = InspectionTracker()
        report = _build_final_multi_stage_report("Search repo", state, tracker, ["## Analysis\nAll good"])

        assert "**Status:** COMPLETE" in report
        assert "### Inspection Notices" in report
        assert "- read_file: File does not exist: setup.cfg" in report
        assert "### Task Defects" not in report
        assert "### Execution Errors" not in report

    def test_final_report_categorizes_notices_errors_and_defects(self):
        """Final report cleanly separates notices, execution errors, and task defects."""
        state = TaskState(
            original_request="Multi-step task",
            stage=TaskStage.COMPLETE,
        )
        state.add_inspection_notice("read_file: File does not exist: optional.json")
        state.add_execution_error("write_file: Disk write warning")
        state.add_defect("Verification found regression in test_auth.py")

        tracker = InspectionTracker()
        report = _build_final_multi_stage_report("Multi-step task", state, tracker, ["Results"])

        # When defects are present, status reflects FAILED
        assert "**Status:** FAILED" in report
        assert "### Inspection Notices" in report
        assert "- read_file: File does not exist: optional.json" in report
        assert "### Execution Errors" in report
        assert "- write_file: Disk write warning" in report
        assert "### Task Defects" in report
        assert "- Verification found regression in test_auth.py" in report
