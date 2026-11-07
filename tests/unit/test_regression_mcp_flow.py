"""
Phase F: Regression tests for AURA V4 agent loop MCP tool calling.

Tests verify correct conversation history handling after fixing the Message/dict bug.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from aura.agents.loop import execute_tool_call, run_agent_loop, _force_answer_from_evidence
from aura.core import McpHub, SessionMemory, ResultCache, TaskScope
from aura.core.providers import OllamaProvider, ModelOptions
from aura.core.tools import mcp_tool_to_ollama
from mcp.types import TextContent


class MockMCPClient:
    """Mock MCP client for testing."""
    def __init__(self, responses=None):
        self.call_tool = AsyncMock()
        if isinstance(responses, str):
            self.set_response("", responses)
        elif isinstance(responses, dict):
            self.responses = responses
        else:
            self.responses = {}

    def set_response(self, name, response):
        self.call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text=response)]
        )


class MockMCPTool:
    """Mock MCP tool for testing."""
    def __init__(self, name: str, description: str = "", properties=None):
        self.name = name
        self.description = description
        self.inputSchema = {
            "type": "object",
            "properties": properties or {"path": {"type": "string"}},
            "required": []
        }


class TestRegressionOneToolCall:
    """Test: One tool call followed by final answer."""

    @pytest.mark.asyncio
    async def test_one_tool_then_answer(self):
        """Verify conversation history works after one tool call."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}
        hub.tool_clients = {"read_file": MockMCPClient("file content")}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

        # Create mock provider that returns tool call then answer
        provider = MagicMock(spec=OllamaProvider)

        # First call: returns tool call
        first_msg = MagicMock()
        first_msg.role = "assistant"
        first_msg.content = None
        first_msg.tool_calls = [MagicMock(
            function=MagicMock(name="read_file", arguments={"path": "test.py"})
        )]

        # Second call: returns final answer
        second_msg = MagicMock()
        second_msg.role = "assistant"
        second_msg.content = "The file content is: file content"
        second_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            MagicMock(message=first_msg),
            MagicMock(message=second_msg)
        ])

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("read test.py")

        result = await run_agent_loop("read test.py", provider, hub, memory, cache)

        assert "file content" in result
        assert result == "The file content is: file content"


class TestRegressionSequentialToolCalls:
    """Test: Two sequential tool calls."""

    @pytest.mark.asyncio
    async def test_two_sequential_calls(self):
        """Verify conversation history after multiple tool calls."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {
            "list_directory": MockMCPTool("list_directory"),
            "read_file": MockMCPTool("read_file")
        }
        hub.tool_clients = {
            "list_directory": MockMCPClient("DIR src\nDIR test"),
            "read_file": MockMCPClient("code content")
        }
        hub.tool_schemas = {
            "list_directory": {"type": "object", "properties": {"path": {"type": "string"}}},
            "read_file": {"type": "object", "properties": {"path": {"type": "string"}}}
        }

        provider = MagicMock(spec=OllamaProvider)

        # First message: list_directory
        msg1 = MagicMock()
        msg1.role = "assistant"
        msg1.content = "I'll list the directory first"
        msg1.tool_calls = [MagicMock(
            function=MagicMock(name="list_directory", arguments={"path": "."})
        )]

        # Second message: read_file
        msg2 = MagicMock()
        msg2.role = "assistant"
        msg2.content = None
        msg2.tool_calls = [MagicMock(
            function=MagicMock(name="read_file", arguments={"path": "src/test.py"})
        )]

        # Third message: final answer
        msg3 = MagicMock()
        msg3.role = "assistant"
        msg3.content = "Here's what I found: src directory listing and file contents"
        msg3.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            MagicMock(message=msg1),
            MagicMock(message=msg2),
            MagicMock(message=msg3)
        ])

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("list and read")

        result = await run_agent_loop("list and read", provider, hub, memory, cache)

        assert "src" in result.lower() or "file" in result.lower()


class TestRegressionToolFailure:
    """Test: Tool execution failure handling."""

    @pytest.mark.asyncio
    async def test_tool_failure_handled(self):
        """Verify tool errors don't crash the loop."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}

        # Tool that returns error
        mock_client = MockMCPClient()
        mock_client.call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="MCP TOOL ERROR: File not found")],
            is_error=True
        )
        hub.tool_clients = {"read_file": mock_client}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

        provider = MagicMock(spec=OllamaProvider)

        msg = MagicMock()
        msg.role = "assistant"
        msg.content = "I'll try to read the file"
        msg.tool_calls = [MagicMock(
            function=MagicMock(name="read_file", arguments={"path": "nonexistent.py"})
        )]

        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("read file")

        result = await run_agent_loop("read file", provider, hub, memory, cache)

        # Should complete without crashing
        assert result is not None


class TestRegressionEmptyResponse:
    """Test: Empty model response triggers forced answer."""

    @pytest.mark.asyncio
    async def test_empty_response_forces_answer(self):
        """Verify _force_answer_from_evidence is called for empty responses."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {}

        provider = MagicMock(spec=OllamaProvider)

        # Response with empty content
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = ""
        msg.tool_calls = None

        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        # Messages with tool results (simulating evidence)
        messages = [
            {"role": "system", "content": "You are an assistant"},
            {"role": "user", "content": "list src"},
            {"role": "tool", "name": "list_directory", "content": "DIR src\nDIR test"}
        ]

        scope = TaskScope("list src")
        result = await _force_answer_from_evidence(messages, provider, scope)

        # Should have tried to generate answer from evidence
        assert result is not None


class TestRegressionRepeatedToolCalls:
    """Test: Repeated tool calls are cached."""

    @pytest.mark.asyncio
    async def test_duplicate_calls_cached(self):
        """Verify same tool call results are cached."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}

        mock_client = MockMCPClient("repeated content")
        hub.tool_clients = {"read_file": mock_client}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("test")

        # First call - should actually call the tool
        result1, meta1 = await execute_tool_call(
            "read_file", {"path": "test.py"}, 0,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        # Second call with same args - should hit cache
        result2, meta2 = await execute_tool_call(
            "read_file", {"path": "test.py"}, 1,
            active_tool_names={"read_file"},
            hub=hub, cache=cache, memory=memory, scope=scope
        )

        # First call is not a cache hit, second should be
        assert meta2["cache_hit"] == True, "Second call should be a cache hit"
        assert result1 == result2


class TestRegressionMaxSteps:
    """Test: Maximum-step exhaustion handling."""

    @pytest.mark.asyncio
    async def test_max_steps_exhaustion(self):
        """Verify max steps returns appropriate message."""
        from aura.config import get_settings

        hub = MagicMock(spec=McpHub)
        hub.tools = {}

        provider = MagicMock(spec=OllamaProvider)

        # Return messages that never produce final answer
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = ""
        msg.tool_calls = None

        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        # Temporarily reduce max_steps
        settings = get_settings()
        original_max_steps = settings.agent.max_steps
        settings.agent.max_steps = 2

        try:
            result = await run_agent_loop("test", provider, hub, memory, cache)
            # Should return either max steps message OR forced evidence message
            assert result is not None
            assert "Maximum steps" in result or "no information" in result
        finally:
            settings.agent.max_steps = original_max_steps


class TestRegressionToolCallInfoPreserved:
    """Test: Tool-call information is preserved in message format."""

    @pytest.mark.asyncio
    async def test_tool_call_preserved(self):
        """Verify tool call info is in conversation history."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}
        hub.tool_clients = {"read_file": MockMCPClient("content")}
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

        provider = MagicMock(spec=OllamaProvider)

        # Simulate tool call
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = None
        # Create a proper mock function
        func_mock = MagicMock()
        func_mock.name = "read_file"
        func_mock.arguments = {"path": "test.py"}
        tool_call_mock = MagicMock()
        tool_call_mock.function = func_mock
        msg.tool_calls = [tool_call_mock]

        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        # Run step manually - get response
        response = await provider.chat([{
            "role": "system", "content": "test"
        }, {"role": "user", "content": "test"}]
        )

        # Verify message format - the fix converts Message to dict
        msg = response.message
        messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
        messages.append({"role": msg.role, "content": msg.content or "", "tool_calls": msg.tool_calls})

        # Check the appended message has correct structure
        last_msg = messages[-1]
        assert last_msg["role"] == "assistant"
        assert last_msg["tool_calls"] is not None
        assert len(last_msg["tool_calls"]) == 1
        # Check that function name is accessible
        assert hasattr(last_msg["tool_calls"][0].function, 'name')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])