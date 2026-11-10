"""
Unit tests for AURA V4 agent loop components.

Tests mocked model responses for deterministic unit testing.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from aura.agents.loop import execute_tool_call, run_agent_loop
from aura.core import McpHub, SessionMemory, ResultCache, TaskScope, select_active_tools, mcp_tool_to_ollama
from aura.core.providers import OllamaProvider, ProviderFactory, ModelOptions
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
        self.inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


class TestModelProvider:
    """Test Ollama provider initialization and configuration."""

    def test_create_provider_from_env(self):
        provider = ProviderFactory.create_from_env()
        assert provider.model == "qwen3.5:4b"
        assert provider.default_options.temperature == 0.0
        assert provider.default_options.num_ctx == 12288

    def test_create_provider_with_override(self):
        provider = ProviderFactory.create_ollama(
            model="qwen2.5-coder:7b",
            num_ctx=4096,
            temperature=0.5
        )
        assert provider.model == "qwen2.5-coder:7b"
        assert provider.default_options.num_ctx == 4096
        assert provider.default_options.temperature == 0.5

    def test_model_options_to_dict(self):
        opts = ModelOptions(num_ctx=4096, temperature=0.7)
        d = opts.to_dict()
        assert d["num_ctx"] == 4096
        assert d["temperature"] == 0.7


class TestToolSchemaConversion:
    """Test MCP tool to Ollama schema conversion."""

    def test_mcp_tool_to_ollama_basic(self):
        tool = MockMCPTool("read_file", "Read a file")
        schema = mcp_tool_to_ollama(tool)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read_file"
        assert "parameters" in schema["function"]

    def test_mcp_tool_to_ollama_with_scope(self):
        tool = MockMCPTool("list_directory", "List directory")
        scope = TaskScope("analyze the backend")
        schema = mcp_tool_to_ollama(tool, scope)
        assert "BACKEND TASK RULE" in schema["function"]["description"]


class TestExecuteToolCall:
    """Test tool execution with mocked MCP."""

    @pytest.mark.asyncio
    async def test_execute_allowed_tool(self):
        hub = MagicMock(spec=McpHub)
        hub.tool_clients = {"read_file": MockMCPClient()}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        hub.tools = {"read_file": MockMCPTool("read_file")}

        # Mock successful call
        hub.tool_clients["read_file"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="file content")]
        )

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("read file test")

        result, meta = await execute_tool_call(
            "read_file", {"path": "test.py"}, 1,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        assert "file content" in result
        assert not meta["error"]
        assert not meta["blocked"]

    @pytest.mark.asyncio
    async def test_execute_blocked_tool(self):
        hub = MagicMock(spec=McpHub)
        hub.tool_clients = {"read_file": MockMCPClient()}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("read file test")

        # Blocked by path policy - lock file
        result, meta = await execute_tool_call(
            "read_file", {"path": "package-lock.json"}, 1,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        assert meta["blocked"]
        assert "blocked by policy" in result

    @pytest.mark.asyncio
    async def test_execute_duplicate_call(self):
        hub = MagicMock(spec=McpHub)
        hub.tool_clients = {"read_file": MockMCPClient()}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}

        cache = ResultCache(ttl_seconds=300)
        cache.put(("read_file", json.dumps({"path": "test.py"}, sort_keys=True)), "cached result")
        memory = SessionMemory()
        scope = TaskScope("read file test")

        result, meta = await execute_tool_call(
            "read_file", {"path": "test.py"}, 1,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        assert meta["cache_hit"]
        assert result == "cached result"

    @pytest.mark.asyncio
    async def test_execute_unauthorized_tool(self):
        hub = MagicMock(spec=McpHub)
        hub.tool_clients = {}
        hub.tool_schemas = {}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("test")

        result, meta = await execute_tool_call(
            "delete_file", {"path": "test.py"}, 1,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        assert meta["error"]
        assert "not permitted" in result


class TestRunAgentLoop:
    """Test the main agent loop with mocked responses."""

    @pytest.mark.asyncio
    async def test_loop_returns_final_answer(self):
        """Test that loop returns model's final answer without tool calls."""
        provider = MagicMock(spec=OllamaProvider)
        provider.chat = AsyncMock(return_value=MagicMock(
            message=MagicMock(content="Final answer", tool_calls=None)
        ))

        hub = MagicMock(spec=McpHub)
        hub.tools = {}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("test question", provider, hub, memory, cache)
        assert result == "Final answer"

    @pytest.mark.asyncio
    async def test_loop_max_steps(self):
        """Test that loop respects max steps limit."""
        provider = MagicMock(spec=OllamaProvider)
        # Always return tool calls - should hit max steps
        func_mock = MagicMock()
        func_mock.name = "read_file"
        func_mock.arguments = {"path": "test.py"}
        tc_mock = MagicMock()
        tc_mock.function = func_mock

        provider.chat = AsyncMock(return_value=MagicMock(
            message=MagicMock(content="", role="assistant", tool_calls=[tc_mock])
        ))

        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}
        hub.tool_clients = {"read_file": MockMCPClient()}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        hub.tool_clients["read_file"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="content")]
        )

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("test", provider, hub, memory, cache)
        # ExecutionController stops on repeated calls or budget exhaustion
        assert "Steps:" in result or "consecutive" in result.lower()

    @pytest.mark.asyncio
    async def test_loop_max_steps_forces_synthesis(self):
        """Test that reaching max steps synthesizes results from gathered evidence."""
        provider = MagicMock(spec=OllamaProvider)

        # Varying tool calls to avoid duplicate turn trigger
        call_count = 0
        def make_tc():
            tc = MagicMock()
            tc.function = MagicMock()
            tc.function.name = "read_file"
            tc.function.arguments = {"path": f"test_{call_count}.py"}
            return tc

        synth_msg = MagicMock(content="Found test configuration across files", role="assistant", tool_calls=None)

        async def side_effect(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if tools is None or any("Synthesize a clear" in (m.get("content") or "") for m in messages if m.get("role") == "system"):
                return MagicMock(message=synth_msg)
            return MagicMock(message=MagicMock(content="", role="assistant", tool_calls=[make_tc()]))

        provider.chat = AsyncMock(side_effect=side_effect)

        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}
        hub.tool_clients = {"read_file": MockMCPClient()}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        hub.tool_clients["read_file"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="target content")]
        )

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("test", provider, hub, memory, cache)
        # Budget exhausted with forced synthesis
        assert "Steps:" in result
        assert "Found test configuration across files" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])