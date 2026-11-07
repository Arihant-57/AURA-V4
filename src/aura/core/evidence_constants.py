"""
Evidence & Prefetch Constants for AURA V4.

These are repository-agnostic configuration values controlling the
deterministic evidence prefetch fast path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

# ─── Deterministic Prefetch Configuration ────────────────────────────────
# All tunable via environment variables (see settings.py PrefetchSettings)

PREFETCH_MAX_READS = 8
PREFETCH_MANIFEST_CHARS = 8000
PREFETCH_SOURCE_RAW_CHARS = 20000
PREFETCH_SKELETON_CHARS = 6000
PREFETCH_PACKET_CHARS = 28000
EXECUTION_ORDER_VIEW_BUDGET = 5000
PREFETCH_HEAD_CHARS = 1200
PREFETCH_TAIL_CHARS = 600
INSUFFICIENT_MARKER = "INSUFFICIENT EVIDENCE"

# ─── Filesystem Noise Filters ────────────────────────────────────────────

NOISE_DIR_COMPONENTS = (
    ".git", "node_modules", ".next", "dist", "build", "out",
    "__pycache__", ".venv", "venv", ".idea", ".vscode",
)

LOCK_FILES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "composer.lock",
)

SENSITIVE_FILE_NAMES: FrozenSet[str] = frozenset({
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "id_ecdsa",
    ".netrc", ".npmrc", ".pypirc",
})

SENSITIVE_SUFFIXES: Tuple[str, ...] = (
    ".pem", ".key", ".pfx", ".p12", ".pkcs12",
)

SENSITIVE_DIR_NAMES: Tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg",
)

FRONTEND_DIRS = ("frontend", "client", "web", "ui", "public", "static", "assets")
BACKEND_DIRS = ("backend", "server", "api")

# ─── Manifest & Entrypoint Conventions (repository-independent) ──────────

MANIFEST_NAMES: FrozenSet[str] = frozenset({
    "package.json", "requirements.txt", "pyproject.toml", "pipfile",
    "setup.py", "setup.cfg", "go.mod", "cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "gemfile",
})

BACKEND_ENTRYPOINT_NAMES: FrozenSet[str] = frozenset({
    "server.js", "app.js", "index.js", "main.js",
    "server.ts", "app.ts", "index.ts", "main.ts",
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "run.py",
    "main.go", "main.rs", "program.cs",
})

FRONTEND_ENTRYPOINT_NAMES: FrozenSet[str] = frozenset({
    "index.html", "main.jsx", "main.tsx", "app.jsx", "app.tsx", "app.vue",
    "main.js", "index.js",
    "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
    "angular.json", "svelte.config.js",
})

BACKEND_SOURCE_SUFFIXES: Tuple[str, ...] = (".js", ".ts", ".py", ".go", ".rb", ".java", ".cs", ".php")
FRONTEND_SOURCE_SUFFIXES: Tuple[str, ...] = (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html")

# ─── Intent Detection Tokens ────────────────────────────────────────────

_ANALYSIS_VERB_TOKENS = {
    "analyze", "analyse", "explain", "describe", "identify",
    "summarize", "summarise", "review", "inspect", "overview",
}
_QUESTION_STARTERS = ("what", "how", "which", "why", "where")

_BACKEND_TOKENS = {
    "backend", "server", "api", "apis", "endpoint", "endpoints",
    "express", "fastapi", "flask", "django", "manifest",
    "dependency", "dependencies", "middleware",
}
_FRONTEND_TOKENS = {
    "frontend", "client", "ui", "react", "vue", "angular", "svelte",
    "css", "html", "component", "components", "page", "pages", "widget",
}
_FS_OBJECT_TOKENS = _BACKEND_TOKENS | _FRONTEND_TOKENS | {
    "file", "files", "folder", "folders", "directory", "directories",
    "code", "codebase", "project", "repo", "repository", "structure",
    "package", "packages", "bug", "bugs", "error", "errors", "crash",
    "function", "functions", "class", "classes", "module", "modules",
    "import", "imports", "readme", "config", "configs", "configuration",
    "test", "tests", "src", "source", "script", "scripts",
    "library", "libraries", "framework", "implementation",
    "route", "routes", "database", "schema", "handler", "handlers",
}
_CONVERSATIONAL_RE = None  # compiled lazily

_GIT_PHRASES = (
    "git status", "git log", "git diff", "git branch", "git show",
    "git blame", "git stash", "git fetch", "git pull", "git push",
    "git checkout", "git history", "git repo", "git repository",
    "git commit", "commit history", "commit log",
    "staged changes", "staged files", "unstaged changes",
    "untracked files", "current branch", "which branch",
    "active branch", "what branch", "latest commit", "last commit",
    "recent commits", "previous commit", "previous commits",
)
_GIT_BARE_RE = None  # compiled lazily
_GIT_NEGATION_RE = None
_GIT_CONTRAST_RE = None

_QUOTED_RE = None
_SENTENCE_SPLIT_RE = None
_WORD_RE = None

# ─── Prefilter patterns for structural extraction ────────────────────────

_SOURCE_FILTER_PATTERNS = (
    r"^\s*(import\s+\S|from\s+\S+\s+import)",  # python / ES imports
    r"\brequire\s*\(",  # CommonJS imports
    r"^\s*(export\s|module\.exports)",  # exports
    r"\.(get|post|put|patch|delete|use|route)\s*\(\s*['\"]",  # express-style routes
    r"\b[A-Za-z_$][\w$]*\s*=\s*async\s",  # async assignment
    r"^\s*func\s+[\(\w]",  # go funcs
    r"\.(listen|run)\s*\(",  # server bootstrap
    r"(Flask|FastAPI|express|Django)\s*\(",  # app construction
)
_SOURCE_FILTER_RES = _SOURCE_FILTER_PATTERNS

@dataclass(frozen=True)
class PrefetchPolicy:
    """Repository-independent evidence policy for one task scope."""
    name: str
    candidate_dirs: Tuple[str, ...]
    entrypoint_names: FrozenSet[str]
    source_suffixes: Tuple[str, ...]

BACKEND_PREFETCH_POLICY = PrefetchPolicy(
    "backend", BACKEND_DIRS, BACKEND_ENTRYPOINT_NAMES, BACKEND_SOURCE_SUFFIXES)

FRONTEND_PREFETCH_POLICY = PrefetchPolicy(
    "frontend", FRONTEND_DIRS, FRONTEND_ENTRYPOINT_NAMES, FRONTEND_SOURCE_SUFFIXES)

ROOT_MANIFEST_POLICY = PrefetchPolicy(
    "root-manifests", (".",), frozenset(), ())

_DEPENDENCY_QUESTION_TOKENS = {"dependency", "dependencies", "manifest", "package", "packages"}
_BACKEND_PHRASES = (
    "backend analysis", "analyze the backend", "analyse the backend",
    "analyze backend", "analyse backend",
)