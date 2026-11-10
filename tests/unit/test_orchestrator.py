"""Tests for multi-stage orchestrator, parsing helpers, and integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.aura.core.orchestrator import (
    run_multi_stage,
    _extract_findings,
    _extract_implementation,
    _verdict_is_fail,
    _extract_defects,
    _get_section_text,
    ANALYSIS_INSTRUCTIONS,
    CODING_INSTRUCTIONS_TEMPLATE,
    VERIFICATION_INSTRUCTIONS_TEMPLATE,
)
from src.aura.core.model_router import (
    TaskState, TaskStage, ModelRouter, needs_multi_stage,
    classify_task_role,
)
from src.aura.core.model_registry import ModelRegistry, ModelCapability, ModelRole
from src.aura.core.scope import TaskScope


# ── Parsing helpers ─────────────────────────────────────────────────────

class TestGetSectionText:
    def test_extracts_section(self):
        text = "Some preamble\nFINDINGS:\n- bug in auth\n- missing test\nCHANGES:\n- fix auth"
        result = _get_section_text(text, "FINDINGS:")
        assert "bug in auth" in result
        assert "missing test" in result
        assert "fix auth" not in result

    def test_extracts_last_section(self):
        text = "REMAINING:\n- open question"
        result = _get_section_text(text, "REMAINING:")
        assert "open question" in result

    def test_missing_section_returns_empty(self):
        assert _get_section_text("no sections here", "FINDINGS:") == ""


class TestExtractFindings:
    def test_populates_state(self):
        state = TaskState(original_request="test")
        text = (
            "Analysis complete.\n"
            "FINDINGS:\n"
            "- auth module missing validation\n"
            "- session handler has race condition\n"
            "FILES:\n"
            "- src/auth.py\n"
            "- src/session.py\n"
            "CHANGES:\n"
            "- add input validation to auth.py\n"
            "REMAINING:\n"
            "- check if session lock is needed\n"
        )
        _extract_findings(text, state)
        assert len(state.findings) == 2
        assert "auth module missing validation" in state.findings[0]
        assert "src/auth.py" in state.inspected_files
        assert len(state.proposed_changes) == 1
        assert len(state.remaining_work) == 1

    def test_empty_text_no_crash(self):
        state = TaskState()
        _extract_findings("", state)
        assert len(state.findings) == 0


class TestExtractImplementation:
    def test_populates_state(self):
        state = TaskState()
        text = (
            "Done.\n"
            "IMPLEMENTED:\n"
            "- added input validation\n"
            "- fixed race condition\n"
            "MODIFIED_FILES:\n"
            "- src/auth.py\n"
            "- src/session.py\n"
        )
        _extract_implementation(text, state)
        assert len(state.completed_work) == 2
        assert state.inspected_files.get("src/auth.py") == "modified"


class TestVerdictIsFail:
    def test_pass(self):
        assert _verdict_is_fail("VERDICT: PASS\nAll tests passed.") is False

    def test_fail(self):
        assert _verdict_is_fail("VERDICT: FAIL\nTest x broke.") is True

    def test_no_verdict(self):
        assert _verdict_is_fail("Everything looks fine.") is False

    def test_case_insensitive(self):
        assert _verdict_is_fail("verdict: fail") is True
        assert _verdict_is_fail("Verdict: Pass") is False


class TestExtractDefects:
    def test_extracts(self):
        text = "VERDICT: FAIL\nDEFECTS:\n- missing return statement\n- wrong variable name"
        defects = _extract_defects(text)
        assert len(defects) == 2
        assert "missing return statement" in defects[0]

    def test_no_defects(self):
        assert _extract_defects("VERDICT: PASS") == []


# ── Multi-stage detection ───────────────────────────────────────────────

class TestMultiStageDetection:
    def test_simple_question_is_single(self):
        scope = TaskScope("what is this project")
        assert needs_multi_stage("what is this project", scope) is False

    def test_short_fix_is_single(self):
        scope = TaskScope("fix the typo")
        assert needs_multi_stage("fix the typo", scope) is False

    def test_complex_fix_is_multi(self):
        task = "fix the authentication bug in the login handler by checking the token validation logic and updating tests"
        scope = TaskScope(task)
        assert needs_multi_stage(task, scope) is True

    def test_conversational_never_multi(self):
        scope = TaskScope("hello")
        assert needs_multi_stage("hello", scope) is False

    def test_analysis_only_is_single(self):
        task = "explain the architecture of the backend module in detail showing all the service layers"
        scope = TaskScope(task)
        assert needs_multi_stage(task, scope) is False


# ── Stage instructions ──────────────────────────────────────────────────

class TestStageInstructions:
    def test_analysis_instructions_contain_key_rules(self):
        assert "DO NOT modify" in ANALYSIS_INSTRUCTIONS
        assert "FINDINGS:" in ANALYSIS_INSTRUCTIONS

    def test_coding_template_has_placeholder(self):
        assert "{handoff}" in CODING_INSTRUCTIONS_TEMPLATE
        rendered = CODING_INSTRUCTIONS_TEMPLATE.format(handoff="test context")
        assert "test context" in rendered

    def test_verification_template_has_placeholder(self):
        assert "{handoff}" in VERIFICATION_INSTRUCTIONS_TEMPLATE
        rendered = VERIFICATION_INSTRUCTIONS_TEMPLATE.format(handoff="test context")
        assert "VERDICT:" in rendered


# ── Orchestrator integration ────────────────────────────────────────────

def _make_router(*model_names):
    reg = ModelRegistry()
    for name in model_names:
        reg.models[name] = ModelCapability(
            name=name, parameter_count=4.0,
            context_length=32768, supports_tools=True,
        )
    reg._refreshed = True
    return ModelRouter(registry=reg)


class TestRunMultiStage:
    @pytest.mark.asyncio
    async def test_read_only_skips_coding(self):
        """In read-only mode, coding stage should be skipped."""
        router = _make_router("qwen3.5:4b")

        hub = MagicMock()
        hub.workspace = "/project"
        hub.read_only = True
        hub.tools = {}
        hub.tool_clients = {}
        hub.tool_schemas = {}
        hub.clients = MagicMock()
        hub.clients.keys = MagicMock(return_value=[])

        memory = MagicMock()
        memory.render = MagicMock(return_value="")
        memory.files = {}
        memory.dirs = {}
        memory.modified = set()
        memory.note_file = MagicMock()
        memory.note_dir = MagicMock()
        memory.note_modified = MagicMock()
        memory.clear = MagicMock()

        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.put = MagicMock()
        cache.clear = MagicMock()

        # Mock run_agent_loop to return stage-appropriate responses
        call_count = 0
        async def mock_loop(task, provider, hub, memory, cache, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "FINDINGS:\n- project uses Express\nFILES:\n- server.js\nCHANGES:\n- none needed"
            else:
                return "VERDICT: PASS\nDETAILS:\n- read-only analysis verified"

        with patch("src.aura.core.orchestrator.run_agent_loop", side_effect=mock_loop):
            result = await run_multi_stage(
                "inspect and fix the server configuration in this complex multi-file project",
                router, hub, memory, cache,
            )

        # Should have called loop twice (analysis + verification, no coding)
        assert call_count == 2
        assert "Coding skipped" in result or "read-only" in result.lower() or "PASS" in result

    @pytest.mark.asyncio
    async def test_full_pipeline_with_pass(self):
        """Full analysis → coding → verification pipeline."""
        router = _make_router("qwen3.5:4b", "qwen2.5-coder:7b")

        hub = MagicMock()
        hub.workspace = "/project"
        hub.read_only = False
        hub.tools = {}
        hub.tool_clients = {}
        hub.tool_schemas = {}
        hub.clients = MagicMock()
        hub.clients.keys = MagicMock(return_value=[])

        memory = MagicMock()
        memory.render = MagicMock(return_value="")
        memory.files = {}
        memory.dirs = {}
        memory.modified = set()
        memory.note_file = MagicMock()
        memory.note_dir = MagicMock()
        memory.note_modified = MagicMock()
        memory.clear = MagicMock()

        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.put = MagicMock()
        cache.clear = MagicMock()

        call_count = 0
        async def mock_loop(task, provider, hub, memory, cache, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "FINDINGS:\n- missing validation\nFILES:\n- auth.py"
            elif call_count == 2:
                return "IMPLEMENTED:\n- added validation\nMODIFIED_FILES:\n- auth.py"
            else:
                return "VERDICT: PASS\nDETAILS:\n- validation works"

        with patch("src.aura.core.orchestrator.run_agent_loop", side_effect=mock_loop):
            result = await run_multi_stage(
                "fix the authentication validation bug in the login handler across multiple modules",
                router, hub, memory, cache,
            )

        assert call_count == 3  # analysis + coding + verification
        assert "PASS" in result

    @pytest.mark.asyncio
    async def test_correction_cycle_on_fail(self):
        """Verification FAIL triggers bounded correction."""
        router = _make_router("qwen3.5:4b")

        hub = MagicMock()
        hub.workspace = "/project"
        hub.read_only = False
        hub.tools = {}
        hub.tool_clients = {}
        hub.tool_schemas = {}
        hub.clients = MagicMock()
        hub.clients.keys = MagicMock(return_value=[])

        memory = MagicMock()
        memory.render = MagicMock(return_value="")
        memory.files = {}
        memory.dirs = {}
        memory.modified = set()
        memory.note_file = MagicMock()
        memory.note_dir = MagicMock()
        memory.note_modified = MagicMock()
        memory.clear = MagicMock()

        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.put = MagicMock()
        cache.clear = MagicMock()

        call_count = 0
        async def mock_loop(task, provider, hub, memory, cache, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "FINDINGS:\n- bug found\nFILES:\n- main.py"
            elif call_count == 2:
                return "IMPLEMENTED:\n- attempted fix\nMODIFIED_FILES:\n- main.py"
            elif call_count == 3:
                return "VERDICT: FAIL\nDEFECTS:\n- fix is incomplete"
            elif call_count == 4:
                return "IMPLEMENTED:\n- corrected fix\nMODIFIED_FILES:\n- main.py"
            else:
                return "VERDICT: PASS\nDETAILS:\n- all good now"

        with patch("src.aura.core.orchestrator.run_agent_loop", side_effect=mock_loop):
            result = await run_multi_stage(
                "fix the critical bug in the main handler that causes crashes when processing multiple requests simultaneously",
                router, hub, memory, cache,
            )

        # analysis(1) + coding(2) + verify-fail(3) + correction(4) + re-verify(5)
        assert call_count == 5
        assert "PASS" in result


class TestContextPreservation:
    def test_handoff_includes_all_fields(self):
        state = TaskState(
            original_request="fix auth",
            workspace="/project",
            read_only=False,
            stage=TaskStage.CODING,
        )
        state.findings.append("token not validated")
        state.inspected_files["auth.py"] = "read"
        state.completed_work.append("analysis done")
        state.proposed_changes.append("add validation")
        state.remaining_work.append("write tests")
        state.errors.append("initial read failed")

        handoff = state.build_handoff()
        assert "fix auth" in handoff
        assert "/project" in handoff
        assert "rw" in handoff
        assert "coding" in handoff
        assert "token not validated" in handoff
        assert "auth.py" in handoff
        assert "analysis done" in handoff
        assert "add validation" in handoff
        assert "write tests" in handoff
        assert "initial read failed" in handoff

    def test_stage_transitions(self):
        state = TaskState(stage=TaskStage.ANALYSIS)
        assert state.stage == TaskStage.ANALYSIS
        state.stage = TaskStage.CODING
        assert state.stage == TaskStage.CODING
        state.stage = TaskStage.VERIFICATION
        assert state.stage == TaskStage.VERIFICATION
        state.stage = TaskStage.COMPLETE
        assert state.stage == TaskStage.COMPLETE


# ── Regression tests for identified defects ──────────────────────────────

class TestMultiStageFinalReportDefect:
    """Verifies that analysis findings and verification verdicts survive into the final report."""

    @pytest.mark.asyncio
    async def test_analysis_findings_survive_when_coding_skipped(self):
        """In read-only mode, analysis findings must be in the final return string."""
        router = _make_router("qwen3.5:4b")

        hub = MagicMock()
        hub.workspace = "/readonly_project"
        hub.read_only = True
        hub.tools = {}
        hub.tool_clients = {}
        hub.tool_schemas = {}
        hub.clients = MagicMock()
        hub.clients.keys = MagicMock(return_value=[])

        memory = MagicMock()
        memory.render = MagicMock(return_value="")
        memory.files = {}
        memory.dirs = {}
        memory.modified = set()

        cache = MagicMock()
        cache.get = MagicMock(return_value=None)

        call_count = 0
        async def mock_loop(task, provider, hub, memory, cache, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (
                    "Analysis phase complete.\n"
                    "FINDINGS:\n"
                    "- genuine defect in auth middleware: missing token signature check\n"
                    "- database uses direct MongoClient with DNS SRV fallback\n"
                    "FILES:\n"
                    "- middleware/auth.js\n"
                    "- config/db.js\n"
                    "CHANGES:\n"
                    "- add jwt.verify signature validation\n"
                )
            else:
                return "VERDICT: PASS\nDETAILS:\n- all read-only checks verified"

        with patch("src.aura.core.orchestrator.run_agent_loop", side_effect=mock_loop):
            result = await run_multi_stage(
                "inspect the auth middleware and report defects in this complex multi-tier project",
                router, hub, memory, cache,
            )

        # 1. Analysis findings MUST be present in the output
        assert "genuine defect in auth middleware: missing token signature check" in result
        assert "database uses direct MongoClient with DNS SRV fallback" in result

        # 2. Coding skipped note must be present
        assert "Skipped (workspace is in read-only mode)" in result

        # 3. Verification verdict must be present
        assert "VERDICT: PASS" in result

        # 4. Inspected files must be present
        assert "middleware/auth.js" in result


class TestPunctuationInsensitiveClassification:
    """Verifies punctuation-insensitive classification and single/multi-stage boundary."""

    def test_fix_with_and_without_punctuation(self):
        scope1 = TaskScope("fix authentication bug")
        assert classify_task_role("fix authentication bug", scope1) == ModelRole.CODING
        assert needs_multi_stage("fix authentication bug", scope1) is False

        scope2 = TaskScope("fix, authentication bug")
        assert classify_task_role("fix, authentication bug", scope2) == ModelRole.CODING
        assert needs_multi_stage("fix, authentication bug", scope2) is False

    def test_implement_with_and_without_punctuation(self):
        scope1 = TaskScope("implement.")
        assert classify_task_role("implement.", scope1) == ModelRole.CODING
        assert needs_multi_stage("implement.", scope1) is False

        scope2 = TaskScope("implement feature")
        assert classify_task_role("implement feature", scope2) == ModelRole.CODING
        assert needs_multi_stage("implement feature", scope2) is False

    def test_refactor_with_punctuation(self):
        scope = TaskScope("refactor, backend")
        assert classify_task_role("refactor, backend", scope) == ModelRole.CODING
        assert needs_multi_stage("refactor, backend", scope) is False

    def test_simple_conversational_questions(self):
        scope = TaskScope("hello, how are you?")
        assert classify_task_role("hello, how are you?", scope) == ModelRole.LIGHTWEIGHT
        assert needs_multi_stage("hello, how are you?", scope) is False

    def test_single_file_inspection_tasks(self):
        scope = TaskScope("read package.json and explain the dependencies")
        assert classify_task_role("read package.json and explain the dependencies", scope) == ModelRole.ANALYSIS
        assert needs_multi_stage("read package.json and explain the dependencies", scope) is False

    def test_multi_stage_task_with_punctuation(self):
        task = "fix, authentication bug in the login handler by checking the token validation logic across the modules and running tests"
        scope = TaskScope(task)
        assert classify_task_role(task, scope) == ModelRole.CODING
        assert needs_multi_stage(task, scope) is True


class TestFormatTaskMetricsDefect:
    """Verifies that format_task_metrics accurately captures actual provider usage without inventing counts."""

    def test_single_provider_metrics_reported(self):
        from src.aura.agents.aura_v4 import format_task_metrics
        from src.aura.core.providers import ModelOptions

        mock_provider = MagicMock()
        mock_provider.model = "qwen3.5:4b"
        mock_provider.last_prompt_eval_count = 120
        mock_provider.last_eval_count = 45
        mock_provider.total_prompt_eval_count = 350
        mock_provider.default_options = ModelOptions(num_ctx=12288)

        formatted = format_task_metrics([mock_provider])
        assert "qwen3.5:4b" in formatted
        assert "prompt_eval_tokens=120" in formatted
        assert "eval_tokens=45" in formatted
        assert "total_prompt_tokens=350" in formatted
        assert "num_ctx=12288" in formatted

    def test_missing_metrics_handled_honestly(self):
        from src.aura.agents.aura_v4 import format_task_metrics

        mock_provider = MagicMock()
        mock_provider.model = "custom_model"
        mock_provider.last_prompt_eval_count = None
        mock_provider.last_eval_count = None
        mock_provider.total_prompt_eval_count = 0
        mock_provider.default_options = None

        formatted = format_task_metrics([mock_provider])
        assert "prompt_eval_tokens=unavailable" in formatted
        assert "eval_tokens=unavailable" in formatted
        assert "total_prompt_tokens=0" in formatted

    def test_multi_stage_metrics_aggregated(self):
        from src.aura.agents.aura_v4 import format_task_metrics

        p1 = MagicMock()
        p1.model = "qwen3.5:9b"
        p1.total_prompt_eval_count = 500
        p1.total_eval_count = 150

        p2 = MagicMock()
        p2.model = "qwen2.5-coder:7b"
        p2.total_prompt_eval_count = 800
        p2.total_eval_count = 300

        formatted = format_task_metrics([p1, p2])
        assert "multi-stage" in formatted
        assert "total_prompt_tokens=1300" in formatted
        assert "total_eval_tokens=450" in formatted
        assert "qwen3.5:9b(prompt=500, eval=150)" in formatted
        assert "qwen2.5-coder:7b(prompt=800, eval=300)" in formatted

    def test_empty_providers_handled(self):
        from src.aura.agents.aura_v4 import format_task_metrics

        assert format_task_metrics([]) == "[Metrics: unavailable]"
        assert format_task_metrics([None]) == "[Metrics: unavailable]"

    @pytest.mark.asyncio
    async def test_run_task_multi_stage_invocation(self):
        from src.aura.agents.aura_v4 import _run_task

        mock_router = MagicMock()
        mock_provider = MagicMock()
        mock_hub = MagicMock()
        mock_hub.workspace = "/workspace"
        mock_hub.read_only = False
        mock_memory = MagicMock()
        mock_cache = MagicMock()

        with patch("src.aura.agents.aura_v4.needs_multi_stage", return_value=True):
            with patch("src.aura.agents.aura_v4.run_multi_stage", AsyncMock(return_value="report")) as mock_rms:
                answer, providers = await _run_task(
                    "complex task", mock_router, mock_provider,
                    mock_hub, mock_memory, mock_cache, None
                )
                assert answer == "report"
                assert mock_rms.called

