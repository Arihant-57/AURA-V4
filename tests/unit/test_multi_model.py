"""Tests for model registry, model router, execution controller, and file tracker."""

import pytest
from unittest.mock import patch, MagicMock

from src.aura.core.model_registry import (
    ModelRegistry, ModelCapability, ModelRole, DEFAULT_ROLE_MAP,
    _is_embedding_model, _parse_param_count,
)
from src.aura.core.model_router import (
    ModelRouter, TaskState, TaskStage, classify_task_role, needs_multi_stage,
)
from src.aura.core.execution_controller import ExecutionController
from src.aura.core.file_tracker import InspectionTracker, FileInspection, _merge_ranges
from src.aura.core.scope import TaskScope


# ── ModelCapability ─────────────────────────────────────────────────────

class TestModelCapability:
    def test_is_small(self):
        cap = ModelCapability(name="test:4b", parameter_count=4.0, context_length=4096, supports_tools=True)
        assert cap.is_small is True
        big = ModelCapability(name="test:70b", parameter_count=70.0, context_length=4096, supports_tools=True)
        assert big.is_small is False

    def test_is_coding_specialist(self):
        coder = ModelCapability(name="qwen2.5-coder:7b", parameter_count=7.0, context_length=32768, supports_tools=True)
        assert coder.is_coding_specialist is True
        non_coder = ModelCapability(name="qwen3.5:4b", parameter_count=4.0, context_length=4096, supports_tools=True)
        assert non_coder.is_coding_specialist is False


class TestModelRegistryHelpers:
    def test_embedding_model_detection(self):
        assert _is_embedding_model("nomic-embed-text:latest") is True
        assert _is_embedding_model("qwen3.5:4b") is False

    def test_parse_param_count_from_name(self):
        count = _parse_param_count({}, {}, "qwen3.5:4b")
        assert count == 4.0

    def test_parse_param_count_from_details(self):
        details = {"parameter_size": "9.7B"}
        count = _parse_param_count(details, {}, "unknown")
        assert count == 9.7


# ── ModelRegistry ───────────────────────────────────────────────────────

class TestModelRegistry:
    def _make_registry_with_models(self, *names):
        """Create a registry with pre-loaded model capabilities."""
        reg = ModelRegistry()
        for name in names:
            param = 4.0 if "4b" in name else 7.0 if "7b" in name else 9.0
            tools = "coder" not in name.lower() or True  # all support tools for testing
            reg.models[name] = ModelCapability(
                name=name, parameter_count=param,
                context_length=32768, supports_tools=True,
            )
        reg._refreshed = True
        return reg

    def test_select_role(self):
        reg = self._make_registry_with_models("qwen3.5:4b", "qwen2.5-coder:7b", "qwen3.5:9b")
        selected = reg.select(ModelRole.CODING, require_tools=True)
        # Should pick first available from DEFAULT_ROLE_MAP[CODING]
        assert selected in ("qwen2.5-coder:7b", "qwen3.5:9b", "qwen3.5:4b")

    def test_select_fallback_when_preferred_missing(self):
        reg = self._make_registry_with_models("qwen3.5:4b")
        # CODING prefers qwen2.5-coder:7b which isn't available
        selected = reg.select(ModelRole.CODING)
        assert selected == "qwen3.5:4b"  # fallback to available

    def test_select_returns_none_when_empty(self):
        reg = ModelRegistry()
        reg._refreshed = True
        assert reg.select(ModelRole.ANALYSIS) is None

    def test_available_names(self):
        reg = self._make_registry_with_models("a:1b", "b:2b")
        assert set(reg.available_names) == {"a:1b", "b:2b"}

    def test_describe(self):
        reg = self._make_registry_with_models("qwen3.5:4b")
        desc = reg.describe()
        assert "qwen3.5:4b" in desc
        assert "tools" in desc

    def test_refresh_with_no_ollama(self):
        """Registry handles missing ollama gracefully."""
        reg = ModelRegistry()
        with patch.dict("sys.modules", {"ollama": None}):
            # This should not raise
            reg.refresh()
        assert reg._refreshed is True


# ── classify_task_role ──────────────────────────────────────────────────

class TestClassifyTaskRole:
    def test_conversational_is_lightweight(self):
        scope = TaskScope("hello")
        assert classify_task_role("hello", scope) == ModelRole.LIGHTWEIGHT

    def test_coding_task(self):
        scope = TaskScope("fix the bug in main.py")
        assert classify_task_role("fix the bug in main.py", scope) == ModelRole.CODING

    def test_analysis_task(self):
        scope = TaskScope("explain the architecture of this project")
        assert classify_task_role("explain the architecture of this project", scope) == ModelRole.ANALYSIS

    def test_verification_task(self):
        scope = TaskScope("test the login endpoint")
        assert classify_task_role("test the login endpoint", scope) == ModelRole.VERIFICATION

    def test_short_prompt_is_lightweight(self):
        scope = TaskScope("what is 2+2")
        role = classify_task_role("what is 2+2", scope)
        assert role == ModelRole.LIGHTWEIGHT

    def test_needs_multi_stage_simple(self):
        scope = TaskScope("hello")
        assert needs_multi_stage("hello", scope) is False

    def test_needs_multi_stage_complex(self):
        scope = TaskScope("fix the authentication bug in the login handler by checking the token validation logic")
        assert needs_multi_stage(
            "fix the authentication bug in the login handler by checking the token validation logic",
            scope
        ) is True


# ── TaskState ───────────────────────────────────────────────────────────

class TestTaskState:
    def test_build_handoff(self):
        state = TaskState(
            original_request="fix auth bug",
            workspace="/project",
            read_only=False,
            stage=TaskStage.CODING,
        )
        state.add_finding("Token validation missing")
        state.completed_work.append("Read auth.py")
        state.remaining_work.append("Apply fix")
        handoff = state.build_handoff()
        assert "fix auth bug" in handoff
        assert "Token validation missing" in handoff
        assert "Read auth.py" in handoff
        assert "Apply fix" in handoff
        assert "coding" in handoff

    def test_progress_ratio(self):
        state = TaskState()
        assert state.progress_ratio == 1.0
        state.record_tool_call(useful=True)
        state.record_tool_call(useful=False, repeated=True)
        assert state.progress_ratio == 0.5

    def test_is_stuck(self):
        state = TaskState()
        for _ in range(5):
            state.record_tool_call(useful=False, repeated=True)
        assert state.is_stuck is True

    def test_add_error_capped(self):
        state = TaskState()
        for i in range(25):
            state.add_error(f"error {i}")
        assert len(state.errors) == 20


# ── ModelRouter ─────────────────────────────────────────────────────────

class TestModelRouter:
    def _make_router(self, *model_names):
        reg = ModelRegistry()
        for name in model_names:
            reg.models[name] = ModelCapability(
                name=name, parameter_count=4.0,
                context_length=32768, supports_tools=True,
            )
        reg._refreshed = True
        return ModelRouter(registry=reg)

    def test_select_returns_model(self):
        router = self._make_router("qwen3.5:4b")
        result = router.select(ModelRole.LIGHTWEIGHT)
        assert result == "qwen3.5:4b"

    def test_override_forces_model(self):
        router = self._make_router("qwen3.5:4b", "qwen3.5:9b")
        router.set_override("custom-model")
        assert router.select(ModelRole.CODING) == "custom-model"

    def test_fallback_when_empty(self):
        router = self._make_router()
        # Should return the hardcoded default
        result = router.select(ModelRole.ANALYSIS)
        assert result == "qwen3.5:4b"

    def test_describe_routing(self):
        router = self._make_router("qwen3.5:4b")
        scope = TaskScope("explain this code")
        desc = router.describe_routing("explain this code", scope)
        assert "analysis" in desc.lower() or "lightweight" in desc.lower()


# ── ExecutionController ─────────────────────────────────────────────────

class TestExecutionController:
    def test_basic_budget(self):
        ctrl = ExecutionController(max_steps=5)
        assert ctrl.should_continue is True
        for _ in range(5):
            ctrl.tick(useful=True)
        # Should extend because progress ratio is 100%
        assert ctrl.should_continue is True
        assert ctrl._extended is True

    def test_stops_on_consecutive_failures(self):
        ctrl = ExecutionController(max_steps=20)
        ctrl.tick(useful=True)
        ctrl.tick(useful=False, failed=True)
        ctrl.tick(useful=False, failed=True)
        ctrl.tick(useful=False, failed=True)
        assert ctrl.should_continue is False
        assert ctrl.stop_reason == "consecutive_failures"

    def test_stops_on_consecutive_repeats(self):
        ctrl = ExecutionController(max_steps=20)
        ctrl.tick(useful=True)
        ctrl.tick(useful=False, repeated=True)
        ctrl.tick(useful=False, repeated=True)
        ctrl.tick(useful=False, repeated=True)
        assert ctrl.should_continue is False
        assert ctrl.stop_reason == "consecutive_repeats"

    def test_no_extension_when_low_progress(self):
        ctrl = ExecutionController(max_steps=3, extension_budget=3, min_progress_ratio=0.5)
        ctrl.tick(useful=False, failed=True)
        ctrl.tick(useful=True)
        ctrl.tick(useful=False, failed=True)
        # progress ratio = 1/3 = 0.33 < 0.5 threshold
        assert ctrl._extended is False
        # But consecutive failures < 3, so check budget
        assert ctrl.budget_remaining == 0
        assert ctrl.should_continue is False
        assert ctrl.stop_reason == "budget_exhausted"

    def test_extension_granted_when_progressing(self):
        ctrl = ExecutionController(max_steps=3, extension_budget=2, min_progress_ratio=0.3)
        ctrl.tick(useful=True)
        ctrl.tick(useful=True)
        ctrl.tick(useful=True)
        # All useful, ratio = 1.0 > 0.3
        assert ctrl.should_continue is True
        assert ctrl._extended is True
        assert ctrl.budget_remaining == 2

    def test_summary(self):
        ctrl = ExecutionController(max_steps=5)
        ctrl.tick(useful=True)
        ctrl.tick(useful=False, failed=True)
        s = ctrl.summary()
        assert "useful=1" in s
        assert "failed=1" in s

    def test_budget_remaining(self):
        ctrl = ExecutionController(max_steps=10, extension_budget=5)
        assert ctrl.budget_remaining == 10
        ctrl.tick(useful=True)
        assert ctrl.budget_remaining == 9


# ── FileInspection & InspectionTracker ──────────────────────────────────

class TestFileInspection:
    def test_complete_read(self):
        fi = FileInspection(path="test.py", total_lines=100)
        fi.record_read(1, 100, 100)
        assert fi.is_complete is True
        assert fi.coverage == 1.0

    def test_partial_read(self):
        fi = FileInspection(path="test.py", total_lines=200)
        fi.record_read(1, 100, 200)
        assert fi.is_complete is False
        assert fi.coverage == 0.5

    def test_overlapping_ranges(self):
        fi = FileInspection(path="test.py", total_lines=100)
        fi.record_read(1, 50)
        fi.record_read(40, 100, 100)
        assert fi.lines_read == 100
        assert fi.is_complete is True

    def test_missing_ranges(self):
        fi = FileInspection(path="test.py", total_lines=100)
        fi.record_read(1, 30, 100)
        fi.record_read(60, 100, 100)
        missing = fi.missing_ranges
        assert missing == [(31, 59)]


class TestInspectionTracker:
    def test_record_and_retrieve(self):
        tracker = InspectionTracker()
        tracker.record("src/main.py", 1, 50, 100)
        fi = tracker.get("src/main.py")
        assert fi is not None
        assert fi.coverage == 0.5

    def test_is_fully_read(self):
        tracker = InspectionTracker()
        tracker.record("test.py", 1, 100, 100)
        assert tracker.is_fully_read("test.py") is True
        assert tracker.is_fully_read("other.py") is False

    def test_normalizes_paths(self):
        tracker = InspectionTracker()
        tracker.record("src\\main.py", 1, 50, 100)
        assert tracker.get("src/main.py") is not None

    def test_summary(self):
        tracker = InspectionTracker()
        tracker.record("a.py", 1, 50, 100)
        s = tracker.summary()
        assert "a.py" in s
        assert "50%" in s


class TestMergeRanges:
    def test_empty(self):
        assert _merge_ranges([]) == []

    def test_no_overlap(self):
        assert _merge_ranges([(1, 10), (20, 30)]) == [(1, 10), (20, 30)]

    def test_overlap(self):
        assert _merge_ranges([(1, 15), (10, 30)]) == [(1, 30)]

    def test_adjacent(self):
        assert _merge_ranges([(1, 10), (11, 20)]) == [(1, 20)]
