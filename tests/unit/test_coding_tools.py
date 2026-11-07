"""
Unit tests for AURA V4 Coding Tools:
- write_file
- edit_file
- run_tests
- ResultCache.invalidate_path
- _parse_text_tool_calls & _looks_like_tool_call
"""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aura.mcp.filesystem_server import write_file, edit_file, run_tests, read_file, get_file_diff, safe_path
from aura.core.cache import ResultCache
from aura.core.tools import normalize_tool_arguments
from aura.agents.loop import _parse_text_tool_calls, _looks_like_tool_call, prune_conversation_history
from aura.config import get_settings


class TestWriteAndEditFileTools:
    """Test write_file and edit_file tools inside an isolated workspace."""

    @pytest.fixture
    def test_workspace(self, tmp_path):
        old_env = os.environ.get("AURA_WORKSPACE")
        os.environ["AURA_WORKSPACE"] = str(tmp_path)
        yield tmp_path
        if old_env is not None:
            os.environ["AURA_WORKSPACE"] = old_env
        else:
            os.environ.pop("AURA_WORKSPACE", None)

    def test_write_file_success(self, test_workspace):
        res = write_file("src/module.py", "def hello(): return 'world'")
        assert res.startswith("SUCCESS:")
        target = test_workspace / "src" / "module.py"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "def hello(): return 'world'"

    def test_write_file_traversal_blocked(self, test_workspace):
        res = write_file("../outside.py", "malicious content")
        assert res.startswith("ERROR:")
        assert not (test_workspace.parent / "outside.py").exists()

    def test_edit_file_success(self, test_workspace):
        target = test_workspace / "math.py"
        target.write_text("def add(a, b): return a - b\n", encoding="utf-8")

        res = edit_file("math.py", "a - b", "a + b")
        assert res.startswith("SUCCESS:")
        assert "Replaced 1 occurrence(s)" in res
        assert target.read_text(encoding="utf-8") == "def add(a, b): return a + b\n"

    def test_edit_file_target_not_found(self, test_workspace):
        target = test_workspace / "math.py"
        target.write_text("def add(a, b): return a + b\n", encoding="utf-8")

        res = edit_file("math.py", "non_existent_code", "replacement")
        assert res.startswith("ERROR: Target string not found")
        # File unchanged
        assert target.read_text(encoding="utf-8") == "def add(a, b): return a + b\n"

    def test_edit_file_nonexistent(self, test_workspace):
        res = edit_file("missing.py", "target", "replacement")
        assert "File does not exist" in res


class TestRunTestsTool:
    """Test safe test execution tool inside workspace."""

    @pytest.fixture
    def test_workspace(self, tmp_path):
        old_env = os.environ.get("AURA_WORKSPACE")
        os.environ["AURA_WORKSPACE"] = str(tmp_path)
        yield tmp_path
        if old_env is not None:
            os.environ["AURA_WORKSPACE"] = old_env
        else:
            os.environ.pop("AURA_WORKSPACE", None)

    def test_run_tests_in_workspace(self, test_workspace):
        test_file = test_workspace / "test_example.py"
        test_file.write_text("def test_ok(): assert 1 + 1 == 2\n", encoding="utf-8")

        res = run_tests("test_example.py")
        assert "TEST SUITE PASSED" in res
        assert "1 passed" in res

    def test_run_tests_detects_failure(self, test_workspace):
        test_file = test_workspace / "test_fail.py"
        test_file.write_text("def test_f(): assert 1 == 2\n", encoding="utf-8")

        res = run_tests("test_fail.py")
        assert "TEST SUITE FAILED" in res
        assert "1 failed" in res

    def test_run_tests_traversal_blocked(self, test_workspace):
        res = run_tests("../test_outside.py")
        assert "ERROR invalid test_path" in res


class TestCacheInvalidationOnMutation:
    """Verify that file writes and edits invalidate stale cached file reads."""

    def test_cache_invalidates_on_path(self):
        cache = ResultCache(ttl_seconds=300)
        cache.put(("read_file", json.dumps({"path": "src/app.py"}, sort_keys=True)), "old content")
        cache.put(("read_file", json.dumps({"path": "src/other.py"}, sort_keys=True)), "other content")

        assert cache.get(("read_file", json.dumps({"path": "src/app.py"}, sort_keys=True))) == "old content"

        # Invalidate src/app.py
        cache.invalidate_path("src/app.py")

        assert cache.get(("read_file", json.dumps({"path": "src/app.py"}, sort_keys=True))) is None
        # other file remains cached
        assert cache.get(("read_file", json.dumps({"path": "src/other.py"}, sort_keys=True))) == "other content"


class TestTextToolCallParsing:
    """Verify fallback parsing of text-based tool calls emitted by small models."""

    def test_parse_tool_call_tag(self):
        content = '<tool_call>\n{"name": "read_file", "arguments": {"path": "src/main.py"}}\n</tool_call>'
        calls = _parse_text_tool_calls(content, {"read_file", "list_directory"})
        assert len(calls) == 1
        assert calls[0] == ("read_file", {"path": "src/main.py"})

    def test_parse_markdown_json_block(self):
        content = '```json\n{\n  "name": "edit_file",\n  "arguments": {\n    "path": "test.py",\n    "target": "old",\n    "replacement": "new"\n  }\n}\n```'
        calls = _parse_text_tool_calls(content, {"edit_file", "write_file"})
        assert len(calls) == 1
        assert calls[0] == ("edit_file", {"path": "test.py", "target": "old", "replacement": "new"})

    def test_parse_direct_json(self):
        content = '{"name": "list_directory", "arguments": {"path": "."}}'
        calls = _parse_text_tool_calls(content, {"list_directory"})
        assert len(calls) == 1
        assert calls[0] == ("list_directory", {"path": "."})

    def test_looks_like_tool_call_detection(self):
        assert _looks_like_tool_call('<tool_call>{"name": "read_file"}</tool_call>')
        assert _looks_like_tool_call('```json\n{"name": "read_file", "arguments": {"path": "a"}}\n```')
        assert not _looks_like_tool_call("Here is the final explanation of the bug.")


class TestArgumentNormalization:
    """Verify alias normalization into canonical schema parameters."""

    def test_path_aliases(self):
        assert normalize_tool_arguments("read_file", {"file_path": "a/b.py"}) == {"path": "a/b.py"}
        assert normalize_tool_arguments("write_file", {"filename": "c.py", "content": "x"}) == {"path": "c.py", "content": "x"}
        assert normalize_tool_arguments("list_directory", {"dir_path": "src"}) == {"path": "src"}

    def test_content_aliases(self):
        assert normalize_tool_arguments("write_file", {"path": "a.py", "code": "print(1)"}) == {"path": "a.py", "content": "print(1)"}
        assert normalize_tool_arguments("write_file", {"path": "a.py", "text": "hello"}) == {"path": "a.py", "content": "hello"}

    def test_edit_aliases(self):
        res = normalize_tool_arguments("edit_file", {
            "path": "app.py",
            "old_string": "def old():",
            "new_string": "def new():"
        })
        assert res == {"path": "app.py", "target": "def old():", "replacement": "def new():"}

    def test_run_tests_aliases(self):
        assert normalize_tool_arguments("run_tests", {"test": "tests/test_x.py"}) == {"test_path": "tests/test_x.py"}

    def test_search_aliases(self):
        assert normalize_tool_arguments("search_files", {"term": "TODO"}) == {"query": "TODO"}


class TestContextPruningAndCompression:
    """Verify that conversation history is safely pruned without breaking tool message contracts."""

    def test_prune_conversation_preserves_under_limit(self):
        settings = get_settings()
        messages = [
            {"role": "system", "content": "System instructions"},
            {"role": "user", "content": "User task"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
            {"role": "tool", "name": "read_file", "content": "line 1\nline 2"},
        ]
        pruned = prune_conversation_history(messages, settings)
        assert len(pruned) == len(messages)
        assert pruned[3]["content"] == "line 1\nline 2"

    def test_prune_shortens_older_tool_results(self):
        settings = get_settings()
        messages = [
            {"role": "system", "content": "System instructions"},
            {"role": "user", "content": "User task"},
        ]
        # Add 6 tool turns with large content
        for i in range(6):
            messages.append({"role": "assistant", "content": f"call {i}", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"f{i}.py"}}}]})
            messages.append({"role": "tool", "name": "read_file", "content": f"data {i} " + ("x" * 1000)})

        pruned = prune_conversation_history(messages, settings)
        assert len(pruned) == len(messages)

        # Tool results at index 3 and 5 are the oldest 2 (out of 6); they must be shortened
        tool_msgs = [m for m in pruned if m.get("role") == "tool"]
        assert len(tool_msgs) == 6
        assert "[PRUNED: older tool result shortened" in tool_msgs[0]["content"]
        assert "[PRUNED: older tool result shortened" in tool_msgs[1]["content"]

        # Most recent 4 tool messages (indices 2, 3, 4, 5) must be intact
        for m in tool_msgs[2:]:
            assert "[PRUNED:" not in m["content"]
            assert len(m["content"]) > 1000


class TestAdvancedFileTools:
    """Test line-range slicing in read_file, disambiguation in edit_file, and get_file_diff."""

    @pytest.fixture
    def test_workspace(self, tmp_path):
        old_env = os.environ.get("AURA_WORKSPACE")
        os.environ["AURA_WORKSPACE"] = str(tmp_path)
        yield tmp_path
        if old_env is not None:
            os.environ["AURA_WORKSPACE"] = old_env
        else:
            os.environ.pop("AURA_WORKSPACE", None)

    def test_read_file_line_range(self, test_workspace):
        target = test_workspace / "numbered.txt"
        target.write_text("\n".join(f"Line {i}" for i in range(1, 11)), encoding="utf-8")

        res = read_file("numbered.txt", start_line=3, end_line=5)
        assert "[Lines 3 to 5 of 10 in numbered.txt]:" in res
        assert "   3 | Line 3" in res
        assert "   4 | Line 4" in res
        assert "   5 | Line 5" in res
        assert "Line 1" not in res
        assert "Line 6" not in res

    def test_edit_file_multiple_matches_requires_replace_all(self, test_workspace):
        target = test_workspace / "multi.py"
        target.write_text("val = 1\nval = 1\nval = 2\n", encoding="utf-8")

        # Calling without replace_all=True should fail with disambiguation error
        res = edit_file("multi.py", "val = 1", "val = 10")
        assert res.startswith("ERROR: Target string matches 2 occurrences")
        assert "replace_all=true" in res
        assert target.read_text(encoding="utf-8") == "val = 1\nval = 1\nval = 2\n"

        # Calling with replace_all=True succeeds and replaces both
        res_ok = edit_file("multi.py", "val = 1", "val = 10", replace_all=True)
        assert res_ok.startswith("SUCCESS: Replaced 2 occurrence(s)")
        assert target.read_text(encoding="utf-8") == "val = 10\nval = 10\nval = 2\n"

    def test_get_file_diff_tracks_modifications(self, test_workspace):
        target = test_workspace / "diff_test.py"
        target.write_text("def run():\n    return False\n", encoding="utf-8")

        # Edit file
        edit_res = edit_file("diff_test.py", "return False", "return True")
        assert edit_res.startswith("SUCCESS:")

        # Get diff
        diff_out = get_file_diff("diff_test.py")
        assert "-    return False" in diff_out
        assert "+    return True" in diff_out

