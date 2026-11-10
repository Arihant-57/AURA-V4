"""
Task-based model routing and shared task state for AURA V4.

Routes tasks to the appropriate model role and preserves context
across model switches via a compact handoff summary.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .model_registry import ModelRegistry, ModelRole
from .scope import TaskScope
from .error_classifier import ErrorCategory, classify_error_message

logger = logging.getLogger(__name__)


class TaskStage(str, Enum):
    """Execution stages within a complex task."""
    ANALYSIS = "analysis"
    CODING = "coding"
    TESTING = "testing"
    VERIFICATION = "verification"
    COMPLETE = "complete"


@dataclass
class TaskState:
    """Shared mutable state across model switches within one task.

    Lightweight: only tracks what's needed for handoff.
    """
    original_request: str = ""
    workspace: str = ""
    read_only: bool = True
    stage: TaskStage = TaskStage.ANALYSIS
    inspected_files: dict[str, str] = field(default_factory=dict)  # path → status
    findings: list[str] = field(default_factory=list)
    proposed_changes: list[str] = field(default_factory=list)
    inspection_notices: list[str] = field(default_factory=list)
    execution_errors: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    useful_tool_calls: int = 0
    failed_tool_calls: int = 0
    repeated_tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    used_providers: list[Any] = field(default_factory=list)

    def add_finding(self, finding: str) -> None:
        if len(self.findings) < 50:  # cap to avoid unbounded growth
            self.findings.append(finding)

    def add_inspection_notice(self, notice: str) -> None:
        """Record an expected exploratory discovery (e.g. missing candidate file)."""
        if len(self.inspection_notices) < 50 and notice not in self.inspection_notices:
            self.inspection_notices.append(notice)

    def add_execution_error(self, error: str) -> None:
        """Record a genuine execution error (permission, boundary, server, I/O)."""
        if len(self.execution_errors) < 20 and error not in self.execution_errors:
            self.execution_errors.append(error)
        if len(self.errors) < 20 and error not in self.errors:
            self.errors.append(error)

    def add_defect(self, defect: str) -> None:
        """Record an actual task defect (verification failure, test failure)."""
        if len(self.defects) < 20 and defect not in self.defects:
            self.defects.append(defect)
        if len(self.errors) < 20 and defect not in self.errors:
            self.errors.append(defect)

    def add_error(self, error: str) -> None:
        """Classify and record error into inspection notices, execution errors, or defects."""
        cat, clean_msg = classify_error_message(error)
        if cat == ErrorCategory.INSPECTION_NOTICE:
            self.add_inspection_notice(clean_msg)
        elif cat == ErrorCategory.TASK_DEFECT:
            self.add_defect(clean_msg)
        else:
            self.add_execution_error(clean_msg)

    def record_tool_call(self, *, useful: bool = True, repeated: bool = False) -> None:
        self.tool_call_count += 1
        if repeated:
            self.repeated_tool_calls += 1
        elif useful:
            self.useful_tool_calls += 1
        else:
            self.failed_tool_calls += 1

    @property
    def progress_ratio(self) -> float:
        """Fraction of tool calls that were useful (0.0–1.0)."""
        if self.tool_call_count == 0:
            return 1.0
        return self.useful_tool_calls / self.tool_call_count

    @property
    def is_stuck(self) -> bool:
        """True when recent tool calls are mostly failures or repeats."""
        if self.tool_call_count < 3:
            return False
        recent_bad = self.failed_tool_calls + self.repeated_tool_calls
        return recent_bad / self.tool_call_count > 0.6

    def build_handoff(self) -> str:
        """Compact handoff summary for model switch."""
        sections = [f"## Task: {self.original_request}"]
        sections.append(f"Workspace: {self.workspace} ({'ro' if self.read_only else 'rw'})")
        sections.append(f"Stage: {self.stage.value}")

        if self.completed_work:
            sections.append("### Completed\n" + "\n".join(f"- {w}" for w in self.completed_work[-10:]))
        if self.findings:
            sections.append("### Findings\n" + "\n".join(f"- {f}" for f in self.findings[-10:]))
        if self.inspected_files:
            files_str = ", ".join(f"{p} ({s})" for p, s in list(self.inspected_files.items())[-15:])
            sections.append(f"### Inspected Files\n{files_str}")
        if self.proposed_changes:
            sections.append("### Proposed Changes\n" + "\n".join(f"- {c}" for c in self.proposed_changes[-10:]))
        if self.inspection_notices:
            sections.append("### Inspection Notices\n" + "\n".join(f"- {n}" for n in self.inspection_notices[-5:]))
        if self.execution_errors:
            sections.append("### Execution Errors\n" + "\n".join(f"- {e}" for e in self.execution_errors[-5:]))
        if self.defects:
            sections.append("### Defects\n" + "\n".join(f"- {d}" for d in self.defects[-5:]))
        elif self.errors and not self.execution_errors:
            sections.append("### Errors\n" + "\n".join(f"- {e}" for e in self.errors[-5:]))
        if self.remaining_work:
            sections.append("### Remaining\n" + "\n".join(f"- {w}" for w in self.remaining_work[-10:]))

        return "\n\n".join(sections)


def _extract_words(text: str) -> set[str]:
    """Extract individual lowercase words without punctuation using word boundaries."""
    return set(re.findall(r"\b[a-z0-9_]+\b", text.lower()))


def classify_task_role(task: str, scope: TaskScope) -> ModelRole:
    """Determine which model role fits *task* best.

    Simple heuristic; no model call needed.
    """
    lower = task.lower()

    if scope.conversational:
        return ModelRole.LIGHTWEIGHT

    # Coding signals
    coding_signals = {"fix", "implement", "create", "write", "add", "refactor",
                      "change", "modify", "update", "edit", "patch", "build"}
    # Verification signals
    verify_signals = {"test", "verify", "check", "validate", "review", "audit",
                      "coverage", "lint", "assert"}
    # Analysis signals
    analysis_signals = {"explain", "describe", "inspect", "analyze", "understand",
                        "summarize", "architecture", "structure", "how does",
                        "what does", "show me", "list", "find"}

    tokens = _extract_words(task)

    if tokens & coding_signals and not scope.conversational:
        return ModelRole.CODING
    if tokens & verify_signals:
        return ModelRole.VERIFICATION
    if tokens & analysis_signals or any(p in lower for p in ("how does", "what does", "show me")) or scope.diagnosis:
        return ModelRole.ANALYSIS

    # Default: analysis for questions, lightweight for short prompts
    word_count = len(re.findall(r"\b\w+\b", task))
    if word_count < 8 and not scope.diagnosis:
        return ModelRole.LIGHTWEIGHT
    return ModelRole.ANALYSIS


def needs_multi_stage(task: str, scope: TaskScope) -> bool:
    """True if the task warrants analysis→coding→verification pipeline."""
    if scope.conversational:
        return False
    change_words = {"fix", "implement", "refactor", "create", "add", "build", "modify", "change", "patch", "update"}
    words = _extract_words(task)
    has_change = bool(words & change_words)
    word_count = len(re.findall(r"\b\w+\b", task))
    # Simple or brief tasks (< 8 words) should never be forced into multi-stage execution
    if word_count < 8:
        return False
    # Multi-stage requires change intent combined with complexity (longer prompt or diagnosis)
    return has_change and (word_count > 12 or scope.diagnosis)


@dataclass
class ModelRouter:
    """Selects models for task roles using the registry."""

    registry: ModelRegistry
    _override: Optional[str] = None  # CLI --model override

    def set_override(self, model: str | None) -> None:
        """Force a specific model for all roles (e.g. --model flag)."""
        self._override = model

    def select(self, role: ModelRole) -> str:
        """Return the model name for *role*. Falls back to any available model."""
        if self._override:
            return self._override

        selected = self.registry.select(role, require_tools=(role != ModelRole.LIGHTWEIGHT))
        if selected:
            return selected

        # Last resort: whatever is available
        if self.registry.available_names:
            return self.registry.available_names[0]

        # Nothing at all — return default so the error surfaces at chat time
        return "qwen3.5:4b"

    def describe_routing(self, task: str, scope: TaskScope) -> str:
        role = classify_task_role(task, scope)
        model = self.select(role)
        multi = needs_multi_stage(task, scope)
        return (
            f"Role: {role.value} → Model: {model}"
            + (f" (multi-stage)" if multi else " (single-pass)")
        )
