"""
Unit tests for AURA V4 Security and Path Policy:
- Sensitive file blocking (.env, *.key, *.pem, id_rsa, credentials.json)
- Write protection on noise/vendored dirs (.git, node_modules) and lock files
- Safe path traversal defense
"""

import pytest
from aura.core.tools import check_path_policy
from aura.core.scope import TaskScope


class TestSecuritySensitiveFileBlocking:
    """Verify that credentials, keys, and environment files are strictly blocked."""

    def test_env_files_blocked(self):
        scope = TaskScope("analyze project")
        for env_file in [".env", ".env.local", ".env.production", "backend/.env"]:
            res = check_path_policy("read_file", {"path": env_file}, scope)
            assert res is not None, f"Expected {env_file} to be blocked"
            assert "sensitive credentials or secrets" in res

    def test_private_keys_and_pem_blocked(self):
        scope = TaskScope("show key")
        for key_file in ["id_rsa", "server.key", "cert.pem", "jwt.key", "config/id_ed25519"]:
            res = check_path_policy("read_file", {"path": key_file}, scope)
            assert res is not None, f"Expected {key_file} to be blocked"
            assert "sensitive credentials or secrets" in res

    def test_credentials_json_blocked(self):
        scope = TaskScope("read config")
        res = check_path_policy("read_file", {"path": "credentials.json"}, scope)
        assert res is not None
        assert "sensitive credentials or secrets" in res

    def test_ssh_directory_blocked(self):
        scope = TaskScope("inspect ssh")
        res = check_path_policy("read_file", {"path": ".ssh/config"}, scope)
        assert res is not None
        assert "sensitive credentials or secrets" in res


class TestWriteAndEditSafetyPolicy:
    """Verify write and edit tools are blocked on protected/vendored files."""

    def test_writing_to_git_blocked(self):
        scope = TaskScope("fix git")
        res = check_path_policy("write_file", {"path": ".git/hooks/pre-commit"}, scope)
        assert res is not None
        assert "protected/vendored directory" in res

    def test_writing_to_node_modules_blocked(self):
        scope = TaskScope("update package")
        res = check_path_policy("write_file", {"path": "node_modules/lib/index.js"}, scope)
        assert res is not None
        assert "protected/vendored directory" in res

    def test_writing_to_lock_file_blocked(self):
        scope = TaskScope("update deps")
        res = check_path_policy("write_file", {"path": "package-lock.json"}, scope)
        assert res is not None
        assert "modifying lock file" in res

    def test_editing_sensitive_file_blocked(self):
        scope = TaskScope("update env")
        res = check_path_policy("edit_file", {"path": ".env"}, scope)
        assert res is not None
        assert "sensitive credentials or secrets" in res

    def test_writing_normal_source_allowed(self):
        scope = TaskScope("add feature")
        res = check_path_policy("write_file", {"path": "src/app.py"}, scope)
        assert res is None


class TestAdvancedSecurityHardening:
    """Verify defensive protections against option injection and diff secret exposure."""

    def test_run_tests_blocks_option_injection(self):
        from aura.mcp.filesystem_server import run_tests
        res1 = run_tests("-o")
        assert "ERROR: Command-line flags are not allowed" in res1
        res2 = run_tests("--capture=no")
        assert "ERROR: Command-line flags are not allowed" in res2

    def test_get_file_diff_blocked_on_sensitive_path(self):
        scope = TaskScope("view diff")
        res = check_path_policy("get_file_diff", {"path": ".env"}, scope)
        assert res is not None
        assert "sensitive credentials or secrets" in res

    def test_sensitive_diff_redacted(self):
        from aura.core.tools import sanitize_tool_result
        raw_diff = (
            "diff --git a/.env b/.env\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            "@@ -1,1 +1,1 @@\n"
            "-SECRET_KEY=supersecret123\n"
            "+SECRET_KEY=supersecret456\n"
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "+print('hello')\n"
        )
        sanitized = sanitize_tool_result("git_diff", raw_diff, "diff")
        assert "supersecret123" not in sanitized
        assert "supersecret456" not in sanitized
        assert "[SENSITIVE DIFF REDACTED:" in sanitized
        assert "print('hello')" in sanitized

