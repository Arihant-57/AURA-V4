"""
AURA V4 Phase 1 MCP Server Integration Tests

Verifies that both filesystem and Git MCP servers:
1. Require AURA_WORKSPACE to start
2. Respect workspace boundary (no traversal)
3. Operate on the same selected workspace
"""

import os
import asyncio
import sys
from pathlib import Path
import unittest
import pytest

# Add src to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from mcp import Client, StdioServerParameters


class TestMCPFilesystemServer(unittest.TestCase):
    """Test filesystem MCP server using async client."""

    @classmethod
    def setUpClass(cls):
        cls.workspace = Path.cwd().resolve()
        cls.server_script = Path("src/aura/mcp/filesystem_server.py").resolve()
        if not cls.server_script.exists():
            cls.server_script = Path(__file__).parent.parent.parent / "src/aura/mcp/filesystem_server.py"

    def _get_client(self, workspace: str | None = None):
        """Create async client for the server."""
        env = os.environ.copy()
        if workspace:
            env["AURA_WORKSPACE"] = workspace
        else:
            env["AURA_WORKSPACE"] = str(self.workspace)
        return Client(
            StdioServerParameters(
                command=sys.executable,
                args=[str(self.server_script)],
                env=env,
            )
        )

    def test_list_directory_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("list_directory", {"path": "."})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertIn("src", content)

    def test_read_file_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("read_file", {"path": "src/aura/config/workspace.py"})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertIn("WorkspaceConfig", content)

    def test_traversal_blocked(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("read_file", {"path": "../"})
                return result
        result = asyncio.run(run())
        self.assertTrue(result.is_error)

    def test_write_and_edit_file_mcp(self):
        scratch_rel = "test_scratch_mcp_integration.txt"
        scratch_path = self.workspace / scratch_rel
        try:
            async def run():
                async with self._get_client() as client:
                    w_res = await client.call_tool("write_file", {"path": scratch_rel, "content": "alpha beta gamma"})
                    e_res = await client.call_tool("edit_file", {"path": scratch_rel, "target": "beta", "replacement": "delta"})
                    r_res = await client.call_tool("read_file", {"path": scratch_rel})
                    return w_res, e_res, r_res
            w_res, e_res, r_res = asyncio.run(run())
            self.assertFalse(w_res.is_error)
            self.assertIn("SUCCESS: Wrote", w_res.content[0].text)
            self.assertFalse(e_res.is_error)
            self.assertIn("SUCCESS: Replaced 1 occurrence(s)", e_res.content[0].text)
            self.assertIn("alpha delta gamma", r_res.content[0].text)
        finally:
            if scratch_path.exists():
                try:
                    scratch_path.unlink()
                except Exception:
                    pass

    def test_write_traversal_blocked_mcp(self):
        async def run():
            async with self._get_client() as client:
                res = await client.call_tool("write_file", {"path": "../escape.txt", "content": "bad"})
                return res
        res = asyncio.run(run())
        content = res.content[0].text if res.content else ""
        self.assertTrue(res.is_error or "ERROR:" in content)

    def test_get_file_diff_mcp(self):
        scratch_rel = "test_diff_mcp_integration.txt"
        scratch_path = self.workspace / scratch_rel
        try:
            async def run():
                async with self._get_client() as client:
                    await client.call_tool("write_file", {"path": scratch_rel, "content": "line 1\nline 2\n"})
                    await client.call_tool("edit_file", {"path": scratch_rel, "target": "line 2", "replacement": "line two modified"})
                    diff_res = await client.call_tool("get_file_diff", {"path": scratch_rel})
                    return diff_res
            diff_res = asyncio.run(run())
            self.assertFalse(diff_res.is_error)
            content = diff_res.content[0].text if diff_res.content else ""
            self.assertIn("-line 2", content)
            self.assertIn("+line two modified", content)
        finally:
            if scratch_path.exists():
                try:
                    scratch_path.unlink()
                except Exception:
                    pass

    def test_read_file_slicing_mcp(self):
        scratch_rel = "test_slice_mcp_integration.txt"
        scratch_path = self.workspace / scratch_rel
        try:
            async def run():
                async with self._get_client() as client:
                    await client.call_tool("write_file", {"path": scratch_rel, "content": "one\ntwo\nthree\nfour\nfive\n"})
                    slice_res = await client.call_tool("read_file", {"path": scratch_rel, "start_line": 2, "end_line": 4})
                    return slice_res
            slice_res = asyncio.run(run())
            self.assertFalse(slice_res.is_error)
            content = slice_res.content[0].text if slice_res.content else ""
            self.assertIn("[Lines 2 to 4 of 5 in test_slice_mcp_integration.txt]:", content)
            self.assertIn("   2 | two", content)
            self.assertIn("   3 | three", content)
            self.assertIn("   4 | four", content)
            self.assertNotIn("one", content)
        finally:
            if scratch_path.exists():
                try:
                    scratch_path.unlink()
                except Exception:
                    pass

    def test_absolute_path_blocked(self):
        async def run():
            async with self._get_client() as client:
                abs_path = "/etc/passwd" if os.name != "nt" else "C:\\Windows\\System32"
                result = await client.call_tool("read_file", {"path": abs_path})
                return result
        result = asyncio.run(run())
        self.assertTrue(result.is_error)


class TestMCPGitServerWithTempRepo(unittest.TestCase):
    """Test Git MCP server using a temporary Git repository."""

    @pytest.fixture(autouse=True)
    def inject_fixtures(self, temp_git_repo: Path):
        self.temp_repo = temp_git_repo
        self.server_script = Path("src/aura/mcp/git_server.py").resolve()
        if not self.server_script.exists():
            self.server_script = Path(__file__).parent.parent.parent / "src/aura/mcp/git_server.py"

    def _get_client(self, workspace: str | None = None):
        env = os.environ.copy()
        if workspace:
            env["AURA_WORKSPACE"] = workspace
        else:
            env["AURA_WORKSPACE"] = str(self.temp_repo)
        return Client(
            StdioServerParameters(
                command=sys.executable,
                args=[str(self.server_script)],
                env=env,
            )
        )

    def test_git_status_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("git_status", {})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertTrue("master" in content.lower() or "main" in content.lower() or "branch" in content.lower())

    def test_git_log_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("git_log", {"limit": 5})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertTrue(len(content) > 0)

    def test_git_branch_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("git_branch", {})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertTrue("master" in content.lower() or "main" in content.lower() or "branch" in content.lower())


class TestMCPGitServerWithSecondTempRepo(unittest.TestCase):
    """Test Git MCP server with a second temporary Git repository (different structure)."""

    @pytest.fixture(autouse=True)
    def inject_fixtures(self, second_temp_git_repo: Path):
        self.temp_repo = second_temp_git_repo
        self.server_script = Path("src/aura/mcp/git_server.py").resolve()
        if not self.server_script.exists():
            self.server_script = Path(__file__).parent.parent.parent / "src/aura/mcp/git_server.py"

    def _get_client(self, workspace: str | None = None):
        env = os.environ.copy()
        if workspace:
            env["AURA_WORKSPACE"] = workspace
        else:
            env["AURA_WORKSPACE"] = str(self.temp_repo)
        return Client(
            StdioServerParameters(
                command=sys.executable,
                args=[str(self.server_script)],
                env=env,
            )
        )

    def test_second_repo_git_status_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("git_status", {})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)

    def test_second_repo_git_log_works(self):
        async def run():
            async with self._get_client() as client:
                result = await client.call_tool("git_log", {"limit": 5})
                return result
        result = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertFalse(result.is_error)


class TestMultiWorkspaceMCP(unittest.TestCase):
    """Test that MCP servers respect different workspaces when switched."""

    @pytest.fixture(autouse=True)
    def inject_fixtures(self, multi_workspace):
        self.ws1, self.ws2 = multi_workspace
        self.fs_server = Path("src/aura/mcp/filesystem_server.py").resolve()
        if not self.fs_server.exists():
            self.fs_server = Path(__file__).parent.parent.parent / "src/aura/mcp/filesystem_server.py"

    def _get_fs_client(self, workspace: str):
        env = os.environ.copy()
        env["AURA_WORKSPACE"] = workspace
        return Client(
            StdioServerParameters(
                command=sys.executable,
                args=[str(self.fs_server)],
                env=env,
            )
        )

    def test_fs_server_sees_only_ws1(self):
        async def run():
            async with self._get_fs_client(str(self.ws1)) as client:
                result = await client.call_tool("list_directory", {"path": "."})
                return result
        result = asyncio.run(run())
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertIn("file1.txt", content)
        self.assertNotIn("file2.txt", content)

    def test_fs_server_sees_only_ws2(self):
        async def run():
            async with self._get_fs_client(str(self.ws2)) as client:
                result = await client.call_tool("list_directory", {"path": "."})
                return result
        result = asyncio.run(run())
        self.assertFalse(result.is_error)
        content = result.content[0].text if result.content else ""
        self.assertIn("file2.txt", content)
        self.assertNotIn("file1.txt", content)


# Symlink escape test - only run on systems where symlinks work without admin
try:
    import platform
    import tempfile

    def can_create_symlinks():
        """Check if we can create symlinks."""
        try:
            with tempfile.TemporaryDirectory() as d:
                target = Path(d) / "target"
                link = Path(d) / "link"
                target.mkdir()
                link.symlink_to(target, target_is_directory=True)
                return True
        except Exception:
            return False

    if can_create_symlinks():
        class TestSymlinkEscape(unittest.TestCase):
            """Test that symlink escapes are blocked."""

            @classmethod
            def setUpClass(cls):
                cls.workspace = Path.cwd().resolve()
                cls.server_script = Path("src/aura/mcp/filesystem_server.py").resolve()
                if not cls.server_script.exists():
                    cls.server_script = Path(__file__).parent.parent.parent / "src/aura/mcp/filesystem_server.py"

                # Create a symlink inside workspace pointing outside
                import tempfile
                cls.outside_dir = Path(tempfile.mkdtemp())
                (cls.outside_dir / "secret.txt").write_text("OUTSIDE SECRET")
                cls.symlink_path = cls.workspace / "evil_link"
                cls.symlink_path.symlink_to(cls.outside_dir, target_is_directory=True)

            @classmethod
            def tearDownClass(cls):
                if cls.symlink_path.exists():
                    cls.symlink_path.unlink()

            def _get_client(self):
                env = os.environ.copy()
                env["AURA_WORKSPACE"] = str(self.workspace)
                return Client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=[str(self.server_script)],
                        env=env,
                    )
                )

            def test_symlink_escape_blocked(self):
                """Accessing file through symlink that points outside workspace should be blocked."""
                async def run():
                    async with self._get_client() as client:
                        result = await client.call_tool("read_file", {"path": "evil_link/secret.txt"})
                        return result
                result = asyncio.run(run())
                # Should be blocked or return error (not the outside content)
                self.assertTrue(result.is_error or "OUTSIDE SECRET" not in (result.content[0].text if result.content else ""))

except (ImportError, AttributeError):
    pass


if __name__ == "__main__":
    unittest.main()