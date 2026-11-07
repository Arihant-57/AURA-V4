"""
Regression tests for AURA V4 agent loop fixes.

Tests the specific bugs that caused "Maximum steps reached without final answer":
1. Tool call serialization (RC-1)
2. Within-turn deduplication (RC-4)
3. Empty-reply nudging (RC-3)
4. System prompt usage (RC-5)
5. Sequential tool execution with cache dedup
6. Model call retry logic
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from aura.agents.loop import (
    execute_tool_call, run_agent_loop,
    _serialize_tool_calls, _deduplicate_tool_calls,
    _force_answer_from_evidence
)
from aura.core.prompts import SYSTEM_PROMPT
from aura.core import McpHub, SessionMemory, ResultCache, TaskScope
from aura.core.providers import OllamaProvider, ModelOptions
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


class TestSerializeToolCalls:
    """Test RC-1 fix: tool call serialization."""

    def test_serialize_ollama_tool_calls(self):
        """ToolCall objects must be converted to plain dicts."""
        mock_tc = MagicMock()
        mock_tc.function.name = "read_file"
        mock_tc.function.arguments = {"path": "test.py"}

        result = _serialize_tool_calls([mock_tc])
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert result[0]["function"]["name"] == "read_file"
        assert result[0]["function"]["arguments"] == {"path": "test.py"}

    def test_serialize_string_arguments(self):
        """String arguments should be parsed as JSON."""
        mock_tc = MagicMock()
        mock_tc.function.name = "read_file"
        mock_tc.function.arguments = '{"path": "test.py"}'

        result = _serialize_tool_calls([mock_tc])
        assert result[0]["function"]["arguments"] == {"path": "test.py"}

    def test_serialize_empty_arguments(self):
        """Empty string arguments should become empty dict."""
        mock_tc = MagicMock()
        mock_tc.function.name = "git_status"
        mock_tc.function.arguments = ""

        result = _serialize_tool_calls([mock_tc])
        assert result[0]["function"]["arguments"] == {}

    def test_serialize_invalid_json_arguments(self):
        """Invalid JSON arguments should become empty dict."""
        mock_tc = MagicMock()
        mock_tc.function.name = "read_file"
        mock_tc.function.arguments = "not json"

        result = _serialize_tool_calls([mock_tc])
        assert result[0]["function"]["arguments"] == {}

    def test_serialize_skips_no_function(self):
        """Tool calls without function attribute should be skipped."""
        mock_tc = MagicMock(spec=[])  # no attributes

        result = _serialize_tool_calls([mock_tc])
        assert len(result) == 0


class TestDeduplicateToolCalls:
    """Test RC-4 fix: within-turn deduplication."""

    def test_removes_exact_duplicates(self):
        """Identical calls should be deduplicated."""
        calls = [
            ("read_file", {"path": "test.py"}),
            ("read_file", {"path": "test.py"}),
            ("read_file", {"path": "test.py"}),
        ]
        result = _deduplicate_tool_calls(calls)
        assert len(result) == 1
        assert result[0] == ("read_file", {"path": "test.py"})

    def test_keeps_different_calls(self):
        """Different calls should be preserved."""
        calls = [
            ("read_file", {"path": "a.py"}),
            ("read_file", {"path": "b.py"}),
            ("list_directory", {"path": "."}),
        ]
        result = _deduplicate_tool_calls(calls)
        assert len(result) == 3

    def test_preserves_order(self):
        """First occurrence should be kept."""
        calls = [
            ("list_directory", {"path": "."}),
            ("read_file", {"path": "a.py"}),
            ("list_directory", {"path": "."}),
        ]
        result = _deduplicate_tool_calls(calls)
        assert len(result) == 2
        assert result[0] == ("list_directory", {"path": "."})
        assert result[1] == ("read_file", {"path": "a.py"})

    def test_empty_list(self):
        """Empty input should return empty output."""
        assert _deduplicate_tool_calls([]) == []


class TestEmptyReplyNudging:
    """Test RC-3 fix: empty reply nudging."""

    @pytest.mark.asyncio
    async def test_empty_reply_nudge_then_answer(self):
        """Model that returns empty first, then answers after nudge."""
        provider = MagicMock(spec=OllamaProvider)

        # First call: model returns empty (no tools)
        empty_msg = MagicMock()
        empty_msg.role = "assistant"
        empty_msg.content = ""
        empty_msg.tool_calls = None

        # Second call: model returns answer after nudge
        answer_msg = MagicMock()
        answer_msg.role = "assistant"
        answer_msg.content = "The answer is 42."
        answer_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            MagicMock(message=empty_msg),
            MagicMock(message=answer_msg),
        ])

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("test question", provider, hub, memory, cache)
        assert result == "The answer is 42."


class TestSystemPromptUsed:
    """Test RC-5 fix: system prompt from prompts.py is used."""

    @pytest.mark.asyncio
    async def test_system_prompt_contains_rules(self):
        """Verify system prompt includes MCP tool guidance."""
        provider = MagicMock(spec=OllamaProvider)
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = "Answer"
        msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file")}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        await run_agent_loop("test", provider, hub, memory, cache)

        # Check the system message sent to the model
        call_args = provider.chat.call_args_list[0]
        messages = call_args[0][0]  # first positional arg
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "MCP tools" in system_msg["content"] or "tool-calling interface" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_system_prompt_uses_central_source(self):
        """Verify SYSTEM_PROMPT from prompts.py is what the model receives."""
        provider = MagicMock(spec=OllamaProvider)
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = "Answer"
        msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        await run_agent_loop("test", provider, hub, memory, cache)

        # Check the system message sent to the model
        call_args = provider.chat.call_args_list[0]
        messages = call_args[0][0]
        system_msg = messages[0]

        # Verify key content from SYSTEM_PROMPT is present
        content = system_msg["content"]
        assert "tool-calling interface" in content, "Missing tool-calling interface guidance"
        assert "CODE SEMANTICS CHECKS" in content, "Missing CODE SEMANTICS CHECKS from central prompt"


class TestSequentialExecution:
    """Test that tool calls execute sequentially (not in parallel)."""

    @pytest.mark.asyncio
    async def test_sequential_allows_cache_dedup(self):
        """Sequential execution means second identical call hits cache."""
        hub = MagicMock(spec=McpHub)
        mock_client = MockMCPClient()
        mock_client.call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="content")]
        )
        hub.tool_clients = {"read_file": mock_client}
        hub.tool_schemas = {
            "read_file": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
        }

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("test")

        # Execute first call
        result1, meta1 = await execute_tool_call(
            "read_file", {"path": "test.py"}, 0,
            active_tool_names={"read_file"}, hub=hub, cache=cache,
            memory=memory, scope=scope
        )
        assert not meta1["cache_hit"]

        # Execute second identical call — should hit cache
        result2, meta2 = await execute_tool_call(
            "read_file", {"path": "test.py"}, 1,
            active_tool_names={"read_file"}, hub=hub, cache=cache,
            memory=memory, scope=scope
        )
        assert meta2["cache_hit"]
        assert meta2["duplicate"]


class TestToolCallWithToolResult:
    """Test complete tool-call-to-result flow."""

    @pytest.mark.asyncio
    async def test_tool_call_then_answer(self):
        """Full flow: tool call -> result -> final answer."""
        hub = MagicMock(spec=McpHub)
        hub.tools = {"read_file": MockMCPTool("read_file", "Read a file")}
        mock_client = MockMCPClient()
        mock_client.call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="def hello(): return 'world'")]
        )
        hub.tool_clients = {"read_file": mock_client}
        hub.tool_schemas = {
            "read_file": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
        }

        provider = MagicMock(spec=OllamaProvider)

        # First: tool call
        tc_msg = MagicMock()
        tc_msg.role = "assistant"
        tc_msg.content = ""
        func = MagicMock()
        func.name = "read_file"
        func.arguments = {"path": "hello.py"}
        tc = MagicMock()
        tc.function = func
        tc_msg.tool_calls = [tc]

        # Second: final answer
        answer_msg = MagicMock()
        answer_msg.role = "assistant"
        answer_msg.content = "The file contains a hello function that returns 'world'."
        answer_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            MagicMock(message=tc_msg),
            MagicMock(message=answer_msg),
        ])

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("read hello.py", provider, hub, memory, cache)
        assert "hello" in result.lower() or "world" in result.lower()


class TestForceAnswerFromEvidence:
    """Test the _force_answer_from_evidence fallback."""

    @pytest.mark.asyncio
    async def test_force_answer_with_evidence(self):
        """Should synthesize answer from tool results."""
        provider = MagicMock(spec=OllamaProvider)
        provider.chat = AsyncMock(return_value=MagicMock(
            message=MagicMock(content="Synthesized answer from evidence")
        ))

        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "What is in test.py?"},
            {"role": "tool", "content": "def test(): pass"},
        ]
        scope = TaskScope("What is in test.py?")

        result = await _force_answer_from_evidence(messages, provider, scope)
        assert "Synthesized answer" in result or "def test" in result

    @pytest.mark.asyncio
    async def test_force_answer_no_evidence(self):
        """Should return helpful message when no evidence exists."""
        provider = MagicMock(spec=OllamaProvider)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "test"},
        ]
        scope = TaskScope("test")

        result = await _force_answer_from_evidence(messages, provider, scope)
        assert "could not gather" in result.lower() or "information" in result.lower()


class TestMcpHealOnBrokenServers:
    """Test MCP server recovery when servers are broken."""

    @pytest.mark.asyncio
    async def test_heal_called_when_servers_broken(self):
        """hub.heal() should be called to reconnect broken servers."""
        provider = MagicMock(spec=OllamaProvider)
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = "Answer"
        msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        # Create a mock hub with broken servers
        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        hub.broken = {"filesystem", "git"}  # Simulate broken servers
        hub.heal = AsyncMock()  # Mock the heal method

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        await run_agent_loop("test", provider, hub, memory, cache)

        # Verify heal was called since servers are broken
        hub.heal.assert_called_once()

    @pytest.mark.asyncio
    async def test_heal_not_called_when_servers_healthy(self):
        """hub.heal() should not be called when all servers are healthy."""
        provider = MagicMock(spec=OllamaProvider)
        msg = MagicMock()
        msg.role = "assistant"
        msg.content = "Answer"
        msg.tool_calls = None
        provider.chat = AsyncMock(return_value=MagicMock(message=msg))

        # Create a mock hub with no broken servers
        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        hub.broken = set()  # No broken servers
        hub.heal = AsyncMock()

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        await run_agent_loop("test", provider, hub, memory, cache)

        # Verify heal was NOT called since servers are healthy
        hub.heal.assert_not_called()


class TestModelRetry:
    """Test model call retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_transient_failure(self):
        """Model call should retry on transient errors."""
        provider = MagicMock(spec=OllamaProvider)

        # First call fails, second succeeds
        answer_msg = MagicMock()
        answer_msg.role = "assistant"
        answer_msg.content = "Success after retry"
        answer_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            ConnectionError("Transient failure"),
            MagicMock(message=answer_msg),
        ])

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        # Patch sleep to avoid actual delay
        with patch("aura.agents.loop.asyncio.sleep", new_callable=AsyncMock):
            result = await run_agent_loop("test", provider, hub, memory, cache)
        assert result == "Success after retry"

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        """All retry attempts fail should return error message."""
        provider = MagicMock(spec=OllamaProvider)
        provider.chat = AsyncMock(side_effect=ConnectionError("Persistent failure"))

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        with patch("aura.agents.loop.asyncio.sleep", new_callable=AsyncMock):
            result = await run_agent_loop("test", provider, hub, memory, cache)
        assert "Failed to get model response" in result

    @pytest.mark.asyncio
    async def test_permanent_error_not_retried(self):
        """ValueError (model not found, config error) should NOT be retried."""
        provider = MagicMock(spec=OllamaProvider)
        # ValueError should NOT trigger retry
        provider.chat = AsyncMock(side_effect=ValueError("Model 'missing:model' not found"))

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        # Should fail immediately without calling sleep (no retries)
        result = await run_agent_loop("test", provider, hub, memory, cache)
        assert "Model 'missing:model' not found" in result

    @pytest.mark.asyncio
    async def test_timeout_error_retries(self):
        """asyncio.TimeoutError should trigger retry."""
        provider = MagicMock(spec=OllamaProvider)

        # First call times out, second succeeds
        answer_msg = MagicMock()
        answer_msg.role = "assistant"
        answer_msg.content = "Success after timeout retry"
        answer_msg.tool_calls = None

        provider.chat = AsyncMock(side_effect=[
            asyncio.TimeoutError("Operation timed out"),
            MagicMock(message=answer_msg),
        ])

        hub = MagicMock(spec=McpHub)
        hub.tools = {}
        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        with patch("aura.agents.loop.asyncio.sleep", new_callable=AsyncMock):
            result = await run_agent_loop("test", provider, hub, memory, cache)
        assert result == "Success after timeout retry"


class TestMissingClient:
    """Test tool execution when client is missing."""

    @pytest.mark.asyncio
    async def test_missing_client_returns_error(self):
        """Tool with no client should return error, not crash."""
        hub = MagicMock(spec=McpHub)
        hub.tool_clients = {}  # no clients
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {}}}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("test")

        result, meta = await execute_tool_call(
            "read_file", {"path": "test.py"}, 0,
            active_tool_names={"read_file"}, hub=hub, cache=cache,
            memory=memory, scope=scope
        )
        assert meta["error"]
        assert "no client" in result


class TestErrorsNotCached:
    """Test that error results are not cached."""

    @pytest.mark.asyncio
    async def test_error_result_not_cached(self):
        """Tool errors should NOT be cached."""
        hub = MagicMock(spec=McpHub)
        mock_client = MockMCPClient()
        mock_client.call_tool.side_effect = Exception("Tool failed")
        hub.tool_clients = {"read_file": mock_client}
        hub.tool_schemas = {
            "read_file": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
        }

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()
        scope = TaskScope("test")

        result1, meta1 = await execute_tool_call(
            "read_file", {"path": "test.py"}, 0,
            active_tool_names={"read_file"}, hub=hub, cache=cache,
            memory=memory, scope=scope
        )
        assert meta1["error"]

        # Cache should NOT have the error
        sig = ("read_file", json.dumps({"path": "test.py"}, sort_keys=True))
        assert cache.get(sig) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
