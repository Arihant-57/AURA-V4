"""
Unit and integration tests for AURA V4 Dynamic Workspace Switching and Permission System.
"""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from mcp.types import TextContent

from aura.config.workspace import normalize_workspace_path, find_referenced_directory
from aura.mcp.filesystem_server import safe_path, write_file, edit_file, _is_read_only
from aura.core.mcp_hub import McpHub
from aura.core.memory import SessionMemory
from aura.core.cache import ResultCache
from aura.core.providers import OllamaProvider
from aura.agents.loop import run_agent_loop
from aura.agents.aura_v4 import prompt_confirm


class TestWorkspacePathNormalization:
    """Test workspace path normalization, unquoting, and referenced directory extraction."""

    def test_normalize_path_with_spaces(self, tmp_path):
        target = tmp_path / "my test project with spaces"
        target.mkdir()
        resolved = normalize_workspace_path(str(target))
        assert resolved == target.resolve()
        assert resolved.exists()
        assert resolved.is_dir()

    def test_normalize_path_with_quotes(self, tmp_path):
        target = tmp_path / "quoted_project"
        target.mkdir()
        quoted_str = f'"{target}"'
        resolved = normalize_workspace_path(quoted_str)
        assert resolved == target.resolve()

    def test_normalize_nonexistent_raises(self, tmp_path):
        fake = tmp_path / "non_existent_folder_xyz"
        with pytest.raises(ValueError, match="does not exist"):
            normalize_workspace_path(str(fake))

    def test_find_referenced_directory_in_query(self, tmp_path):
        target = tmp_path / "hostel complaint management system"
        target.mkdir()
        query = f"Inspect the architecture of {target} and check backend"
        found = find_referenced_directory(query)
        assert found is not None
        assert found == target.resolve()

    def test_find_referenced_directory_quoted(self, tmp_path):
        target = tmp_path / "custom app"
        target.mkdir()
        query = f'Check files in "{target}"'
        found = find_referenced_directory(query)
        assert found is not None
        assert found == target.resolve()

    def test_find_referenced_directory_returns_none_when_no_match(self):
        query = "How do I implement a binary search tree in Python?"
        found = find_referenced_directory(query)
        assert found is None


class TestReadOnlyModeEnforcement:
    """Test read-only access mode enforcement on mutating MCP tools."""

    @pytest.fixture
    def test_env(self, tmp_path):
        old_ws = os.environ.get("AURA_WORKSPACE")
        old_ro = os.environ.get("AURA_READ_ONLY")
        os.environ["AURA_WORKSPACE"] = str(tmp_path)
        yield tmp_path
        if old_ws is not None:
            os.environ["AURA_WORKSPACE"] = old_ws
        else:
            os.environ.pop("AURA_WORKSPACE", None)
        if old_ro is not None:
            os.environ["AURA_READ_ONLY"] = old_ro
        else:
            os.environ.pop("AURA_READ_ONLY", None)

    def test_write_blocked_when_read_only(self, test_env):
        os.environ["AURA_READ_ONLY"] = "1"
        assert _is_read_only() is True
        res = write_file("new_file.txt", "hello")
        assert "ERROR:" in res
        assert "read-only mode" in res
        assert not (test_env / "new_file.txt").exists()

    def test_edit_blocked_when_read_only(self, test_env):
        target = test_env / "existing.txt"
        target.write_text("initial content", encoding="utf-8")
        os.environ["AURA_READ_ONLY"] = "1"
        res = edit_file("existing.txt", "initial", "modified")
        assert "ERROR:" in res
        assert "read-only mode" in res
        assert target.read_text(encoding="utf-8") == "initial content"

    def test_write_allowed_when_read_write(self, test_env):
        os.environ.pop("AURA_READ_ONLY", None)
        assert _is_read_only() is False
        res = write_file("allowed.txt", "allowed content")
        assert res.startswith("SUCCESS:")
        assert (test_env / "allowed.txt").exists()


class TestSafePathAbsoluteResolution:
    """Test safe_path converts in-workspace absolute paths and blocks out-of-workspace paths."""

    @pytest.fixture
    def test_env(self, tmp_path):
        old_ws = os.environ.get("AURA_WORKSPACE")
        os.environ["AURA_WORKSPACE"] = str(tmp_path)
        yield tmp_path
        if old_ws is not None:
            os.environ["AURA_WORKSPACE"] = old_ws
        else:
            os.environ.pop("AURA_WORKSPACE", None)

    def test_absolute_path_inside_workspace_allowed(self, test_env):
        subfile = test_env / "src" / "main.py"
        subfile.parent.mkdir(parents=True, exist_ok=True)
        subfile.write_text("print(1)", encoding="utf-8")

        # Pass absolute path as string
        res = safe_path(str(subfile))
        assert res.resolve() == subfile.resolve()

    def test_absolute_path_outside_workspace_blocked(self, test_env):
        outside = Path("C:/Windows/System32") if os.name == "nt" else Path("/etc/passwd")
        with pytest.raises(ValueError, match="Absolute paths are not allowed"):
            safe_path(str(outside))

    def test_directory_traversal_blocked(self, test_env):
        with pytest.raises(ValueError, match="Access outside the project workspace is not allowed"):
            safe_path("../secret.txt")


class TestMcpHubSwitchWorkspace:
    """Test McpHub dynamic workspace switching and state updates."""

    @pytest.mark.asyncio
    async def test_hub_switch_workspace(self, tmp_path):
        ws1 = tmp_path / "project1"
        ws2 = tmp_path / "project2 with spaces"
        ws1.mkdir()
        ws2.mkdir()

        hub = McpHub(ws1, read_only=False)
        assert hub.workspace == ws1
        assert hub.read_only is False

        # Switch to ws2 in read-only mode
        await hub.switch_workspace(ws2, read_only=True)
        assert hub.workspace == ws2.resolve()
        assert hub.read_only is True
        await hub.close()


class TestConsecutiveMissingPathHandling:
    """Test that agent loop detects repeated missing path errors and terminates early."""

    @pytest.mark.asyncio
    async def test_consecutive_missing_path_terminates_early(self):
        provider = MagicMock(spec=OllamaProvider)

        # Mock model emitting 3 different missing path tool calls
        call_num = 0
        def make_tc():
            nonlocal call_num
            call_num += 1
            tc = MagicMock()
            tc.function = MagicMock()
            tc.function.name = "list_directory"
            tc.function.arguments = {"path": f"non_existent_folder_{call_num}"}
            return tc

        async def fake_chat(messages, tools=None):
            return MagicMock(message=MagicMock(content="", role="assistant", tool_calls=[make_tc()]))

        provider.chat = AsyncMock(side_effect=fake_chat)

        hub = MagicMock(spec=McpHub)
        tool = MagicMock()
        tool.name = "list_directory"
        tool.inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}}
        hub.tools = {"list_directory": tool}
        hub.workspace = Path("C:/Users/ariha/test-workspace")
        hub.read_only = True

        client = MagicMock()
        async def fake_call_tool(name, args):
            p = args.get("path", "")
            return MagicMock(content=[TextContent(type="text", text=f"ERROR: Directory does not exist: {p}")], is_error=False)
        client.call_tool = fake_call_tool

        hub.tool_clients = {"list_directory": client}
        hub.tool_schemas = {"list_directory": tool.inputSchema}

        cache = ResultCache(ttl_seconds=300)
        memory = SessionMemory()

        result = await run_agent_loop("check backend", provider, hub, memory, cache)
        assert "Unable to find the requested paths" in result
        assert "/workspace <path>" in result
        assert str(hub.workspace) in result


class TestPermissionFlow:
    """Test user permission prompting logic."""

    def test_prompt_confirm_yes(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert prompt_confirm("Authorize external access?") is True

    def test_prompt_confirm_no(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert prompt_confirm("Authorize external access?") is False

    def test_prompt_confirm_empty(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert prompt_confirm("Authorize external access?") is False

    def test_prompt_confirm_auto_confirm(self):
        assert prompt_confirm("Authorize external access?", auto_confirm=True) is True

    def test_prompt_confirm_keyboard_interrupt(self, monkeypatch):
        def raise_ki(prompt):
            raise KeyboardInterrupt()
        monkeypatch.setattr("builtins.input", raise_ki)
        assert prompt_confirm("Authorize external access?") is False

