"""
Unit and regression tests for evidence prefetching and fast-path synthesis.
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
from aura.config import get_settings
from aura.core import (
    McpHub, SessionMemory, ResultCache, TaskScope,
    EvidenceItem, evidence_is_sufficient, plan_prefetch_reads,
    extract_source_skeleton, is_prefetch_candidate,
    collect_prefetch_evidence, select_prefetch_policy
)
from aura.core.evidence_constants import (
    BACKEND_PREFETCH_POLICY, FRONTEND_PREFETCH_POLICY, ROOT_MANIFEST_POLICY
)
from aura.core.providers import OllamaProvider
from mcp.types import TextContent


class MockMCPClient:
    """Mock MCP client for testing."""
    def __init__(self):
        self.call_tool = AsyncMock()


class MockMCPTool:
    """Mock MCP tool for testing."""
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.inputSchema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }


class TestPrefetchCandidateScoping:
    """Test is_prefetch_candidate intent detection."""

    def test_repo_analysis_is_candidate(self):
        scope = TaskScope("Analyze the project architecture and codebase structure")
        assert is_prefetch_candidate("Analyze the project architecture and codebase structure", scope)

    def test_backend_analysis_is_candidate(self):
        scope = TaskScope("Explain what the backend server does")
        assert is_prefetch_candidate("Explain what the backend server does", scope)

    def test_dependencies_query_is_candidate(self):
        scope = TaskScope("What dependencies does this project use?")
        assert is_prefetch_candidate("What dependencies does this project use?", scope)

    def test_conversational_is_not_candidate(self):
        scope = TaskScope("hello")
        assert not is_prefetch_candidate("hello", scope)

    def test_git_task_is_not_candidate(self):
        scope = TaskScope("show git status and recent commits")
        assert not is_prefetch_candidate("show git status and recent commits", scope)

    def test_targeted_file_read_is_not_candidate(self):
        scope = TaskScope("read hello.py")
        assert not is_prefetch_candidate("read hello.py", scope)

    def test_prefetch_disabled_setting(self):
        scope = TaskScope("Analyze the project architecture")
        settings = get_settings()
        old_val = settings.prefetch.enabled
        try:
            settings.prefetch.enabled = False
            assert not is_prefetch_candidate("Analyze the project architecture", scope)
        finally:
            settings.prefetch.enabled = old_val


class TestPrefetchPolicySelection:
    """Test select_prefetch_policy mapping."""

    def test_backend_policy(self):
        scope = TaskScope("Analyze the backend api")
        assert select_prefetch_policy(scope) == BACKEND_PREFETCH_POLICY

    def test_frontend_policy(self):
        scope = TaskScope("Review the frontend react components")
        assert select_prefetch_policy(scope) == FRONTEND_PREFETCH_POLICY

    def test_general_repo_policy(self):
        scope = TaskScope("What is this project about?")
        assert select_prefetch_policy(scope) == ROOT_MANIFEST_POLICY


class TestEvidenceSufficiency:
    """Test evidence_is_sufficient logic."""

    def test_empty_items_insufficient(self):
        assert not evidence_is_sufficient([])

    def test_listings_only_insufficient(self):
        items = [EvidenceItem("fs", "list_directory", ".", "FILE main.py", True, "prefetch-listing")]
        assert not evidence_is_sufficient(items)

    def test_reads_only_insufficient(self):
        items = [EvidenceItem("fs", "read_file", "main.py", "print('hello')", True, "prefetch-read")]
        assert not evidence_is_sufficient(items)

    def test_both_listing_and_read_sufficient(self):
        items = [
            EvidenceItem("fs", "list_directory", ".", "FILE main.py", True, "prefetch-listing"),
            EvidenceItem("fs", "read_file", "main.py", "print('hello')", True, "prefetch-read")
        ]
        assert evidence_is_sufficient(items)

    def test_error_read_insufficient(self):
        items = [
            EvidenceItem("fs", "list_directory", ".", "FILE main.py", True, "prefetch-listing"),
            EvidenceItem("fs", "read_file", "main.py", "ERROR: File does not exist", False, "prefetch-read")
        ]
        assert not evidence_is_sufficient(items)


class TestExtractSourceSkeleton:
    """Test extract_source_skeleton structural summarization."""

    def test_small_file_not_truncated(self):
        content = "def test(): pass"
        skeleton, complete = extract_source_skeleton(content, budget=1000)
        assert complete is True
        assert skeleton == content

    def test_oversized_file_extracts_signals(self):
        lines = ["import os", "from typing import Any"] + ["# dummy line " + str(i) for i in range(500)] + ["export default app"]
        content = "\n".join(lines)
        skeleton, complete = extract_source_skeleton(content, budget=2500)
        assert complete is False
        assert "[HEAD of file" in skeleton
        assert "[SIGNAL LINES" in skeleton
        assert "import os" in skeleton or "export default app" in skeleton


class TestPlanPrefetchReads:
    """Test plan_prefetch_reads picking logic."""

    def test_picks_manifests_and_entrypoints(self):
        listing = "FILE package.json\nFILE server.js\nFILE yarn.lock\nFILE readme.txt"
        picks = plan_prefetch_reads(listing, "backend", BACKEND_PREFETCH_POLICY)
        assert "backend/package.json" in picks
        assert "backend/server.js" in picks
        assert "backend/yarn.lock" not in picks  # lock file filtered


class TestPrefetchLoopIntegration:
    """Test end-to-end integration of prefetch in run_agent_loop."""

    @pytest.mark.asyncio
    async def test_prefetch_sufficient_triggers_single_synthesis(self):
        """When prefetch gathers sufficient evidence, single-synthesis returns immediately."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {
            "list_directory": MockMCPTool("list_directory"),
            "read_file": MockMCPTool("read_file")
        }

        fs_client = MockMCPClient()
        # Return listing with is_error=False
        fs_client.call_tool.return_value = MagicMock(
            is_error=False,
            content=[TextContent(type="text", text="FILE pyproject.toml\nDIR src")]
        )
        read_client = MockMCPClient()
        read_client.call_tool.return_value = MagicMock(
            is_error=False,
            content=[TextContent(type="text", text='[project]\nname = "aura-v4"')]
        )
        hub.tool_clients = {
            "list_directory": fs_client,
            "read_file": read_client
        }

        provider = MagicMock(spec=OllamaProvider)
        synth_msg = MagicMock()
        synth_msg.role = "assistant"
        synth_msg.content = "This project is aura-v4 configured via pyproject.toml."
        synth_msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=synth_msg))

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop(
            "Analyze the project dependencies and configuration",
            provider, hub, memory, cache
        )

        assert "aura-v4" in result
        # Only 1 chat call to provider (the single-synthesis fast path)
        assert provider.chat.call_count == 1
        # Check that tools=None was passed (direct synthesis)
        assert provider.chat.call_args[1].get("tools") is None

    @pytest.mark.asyncio
    async def test_prefetch_insufficient_falls_back_to_loop(self):
        """When prefetch evidence is insufficient, continues in multi-turn agent loop."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {
            "list_directory": MockMCPTool("list_directory"),
            "read_file": MockMCPTool("read_file")
        }

        fs_client = MockMCPClient()
        # Return empty listing
        fs_client.call_tool.return_value = MagicMock(
            is_error=False,
            content=[TextContent(type="text", text="")]
        )
        hub.tool_clients = {
            "list_directory": fs_client,
            "read_file": MockMCPClient()
        }

        provider = MagicMock(spec=OllamaProvider)
        loop_msg = MagicMock()
        loop_msg.role = "assistant"
        loop_msg.content = "Completed through normal reasoning loop."
        loop_msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=loop_msg))

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop(
            "Analyze the project dependencies",
            provider, hub, memory, cache
        )

        assert result == "Completed through normal reasoning loop."
        # The multi-turn loop executed (which calls provider with tools)
        assert provider.chat.call_count >= 1
