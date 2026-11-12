"""
Regression tests for AURA V4 agent loop reliability.

Covers the 11 key operational scenarios:
1. Normal model response with no tool calls.
2. Model requests a tool -> executes -> returns result -> final answer.
3. Multiple tool calls in sequence.
4. Malformed tool-call arguments.
5. Unknown / unpermitted tool.
6. MCP tool execution failure.
7. Model-call timeout handling and retry.
8. Model / provider connection error handling.
9. Repeated / duplicate tool calls leading to termination or forced synthesis.
10. Maximum loop steps reached.
11. Session usability / recovery after failure.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aura.agents.loop import run_agent_loop
from aura.config.settings import Settings, get_settings
from aura.core import McpHub, SessionMemory, ResultCache
from aura.core.providers import OllamaProvider
from mcp.types import TextContent


class MockMCPTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


class MockMCPClient:
    def __init__(self):
        self.call_tool = AsyncMock()


def make_tool_call(name: str, arguments: dict):
    func_mock = MagicMock()
    func_mock.name = name
    func_mock.arguments = arguments
    tc_mock = MagicMock()
    tc_mock.function = func_mock
    return tc_mock


@pytest.fixture
def mock_hub():
    hub = MagicMock(spec=McpHub)
    hub.tools = {}
    hub.tool_clients = {}
    hub.tool_schemas = {}
    return hub


@pytest.fixture
def fast_settings(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_TIMEOUT", "1")
    settings = get_settings()
    settings.agent.max_steps = 4
    settings.agent.model_call_attempts = 2
    settings.agent.model_retry_delay_seconds = 0.01
    settings.agent.mcp_tool_timeout_seconds = 2
    return settings


@pytest.mark.asyncio
async def test_1_normal_model_response_no_tools(mock_hub, fast_settings):
    """Scenario 1: Normal model response with no tool calls completes immediately."""
    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(return_value=MagicMock(
        message=MagicMock(content="Hello! How can I help you?", tool_calls=None)
    ))
    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Hello", provider, mock_hub, memory, cache)

    assert result == "Hello! How can I help you?"
    assert provider.chat.call_count == 1


@pytest.mark.asyncio
async def test_2_single_tool_call_flow(mock_hub, fast_settings):
    """Scenario 2: Model requests tool -> tool executes -> returns result -> final answer."""
    tc = make_tool_call("read_file", {"path": "test.txt"})
    resp1 = MagicMock(message=MagicMock(content="", tool_calls=[tc]))
    resp2 = MagicMock(message=MagicMock(content="The file contains: 'hello world'", tool_calls=None))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=[resp1, resp2])

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    client = MockMCPClient()
    client.call_tool.return_value = MagicMock(content=[TextContent(type="text", text="hello world")])
    mock_hub.tool_clients = {"read_file": client}
    mock_hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Read test.txt", provider, mock_hub, memory, cache)

    assert "hello world" in result
    assert provider.chat.call_count == 2
    client.call_tool.assert_called_once()


@pytest.mark.asyncio
async def test_3_multiple_tool_calls_sequence(mock_hub, fast_settings):
    """Scenario 3: Sequential tool execution across steps."""
    tc1 = make_tool_call("read_file", {"path": "a.txt"})
    tc2 = make_tool_call("read_file", {"path": "b.txt"})

    resp1 = MagicMock(message=MagicMock(content="", tool_calls=[tc1]))
    resp2 = MagicMock(message=MagicMock(content="", tool_calls=[tc2]))
    resp3 = MagicMock(message=MagicMock(content="Both files read successfully.", tool_calls=None))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=[resp1, resp2, resp3])

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    client = MockMCPClient()
    client.call_tool.side_effect = [
        MagicMock(content=[TextContent(type="text", text="content of a")]),
        MagicMock(content=[TextContent(type="text", text="content of b")]),
    ]
    mock_hub.tool_clients = {"read_file": client}
    mock_hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Read a.txt and b.txt", provider, mock_hub, memory, cache)

    assert "Both files read successfully." in result
    assert provider.chat.call_count == 3
    assert client.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_4_malformed_tool_call_arguments(mock_hub, fast_settings):
    """Scenario 4: Malformed tool-call arguments handled without crashing loop."""
    tc = make_tool_call("read_file", "invalid_not_dict")
    resp1 = MagicMock(message=MagicMock(content="", tool_calls=[tc]))
    resp2 = MagicMock(message=MagicMock(content="Recovered from malformed arguments.", tool_calls=None))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=[resp1, resp2])

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    mock_hub.tool_clients = {"read_file": MockMCPClient()}
    mock_hub.tool_schemas = {"read_file": {"type": "object"}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Read test", provider, mock_hub, memory, cache)

    assert "Recovered from malformed arguments." in result
    assert provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_5_unknown_tool(mock_hub, fast_settings):
    """Scenario 5: Unknown tool call is blocked cleanly without session crash."""
    tc = make_tool_call("unknown_tool_xyz", {"query": "foo"})
    resp1 = MagicMock(message=MagicMock(content="", tool_calls=[tc]))
    resp2 = MagicMock(message=MagicMock(content="Handled unknown tool smoothly.", tool_calls=None))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=[resp1, resp2])

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Execute unknown", provider, mock_hub, memory, cache)

    assert "Handled unknown tool smoothly." in result
    assert provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_6_mcp_tool_execution_failure(mock_hub, fast_settings):
    """Scenario 6: MCP tool execution raises error, classified and returned to model."""
    tc = make_tool_call("read_file", {"path": "missing.py"})
    resp1 = MagicMock(message=MagicMock(content="", tool_calls=[tc]))
    resp2 = MagicMock(message=MagicMock(content="Missing file acknowledged.", tool_calls=None))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=[resp1, resp2])

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    client = MockMCPClient()
    client.call_tool.side_effect = RuntimeError("File not found")
    mock_hub.tool_clients = {"read_file": client}
    mock_hub.tool_schemas = {"read_file": {"type": "object"}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Check missing.py", provider, mock_hub, memory, cache)

    assert "Missing file acknowledged." in result
    assert provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_7_model_call_timeout(mock_hub, fast_settings):
    """Scenario 7: Model timeout triggers retries and terminates gracefully if exhausted."""
    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=TimeoutError("Model timed out after 60s"))

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Hanging task", provider, mock_hub, memory, cache)

    assert "Error: Failed to get model response after 2 attempts" in result
    assert provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_8_model_provider_unavailable(mock_hub, fast_settings):
    """Scenario 8: Ollama unreachable (ConnectionError) retries and fails cleanly."""
    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=ConnectionError("Cannot connect to Ollama"))

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Offline task", provider, mock_hub, memory, cache)

    assert "Error: Failed to get model response after 2 attempts" in result
    assert "Cannot connect" in result
    assert provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_9_repeated_duplicate_tool_calls(mock_hub, fast_settings):
    """Scenario 9: Repeated identical tool calls trigger duplicate detection and forced termination."""
    tc = make_tool_call("read_file", {"path": "loop.txt"})
    resp = MagicMock(message=MagicMock(content="", tool_calls=[tc]))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(return_value=resp)

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    client = MockMCPClient()
    client.call_tool.return_value = MagicMock(content=[TextContent(type="text", text="sample content")])
    mock_hub.tool_clients = {"read_file": client}
    mock_hub.tool_schemas = {"read_file": {"type": "object"}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Loop task", provider, mock_hub, memory, cache)

    assert result is not None
    # ExecutionController detects repeated actions or forced synthesis triggers
    assert "repeats" in result.lower() or "consecutive" in result.lower() or "steps:" in result.lower()


@pytest.mark.asyncio
async def test_10_maximum_loop_steps_reached(mock_hub, fast_settings):
    """Scenario 10: Loop terminates cleanly when max_steps budget is reached."""
    fast_settings.agent.max_steps = 2
    step_num = 0

    def alternating_calls(*args, **kwargs):
        nonlocal step_num
        step_num += 1
        tc = make_tool_call("read_file", {"path": f"file_{step_num}.txt"})
        return MagicMock(message=MagicMock(content="", tool_calls=[tc]))

    provider = MagicMock(spec=OllamaProvider)
    provider.chat = AsyncMock(side_effect=alternating_calls)

    mock_hub.tools = {"read_file": MockMCPTool("read_file")}
    client = MockMCPClient()
    client.call_tool.return_value = MagicMock(content=[TextContent(type="text", text="data")])
    mock_hub.tool_clients = {"read_file": client}
    mock_hub.tool_schemas = {"read_file": {"type": "object"}}

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    result = await run_agent_loop("Budget task", provider, mock_hub, memory, cache)

    assert result is not None
    assert provider.chat.call_count >= 2


@pytest.mark.asyncio
async def test_11_session_usable_after_recoverable_failure(mock_hub, fast_settings):
    """Scenario 11: Subsequent task succeeds in a fresh loop after a previous task failed."""
    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    # First task fails due to temporary connection drop
    provider1 = MagicMock(spec=OllamaProvider)
    provider1.chat = AsyncMock(side_effect=ConnectionError("Temporary blip"))
    res1 = await run_agent_loop("Task 1", provider1, mock_hub, memory, cache)
    assert "Error" in res1

    # Second task with working provider succeeds without interference
    provider2 = MagicMock(spec=OllamaProvider)
    provider2.chat = AsyncMock(return_value=MagicMock(
        message=MagicMock(content="Task 2 succeeded smoothly.", tool_calls=None)
    ))
    res2 = await run_agent_loop("Task 2", provider2, mock_hub, memory, cache)
    assert res2 == "Task 2 succeeded smoothly."
