"""
Unit tests for AURA V4 orchestration bug fixes:
1. Strict JSON & text fallback parsing in agent loop (SH-4 formatting fix).
2. Subprocess dry-run execution & defect detection (P2P-3 type error & SH-2 missing file fix).
3. Sequential model eviction and unload support for low-VRAM environments.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.aura.agents.loop import _looks_like_tool_call, _parse_text_tool_calls
from src.aura.core.model_router import TaskState, TaskStage
from src.aura.core.orchestrator import _run_subprocess_dry_runs
from src.aura.core.providers import OllamaProvider


class TestTextToolCallParsing:
    """Verify regex & fallback parsing handles small-model formatting slips."""

    ACTIVE_TOOLS = {"read_file", "write_file", "edit_file", "list_directory", "search_code", "run_tests"}

    def test_parse_colon_syntax(self):
        text = "I need to inspect the file.\nread_file: server.js"
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "server.js"})

    def test_parse_colon_json_syntax(self):
        text = 'read_file: {"path": "server.js"}'
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "server.js"})

    def test_parse_function_call_positional(self):
        text = 'Let me inspect read_file("server.js") now.'
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "server.js"})

    def test_parse_function_call_kwargs(self):
        text = 'read_file(path="engine/protocol.cpp")'
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "engine/protocol.cpp"})

    def test_parse_action_input_format(self):
        text = "Action: read_file\nAction Input: {\"path\": \"server.js\"}"
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "server.js"})

    def test_parse_code_block_tool_syntax(self):
        text = "```read_file\nserver.js\n```"
        calls = _parse_text_tool_calls(text, self.ACTIVE_TOOLS)
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "server.js"})

    def test_looks_like_tool_call_heuristics(self):
        assert _looks_like_tool_call("read_file: server.js") is True
        assert _looks_like_tool_call('read_file("server.js")') is True
        assert _looks_like_tool_call("Action: read_file\nAction Input: ...") is True
        assert _looks_like_tool_call("<tool_call>...</tool_call>") is True
        assert _looks_like_tool_call("The code looks good and has no issues.") is False


class TestSubprocessDryRuns:
    """Verify subprocess dry-runs detect syntax defects, runtime errors, and missing files."""

    def test_missing_expected_file_detected(self, tmp_path):
        state = TaskState(workspace=str(tmp_path))
        state.inspected_files["run_tests.js"] = "created"

        defects = _run_subprocess_dry_runs(state, tmp_path)
        assert len(defects) == 1
        assert "Expected file 'run_tests.js' does not exist" in defects[0]

    def test_python_syntax_error_detected(self, tmp_path):
        bad_py = tmp_path / "broken_syntax.py"
        bad_py.write_text("def foo(\n  return 42", encoding="utf-8")

        state = TaskState(workspace=str(tmp_path))
        state.inspected_files["broken_syntax.py"] = "modified"

        defects = _run_subprocess_dry_runs(state, tmp_path)
        assert len(defects) >= 1
        assert any("Python syntax compilation failed" in d for d in defects)

    def test_python_runtime_type_error_detected(self, tmp_path):
        # Simulating the exact P2P-3 bug: str ^ str
        sim_py = tmp_path / "simulate_rs_recovery.py"
        sim_py.write_text(
            "def xor_shards(d0, d1):\n"
            "    return ''.join(str(a ^ b) for a, b in zip(d0, d1))\n"
            "if __name__ == '__main__':\n"
            "    xor_shards('0', '1')\n",
            encoding="utf-8"
        )

        state = TaskState(workspace=str(tmp_path))
        state.inspected_files["simulate_rs_recovery.py"] = "created"

        defects = _run_subprocess_dry_runs(state, tmp_path)
        assert len(defects) >= 1
        assert any("TypeError" in d for d in defects)
        assert any("simulate_rs_recovery.py" in d for d in defects)

    def test_valid_python_script_passes(self, tmp_path):
        good_py = tmp_path / "validate_config.py"
        good_py.write_text(
            "if __name__ == '__main__':\n"
            "    print('CONFIG VALID')\n",
            encoding="utf-8"
        )

        state = TaskState(workspace=str(tmp_path))
        state.inspected_files["validate_config.py"] = "created"

        defects = _run_subprocess_dry_runs(state, tmp_path)
        assert len(defects) == 0


class TestSequentialModelUnload:
    """Verify OllamaProvider model eviction logic."""

    @patch("subprocess.run")
    def test_provider_unload(self, mock_run):
        mock_run.return_value.returncode = 0
        provider = OllamaProvider(model="qwen3.5:4b")
        provider._client = MagicMock()

        success = provider.unload()
        assert success is True
        # Verify ollama stop was invoked (hidden console on Windows)
        import sys
        assert mock_run.called
        args, kwargs = mock_run.call_args
        assert args == (["ollama", "stop", "qwen3.5:4b"],)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 10
        if sys.platform == "win32":
            assert int(kwargs["creationflags"]) & 0x08000000
            assert kwargs["startupinfo"].wShowWindow == 0


class TestRoutingAndToolResilience:
    """Verify standalone snippet routing and MCP timeout recovery."""

    def test_standalone_snippet_not_multi_stage(self):
        from src.aura.core.model_router import needs_multi_stage
        from src.aura.core.scope import TaskScope

        snippet = "Write a 3-line Python function to add two numbers and output only a code block."
        assert needs_multi_stage(snippet, TaskScope(snippet)) is False

        project_fix = "Fix the validation bug in server.js and add unit tests for it"
        assert needs_multi_stage(project_fix, TaskScope(project_fix)) is True

    @pytest.mark.asyncio
    async def test_tool_timeout_marks_server_broken_and_skips_memory(self):
        import asyncio
        from unittest.mock import AsyncMock
        from src.aura.agents.loop import execute_tool_call
        from src.aura.core import ResultCache, SessionMemory, TaskScope

        hub = MagicMock()
        hub.tool_schemas = {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        hub.tool_server = {"read_file": "filesystem"}
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        hub.tool_clients = {"read_file": client}

        memory = SessionMemory()
        cache = ResultCache()
        out, meta = await execute_tool_call(
            "read_file",
            {"path": "missing.py"},
            step=1,
            active_tool_names={"read_file"},
            hub=hub,
            cache=cache,
            memory=memory,
            scope=TaskScope("read missing.py"),
        )
        assert meta["error"] is True
        assert "timed out" in out
        hub.mark_broken.assert_called_once_with("filesystem")
        assert "missing.py" not in memory.files
