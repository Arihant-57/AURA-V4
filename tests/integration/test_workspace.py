"""
AURA V4 Phase 1 Integration Tests

Verifies workspace resolution precedence and MCP server workspace enforcement.
"""

import os
import sys
from pathlib import Path
import pytest

# Add src to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from aura.config.workspace import resolve_workspace, WorkspaceConfig


class TestWorkspacePrecedence:

    def test_precedence_cli_over_all(self, tmp_path):
        # Create two temp directories
        dir1 = tmp_path / "repo1"
        dir2 = tmp_path / "repo2"
        dir1.mkdir()
        dir2.mkdir()

        os.environ["AURA_WORKSPACE"] = str(dir1)
        # CLI should win even if ENV and CWD exist
        res = resolve_workspace(str(dir2), allow_env=True)
        assert res == dir2

    def test_precedence_env_over_cwd(self, tmp_path):
        dir1 = tmp_path / "repo1"
        dir1.mkdir()

        os.environ["AURA_WORKSPACE"] = str(dir1)
        res = resolve_workspace(None, allow_env=True)
        assert res == dir1

    def test_fallback_to_cwd(self, tmp_path):
        # No CLI, no ENV - use tmp_path as the CWD context
        # Must clean AURA_WORKSPACE env var
        old_cwd = Path.cwd()
        old_env = os.environ.get("AURA_WORKSPACE")
        if "AURA_WORKSPACE" in os.environ:
            del os.environ["AURA_WORKSPACE"]
        try:
            os.chdir(tmp_path)
            res = resolve_workspace(None, allow_env=True, allow_cwd=True)
            assert res == tmp_path
        finally:
            os.chdir(old_cwd)
            if old_env:
                os.environ["AURA_WORKSPACE"] = old_env

    def test_invalid_workspace_raises(self):
        with pytest.raises(ValueError):
            resolve_workspace("/non/existent/path/aura/test", allow_cwd=False)


class TestPathSafety:

    def test_safe_path_traversal_detection(self):
        # Set workspace for current process BEFORE import (server requires it at module load)
        os.environ["AURA_WORKSPACE"] = str(Path.cwd().resolve())
        from aura.mcp.filesystem_server import safe_path

        # Relative paths should work
        assert safe_path("src").exists()

        # Traversal should fail
        with pytest.raises(ValueError) as exc_info:
            safe_path("../")
        assert "Access outside" in str(exc_info.value)

        # Absolute paths should fail
        with pytest.raises(ValueError) as exc_info:
            safe_path("/etc/passwd" if os.name != "nt" else "C:\\Windows\\System32")
        assert "Absolute paths are not allowed" in str(exc_info.value)


class TestMultiWorkspaceResolution:
    """Test that workspace resolution works correctly with multiple different workspaces."""

    def test_two_different_workspaces_resolve_independently(self, tmp_path):
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()

        (ws1 / "file1.txt").write_text("content1")
        (ws2 / "file2.txt").write_text("content2")

        res1 = resolve_workspace(str(ws1))
        res2 = resolve_workspace(str(ws2))

        assert res1 == ws1
        assert res2 == ws2
        assert res1 != res2

    def test_workspace_isolation_no_cross_contamination(self, tmp_path):
        """Ensure workspace A doesn't leak into workspace B."""
        ws_a = tmp_path / "ws_a"
        ws_b = tmp_path / "ws_b"
        ws_a.mkdir()
        ws_b.mkdir()

        (ws_a / "secret_a.txt").write_text("secret from A")
        (ws_b / "secret_b.txt").write_text("secret from B")

        res_a = resolve_workspace(str(ws_a))
        res_b = resolve_workspace(str(ws_b))

        # Each workspace resolves to its own path
        assert res_a == ws_a
        assert res_b == ws_b

        # Files are isolated
        assert (res_a / "secret_a.txt").exists()
        assert not (res_a / "secret_b.txt").exists()
        assert (res_b / "secret_b.txt").exists()
        assert not (res_b / "secret_a.txt").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])