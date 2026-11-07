"""
Unit tests for AURA V4 Filesystem MCP Server error handling:
- Clean error string return (no unhandled ValueError) on path traversal for read_file, list_directory, and search_files.
- Helpful Node.js detection notice when run_tests finds no pytest tests in a workspace with package.json.
"""

import os
import pytest
from pathlib import Path

from aura.mcp.filesystem_server import (
    read_file,
    list_directory,
    search_files,
    write_file,
    run_tests,
)


@pytest.fixture
def test_workspace(tmp_path: Path):
    """Set up an isolated workspace with sample files."""
    old_env = os.environ.get("AURA_WORKSPACE")
    os.environ["AURA_WORKSPACE"] = str(tmp_path)
    (tmp_path / "hello.txt").write_text("Hello world\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text("print('sample')\n", encoding="utf-8")
    yield tmp_path
    if old_env is not None:
        os.environ["AURA_WORKSPACE"] = old_env
    else:
        os.environ.pop("AURA_WORKSPACE", None)


class TestFilesystemPathTraversalErrorHandling:
    """Verify that path traversal attempts return clean ERROR strings rather than crashing."""

    def test_read_file_traversal_returns_error(self, test_workspace):
        result = read_file("../outside.txt")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_read_file_deep_traversal_returns_error(self, test_workspace):
        result = read_file("../../etc/passwd")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_read_file_absolute_path_returns_error(self, test_workspace):
        abs_target = Path("C:/Windows/win.ini") if os.name == "nt" else Path("/etc/passwd")
        result = read_file(str(abs_target))
        assert result.startswith("ERROR:")

    def test_list_directory_traversal_returns_error(self, test_workspace):
        result = list_directory("..")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_list_directory_deep_traversal_returns_error(self, test_workspace):
        result = list_directory("../../")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_list_directory_empty_path_returns_error(self, test_workspace):
        result = list_directory("")
        assert result.startswith("ERROR:")
        assert "Invalid path" in result

    def test_search_files_traversal_returns_error(self, test_workspace):
        result = search_files("test", "..")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_search_files_deep_traversal_returns_error(self, test_workspace):
        result = search_files("test", "../../")
        assert result.startswith("ERROR:")
        assert "Access outside the project workspace is not allowed" in result

    def test_search_files_empty_query_returns_error(self, test_workspace):
        result = search_files("", ".")
        assert result.startswith("ERROR: Search query cannot be empty.")


class TestRunTestsNodeJsFlexibility:
    """Verify run_tests behavior when testing non-Python / Node.js workspaces."""

    def test_run_tests_with_package_json_notices_nodejs(self, test_workspace):
        # Create a package.json in workspace to simulate Node.js project
        (test_workspace / "package.json").write_text('{"name": "test-pkg"}', encoding="utf-8")
        result = run_tests("")
        assert "TEST SUITE FAILED (exit code 5):" in result
        assert "(Note: No pytest tests found. This workspace appears to be a Node.js project containing package.json)" in result

    def test_run_tests_without_package_json_no_nodejs_notice(self, test_workspace):
        # Without package.json, should just report exit code 5 without Node.js notice
        result = run_tests("")
        assert "TEST SUITE FAILED (exit code 5):" in result
        assert "Node.js project" not in result
