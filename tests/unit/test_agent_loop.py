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

    def test_model_options_to_dict(self, monkeypatch):
        monkeypatch.delenv("AGENT_NUM_CTX", raising=False)
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


class TestLoopOptimizationsAndMidRunSteer:
    """Tests for schema compactness, poll stall/repeat guards, fast exits, and mid-run steer."""

    def test_mcp_tool_to_ollama_compacts_schema_and_boilerplate(self):
        class RichTool:
            name = "desktop_inspect_path"
            description = (
                "Inspect non-sensitive files or folders safely.\n\n"
                "Args:\n    path: File path to inspect.\nReturns:\n    Summary."
            )
            inputSchema = {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "title": "InspectArgs",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "description": "Optional known folder alias",
                    }
                },
            }

        converted = mcp_tool_to_ollama(RichTool())
        fn = converted["function"]
        assert "Args:" not in fn["description"]
        assert "Returns:" not in fn["description"]
        params = fn["parameters"]
        assert "$schema" not in params
        assert "title" not in params
        assert "additionalProperties" not in params
        loc_prop = params["properties"]["location"]
        assert loc_prop.get("type") == "string"
        assert "default" not in loc_prop

    @pytest.mark.asyncio
    async def test_poll_stall_guard_breaks_identical_ocr_loop_with_synthesis(self):
        provider = MagicMock(spec=OllamaProvider)
        call_num = 0

        async def _chat(messages, tools=None):
            nonlocal call_num
            call_num += 1
            if tools is None:
                return MagicMock(message=MagicMock(content="Screen shows VS Code editor.", role="assistant", tool_calls=None))
            tc = MagicMock()
            tc.function = MagicMock()
            tc.function.name = "desktop_read_screen_ocr"
            tc.function.arguments = {"mode": f"poll_{call_num}"}
            return MagicMock(message=MagicMock(content="", role="assistant", tool_calls=[tc]))

        provider.chat = AsyncMock(side_effect=_chat)
        hub = MagicMock(spec=McpHub)
        hub.workspace = "."
        hub.tools = {"desktop_read_screen_ocr": MockMCPTool("desktop_read_screen_ocr", "OCR screen")}
        hub.tool_clients = {"desktop_read_screen_ocr": MockMCPClient()}
        hub.tool_schemas = {"desktop_read_screen_ocr": {"type": "object", "properties": {"mode": {"type": "string"}}}}
        hub.tool_clients["desktop_read_screen_ocr"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="Static Window Title: VS Code")],
            is_error=False,
        )

        out = await run_agent_loop("read what is on my screen", provider, hub, SessionMemory(), ResultCache())
        assert "Screen shows VS Code editor." in out
        assert call_num <= 5

    @pytest.mark.asyncio
    async def test_fast_exit_on_ro_blocked_vs_compound_read_research_fallback(self):
        provider = MagicMock(spec=OllamaProvider)

        def _tc(name, args):
            m = MagicMock()
            m.function = MagicMock()
            m.function.name = name
            m.function.arguments = args
            return m

        # Simple desktop command hits READ-ONLY BLOCKED and exits on first turn
        provider.chat = AsyncMock(return_value=MagicMock(
            message=MagicMock(content="", role="assistant", tool_calls=[_tc("desktop_open_path", {"path": "smart-helmet"})])
        ))
        hub = MagicMock(spec=McpHub)
        hub.workspace = "."
        hub.tools = {
            "desktop_open_path": MockMCPTool("desktop_open_path", "Open folder"),
            "desktop_inspect_path": MockMCPTool("desktop_inspect_path", "Inspect folder"),
            "jarvis_research": MockMCPTool("jarvis_research", "Research topic"),
        }
        hub.tool_clients = {
            "desktop_open_path": MockMCPClient(),
            "desktop_inspect_path": MockMCPClient(),
            "jarvis_research": MockMCPClient(),
        }
        hub.tool_schemas = {k: {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}}} for k in hub.tools}
        hub.tool_clients["desktop_open_path"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="READ-ONLY BLOCKED: Switch to RW mode to open.")],
            is_error=False,
        )
        hub.tool_clients["desktop_inspect_path"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="Folder: smart-helmet\nREADME.md: ESP32 MPU6050 crash monitor")],
            is_error=False,
        )
        hub.tool_clients["jarvis_research"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="Research Summary: ESP32 IMU fall detection")],
            is_error=False,
        )

        ro_fast = await run_agent_loop("open smart-helmet on desktop", provider, hub, SessionMemory(), ResultCache())
        assert "READ-ONLY BLOCKED:" in ro_fast
        assert provider.chat.await_count == 1

        # Compound read+research task continues past single RO block and inspects + researches
        step_idx = 0

        async def _compound_chat(messages, tools=None):
            nonlocal step_idx
            step_idx += 1
            if step_idx == 1:
                return MagicMock(message=MagicMock(content="", role="assistant", tool_calls=[_tc("desktop_open_path", {"path": "smart-helmet"})]))
            if step_idx == 2:
                return MagicMock(message=MagicMock(content="", role="assistant", tool_calls=[_tc("desktop_inspect_path", {"path": "smart-helmet"})]))
            return MagicMock(message=MagicMock(content="Smart Helmet uses ESP32 + MPU6050 for crash alerts.", role="assistant", tool_calls=None))

        provider.chat = AsyncMock(side_effect=_compound_chat)
        comp_out = await run_agent_loop(
            "open smart-helmet on desktop, read what is inside and research it",
            provider, hub, SessionMemory(), ResultCache(),
        )
        assert "MPU6050" in comp_out
        assert step_idx == 3

    @pytest.mark.asyncio
    async def test_mid_run_steer_preserves_partial_and_expands_scope(self):
        import queue
        provider = MagicMock(spec=OllamaProvider)
        steer_q: queue.Queue[str] = queue.Queue()
        events: list[dict] = []
        seen_tools_per_turn: list[list[str]] = []

        def _tc(name, args):
            m = MagicMock()
            m.function = MagicMock()
            m.function.name = name
            m.function.arguments = args
            return m

        turn = 0

        async def _chat(messages, tools=None):
            nonlocal turn
            turn += 1
            seen_tools_per_turn.append([t["function"]["name"] for t in (tools or [])])
            if turn == 1:
                # Emit partial reasoning + first tool call; steer arrives while tool runs
                steer_q.put("also research online best MPU6050 thresholds")
                return MagicMock(message=MagicMock(
                    content="Partial finding: folder has server.js and README.md.",
                    role="assistant",
                    tool_calls=[_tc("read_file", {"path": "README.md"})],
                ))
            if turn == 2:
                # Newly unlocked jarvis_research must be present after expand_for_steer
                assert "jarvis_research" in seen_tools_per_turn[-1]
                return MagicMock(message=MagicMock(
                    content="",
                    role="assistant",
                    tool_calls=[_tc("jarvis_research", {"query": "MPU6050 crash threshold"})],
                ))
            assert any("Keep useful partial findings" in (m.get("content") or "") for m in messages if m.get("role") == "user")
            return MagicMock(message=MagicMock(
                content="Combined answer with README.md findings and MPU6050 research.",
                role="assistant",
                tool_calls=None,
            ))

        provider.chat = AsyncMock(side_effect=_chat)
        hub = MagicMock(spec=McpHub)
        hub.workspace = "."
        hub.tools = {
            "read_file": MockMCPTool("read_file", "Read workspace file"),
            "jarvis_research": MockMCPTool("jarvis_research", "Research topic"),
        }
        hub.tool_clients = {"read_file": MockMCPClient(), "jarvis_research": MockMCPClient()}
        hub.tool_schemas = {
            "read_file": {"type": "object", "properties": {"path": {"type": "string"}}},
            "jarvis_research": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
        hub.tool_clients["read_file"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="# Smart Helmet")],
            is_error=False,
        )
        hub.tool_clients["jarvis_research"].call_tool.return_value = MagicMock(
            content=[TextContent(type="text", text="Recommended 3G impact threshold")],
            is_error=False,
        )

        out = await run_agent_loop(
            "read README.md",
            provider,
            hub,
            SessionMemory(),
            ResultCache(),
            on_event=lambda ev: events.append(ev),
            steer_queue=steer_q,
        )
        assert "Combined answer" in out
        assert "jarvis_research" not in seen_tools_per_turn[0]
        assert "jarvis_research" in seen_tools_per_turn[1]
        assert any(e.get("event") == "partial" and "Partial finding" in e.get("text", "") for e in events)
        assert any(e.get("event") == "steer" for e in events)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])