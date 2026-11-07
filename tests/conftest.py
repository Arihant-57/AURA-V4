"""
Test fixtures for AURA V4 integration tests.

Provides temporary Git repositories and workspaces.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Generator
import pytest


@pytest.fixture(scope="session")
def temp_git_repo() -> Generator[Path, None, None]:
    """Create a temporary Git repository with some content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize Git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@aura.local"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "AURA Test"], cwd=repo_path, check=True, capture_output=True)

        # Create some source files
        src_dir = repo_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("""#!/usr/bin/env python3
def hello():
    return "Hello, World!"

def add(a, b):
    return a + b

if __name__ == "__main__":
    print(hello())
    print(add(2, 3))
""")

        # Create a manifest
        (repo_path / "pyproject.toml").write_text("""[project]
name = "test-project"
version = "0.1.0"
dependencies = []

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
""")

        # Create a test file
        tests_dir = repo_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text("""import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import hello, add

def test_hello():
    assert hello() == "Hello, World!"

def test_add():
    assert add(2, 3) == 5
""")

        # Commit everything
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)

        yield repo_path


@pytest.fixture(scope="session")
def second_temp_git_repo() -> Generator[Path, None, None]:
    """Create a second temporary Git repository with different content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@aura.local"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "AURA Test"], cwd=repo_path, check=True, capture_output=True)

        # Different project structure - Node.js style
        (repo_path / "package.json").write_text("""{
  "name": "test-node-project",
  "version": "1.0.0",
  "main": "index.js",
  "dependencies": {
    "express": "^4.18.0"
  }
}""")

        src_dir = repo_path / "src"
        src_dir.mkdir()
        (src_dir / "server.js").write_text("""const express = require('express');
const app = express();
const port = 3000;

app.get('/', (req, res) => {
  res.send('Hello from Node.js!');
});

app.get('/api/data', (req, res) => {
  res.json({ message: 'Data endpoint', timestamp: Date.now() });
});

app.listen(port, () => {
  console.log(`Server running on port ${port}`);
});
""")

        (src_dir / "utils.js").write_text("""function formatDate(date) {
  return date.toISOString().split('T')[0];
}

function calculateSum(a, b) {
  return a + b;
}

module.exports = { formatDate, calculateSum };
""")

        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial Node.js commit"], cwd=repo_path, check=True, capture_output=True)

        yield repo_path


@pytest.fixture
def isolated_workspace(tmp_path: Path) -> Path:
    """Create an isolated workspace directory for testing."""
    workspace = tmp_path / "test_workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Test Workspace\n")
    return workspace


@pytest.fixture
def multi_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create two isolated workspaces for multi-repo testing."""
    ws1 = tmp_path / "workspace1"
    ws2 = tmp_path / "workspace2"
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / "file1.txt").write_text("workspace 1 content")
    (ws2 / "file2.txt").write_text("workspace 2 content")
    return ws1, ws2