"""
Unit tests for diagnosis validation, control-flow semantics, and MCP lazy resolution.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from aura.agents.loop import run_agent_loop
from aura.agents.aura_v4 import parse_args
from aura.core import (
    McpHub, SessionMemory, ResultCache, TaskScope,
    validate_diagnosis_answer, validate_control_flow
)
from aura.core.providers import OllamaProvider
from aura.core.prompts import DIAGNOSIS_NUDGE_MESSAGE, CHANGE_PLAN_NUDGE_MESSAGE
import aura.mcp.filesystem_server as fs_server
import aura.mcp.git_server as git_server


class TestDiagnosisValidationUnit:
    """Test unit behavior of validate_diagnosis_answer."""

    def test_diagnosis_inactive_returns_none(self):
        result = validate_diagnosis_answer("any answer", diagnosis_active=False)
        assert result is None

    def test_diagnosis_missing_sections(self):
        answer = "I found a bug in the code."
        err = validate_diagnosis_answer(answer, diagnosis_active=True)
        assert err is not None
        assert "Missing required diagnosis sections" in err

    def test_diagnosis_complete_sections(self):
        answer = (
            "### OBSERVED BEHAVIOR\nThe server returns 500.\n"
            "### CONFIRMED DEFECT\nMissing null check.\n"
            "### BEHAVIOR OF UNKNOWN INTENT\nFallback route.\n"
            "### MINIMAL FIX\nAdd guard.\n"
            "### INVARIANT CHECK\nNo null deref possible."
        )
        err = validate_diagnosis_answer(answer, diagnosis_active=True)
        assert err is None

    def test_diagnosis_unlabeled_redesign_violation(self):
        answer = (
            "### OBSERVED BEHAVIOR\nState flag remains.\n"
            "### CONFIRMED DEFECT\nNone.\n"
            "### BEHAVIOR OF UNKNOWN INTENT\nRemove the state flag.\n"
            "### MINIMAL FIX\nRemove flag.\n"
            "### INVARIANT CHECK\nDone."
        )
        err = validate_diagnosis_answer(answer, diagnosis_active=True)
        assert err is not None
        assert "design change" in err.lower()


class TestControlFlowValidationUnit:
    """Test unit behavior of validate_control_flow."""

    def test_spread_deep_merge_violation(self):
        answer = "The code performs a deep merge of the configuration object."
        evidence = "const newConfig = { ...defaultConfig, ...userConfig };"
        violations = validate_control_flow(answer, evidence)
        assert len(violations) > 0
        assert "shallow merge" in violations[0]

    def test_unconditional_await_violation(self):
        answer = "The handler always returns status 200 unconditionally."
        evidence = "await saveToDatabase();\nreturn res.status(200);"
        violations = validate_control_flow(answer, evidence)
        assert len(violations) > 0
        assert "unconditional" in violations[0].lower()


class TestDiagnosisLoopNudging:
    """Test that the agent loop nudges the model on diagnosis queries."""

    @pytest.mark.asyncio
    async def test_diagnosis_nudge_then_structured_answer(self):
        """When query is a diagnosis task and answer lacks sections, nudge model."""
        provider = MagicMock(spec=OllamaProvider)

        # First reply: unstructured
        unstructured_msg = MagicMock()
        unstructured_msg.role = "assistant"
        unstructured_msg.content = "It crashes because of a null pointer."
        unstructured_msg.tool_calls = None

        # Second reply: after nudge, structured with 5 sections
        structured_msg = MagicMock()
        structured_msg.role = "assistant"
        structured_msg.content = (
            "OBSERVED BEHAVIOR: Crashes on null.\n"
            "CONFIRMED DEFECT: Null dereference.\n"
            "BEHAVIOR OF UNKNOWN INTENT: None.\n"
            "MINIMAL FIX: Check for null.\n"
            "INVARIANT CHECK: Safe."
        )
        structured_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            MagicMock(message=unstructured_msg),
            MagicMock(message=structured_msg),
        ])

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("Why is this crash happening? Diagnose the bug.", provider, hub, memory, cache)

        # Verify chat was called twice (initial answer -> nudge -> structured answer)
        assert provider.chat.call_count == 2
        # Verify the nudge was sent in the messages
        second_call_messages = provider.chat.call_args_list[1][0][0]
        user_nudges = [m for m in second_call_messages if m.get("role") == "user" and DIAGNOSIS_NUDGE_MESSAGE in m.get("content", "")]
        assert len(user_nudges) == 1
        assert "OBSERVED BEHAVIOR" in result

    @pytest.mark.asyncio
    async def test_diagnosis_bounded_nudge(self):
        """If model persists in unstructured answer, loop accepts after max_diagnosis_nudges."""
        provider = MagicMock(spec=OllamaProvider)

        persistent_msg = MagicMock()
        persistent_msg.role = "assistant"
        persistent_msg.content = "Still unstructured explanation."
        persistent_msg.tool_calls = None

        provider.chat = AsyncMock(return_value=MagicMock(message=persistent_msg))

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        # max_diagnosis_nudges is 1 by default, so it should call chat twice and then return
        result = await run_agent_loop("Diagnose this issue", provider, hub, memory, cache)
        assert result == "Still unstructured explanation."
        assert provider.chat.call_count == 2


class TestLazyMcpWorkspaceResolution:
    """Test lazy workspace resolution in filesystem and git MCP servers."""

    def test_filesystem_server_lazy_workspace(self):
        """fs_server should not crash at import time without AURA_WORKSPACE."""
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            if "AURA_WORKSPACE" in os.environ:
                del os.environ["AURA_WORKSPACE"]
            # get_workspace() raises ValueError when accessed without env
            with pytest.raises(ValueError, match="AURA_WORKSPACE is not set"):
                fs_server.get_workspace()

            # Setting env makes it resolve immediately
            os.environ["AURA_WORKSPACE"] = str(Path.cwd().resolve())
            ws = fs_server.get_workspace()
            assert ws == Path.cwd().resolve()
            assert fs_server.WORKSPACE == Path.cwd().resolve()
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env

    def test_git_server_lazy_workspace(self):
        """git_server should return error message instead of crashing without AURA_WORKSPACE."""
        old_env = os.environ.get("AURA_WORKSPACE")
        try:
            if "AURA_WORKSPACE" in os.environ:
                del os.environ["AURA_WORKSPACE"]
            result = git_server.run_git("status")
            assert "AURA_WORKSPACE is not set" in result

            os.environ["AURA_WORKSPACE"] = str(Path.cwd().resolve())
            ws = git_server.get_repository()
            assert ws == Path.cwd().resolve()
            assert git_server.REPOSITORY == Path.cwd().resolve()
        finally:
            if old_env is not None:
                os.environ["AURA_WORKSPACE"] = old_env


class TestCliTaskExecutionArgs:
    """Test CLI task execution argument parsing."""

    def test_parse_args_with_task_flag(self):
        with patch("sys.argv", ["aura_v4", "--task", "List files in src", "--workspace", "."]):
            args = parse_args()
            assert args.task == "List files in src"
            assert args.workspace == "."

    def test_parse_args_with_positional_query(self):
        with patch("sys.argv", ["aura_v4", "Explain", "workspace", "resolution"]):
            args = parse_args()
            assert args.query == ["Explain", "workspace", "resolution"]
