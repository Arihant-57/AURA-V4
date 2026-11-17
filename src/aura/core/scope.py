"""
TaskScope: Deterministic intent routing and tool gating boundaries.

Evaluates user queries to determine routing flags (git, backend,
frontend, conversational, diagnosis) and selects active tool sets.
"""

from __future__ import annotations

import re
from typing import Any


SPECIALIST_AGENTS: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto (Orchestrator)",
        "model_role": "auto",
        "description": "Sequential Planner -> Coder -> Tester -> Debugger pipeline",
        "tools": None,
    },
    "researcher": {
        "label": "Researcher",
        "model_role": "analysis",
        "description": "Web search, deep research, URL fetch, memory & codebase reading",
        "tools": frozenset({
            "jarvis_web_search", "jarvis_research", "jarvis_fetch_url",
            "jarvis_memory", "jarvis_briefing",
            "desktop_find_files", "desktop_inspect_path", "desktop_inspect_cursor",
            "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
        }),
    },
    "planner": {
        "label": "Planner / Architect",
        "model_role": "analysis",
        "description": "Read-only codebase architecture analysis & step-by-step blueprint",
        "tools": frozenset({
            "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
            "git_status", "git_branch", "git_log", "git_diff", "git_ls_files",
        }),
    },
    "coder": {
        "label": "Coder (Ponytail)",
        "model_role": "coding",
        "description": "Minimal-diff code implementation & file editing",
        "tools": frozenset({
            "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
            "edit_file", "write_file", "get_file_diff",
        }),
    },
    "tester": {
        "label": "Tester / Verifier",
        "model_role": "verification",
        "description": "Pytest execution, test authoring & diff verification",
        "tools": frozenset({
            "run_tests", "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
            "edit_file", "write_file", "get_file_diff", "git_diff", "git_status",
        }),
    },
    "debugger": {
        "label": "Debugger",
        "model_role": "coding",
        "description": "Root-cause stack trace analysis & minimal bug fix",
        "tools": frozenset({
            "run_tests", "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
            "get_file_diff", "git_diff", "edit_file",
        }),
    },
    "reviewer": {
        "label": "Reviewer / Security",
        "model_role": "verification",
        "description": "Read-only code quality, anti-bloat & OWASP security audit",
        "tools": frozenset({
            "git_diff", "git_status", "git_log", "get_file_diff",
            "read_file", "list_directory", "search_files", "file_outline", "scan_repository",
        }),
    },
    "jarvis": {
        "label": "Jarvis Desktop",
        "model_role": "lightweight",
        "description": "Windows desktop control, window snapping, media, browser & notes",
        "tools": None,
    },
}

_AGENT_ALIASES: dict[str, str] = {
    "orchestrator": "auto",
    "architect": "planner",
    "plan": "planner",
    "research": "researcher",
    "code": "coder",
    "test": "tester",
    "verifier": "tester",
    "verify": "tester",
    "debug": "debugger",
    "review": "reviewer",
    "security": "reviewer",
    "desktop": "jarvis",
}

_AGENT_PREFIX_RE = re.compile(r"^\s*@([a-zA-Z0-9_-]+)\b[\s,:;-]*(.*)$", re.DOTALL)


def normalize_agent_role(agent_role: str | None) -> str:
    """Normalize explicit or aliased specialist agent identifiers."""
    clean = (agent_role or "").strip().lower()
    if not clean:
        return "auto"
    clean = _AGENT_ALIASES.get(clean, clean)
    return clean if clean in SPECIALIST_AGENTS else "auto"


def parse_agent_prefix(prompt: str) -> tuple[str | None, str]:
    """Extract an optional leading `@agent` selector from *prompt*."""
    text = prompt or ""
    m = _AGENT_PREFIX_RE.match(text)
    if not m:
        return None, text
    raw_name = m.group(1).strip().lower()
    mapped = _AGENT_ALIASES.get(raw_name, raw_name)
    if mapped not in SPECIALIST_AGENTS:
        return None, text
    cleaned = m.group(2).strip()
    return mapped, (cleaned or text.strip())


def _git_signal_spans(sentence: str) -> list[tuple[int, int]]:
    spans = []
    matches = list(re.finditer(r"\bgit\b", sentence, re.IGNORECASE))
    for m in matches:
        start, end = m.span()
        before = sentence[:start]
        after = sentence[end:]

        if re.search(r"\b(show|run|check|view|inspect|get|status|log|diff|branch|ls-files|history|commit|staged|untracked)\b\s*$", before, re.IGNORECASE):
            spans.append((start, end))
            continue
        if re.search(r"^\s*\b(status|log|diff|branch|history|commit|commits|staged|untracked)\b", after, re.IGNORECASE):
            spans.append((start, end))
            continue
        if re.search(r"\b(status|log|diff|branch|ls-files|history|commit|commits|staged|untracked)\b", sentence, re.IGNORECASE):
            spans.append((start, end))
            continue

    return spans


def _sentence_requests_git(sentence: str) -> bool:
    if re.search(r"\b(dont|don't|do not|never|skip|avoid)\b[^.!?]*\bgit\b", sentence, re.IGNORECASE):
        return False
    return len(_git_signal_spans(sentence)) > 0


def detect_git_intent(user_prompt: str) -> bool:
    sentences = re.split(r"[.!?]+", user_prompt)
    for sentence in sentences:
        if _sentence_requests_git(sentence):
            return True
    return False


def _tokens(user_prompt: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", user_prompt.lower()))


def detect_backend_intent(user_prompt: str) -> bool:
    tokens = _tokens(user_prompt)
    backend_keywords = {"backend", "server", "express", "api", "routes", "node", "python", "fastapi", "django"}
    return len(tokens.intersection(backend_keywords)) > 0


def _negated_intent_token(user_prompt: str, token: str) -> bool:
    pattern = rf"\b(dont|don't|do not|never|without|no)\b[^.!?]*\b{token}\b"
    return bool(re.search(pattern, user_prompt, re.IGNORECASE))


def detect_frontend_intent(user_prompt: str) -> bool:
    if _negated_intent_token(user_prompt, "frontend") or _negated_intent_token(user_prompt, "ui"):
        return False
    tokens = _tokens(user_prompt)
    frontend_keywords = {"frontend", "ui", "react", "next", "components", "pages", "css", "html", "view"}
    return len(tokens.intersection(frontend_keywords)) > 0


def detect_diagnosis_intent(user_prompt: str) -> bool:
    prompt_lower = user_prompt.lower()
    diagnosis_keywords = {
        "bug", "defect", "root cause", "diagnose", "investigate",
        "why does", "why is", "failing", "crash", "error", "exception",
        "issue", "incorrect", "broken", "malfunction", "unexpected"
    }
    return any(kw in prompt_lower for kw in diagnosis_keywords)


def detect_desktop_intent(user_prompt: str) -> bool:
    tokens = _tokens(user_prompt)
    desktop_keywords = {
        "screen", "ocr", "window", "windows", "monitor", "monitors", "display",
        "snap", "launch", "desktop", "screenshot", "chrome", "edge", "firefox", "browser",
        "notepad", "spotify", "discord", "calculator", "calc", "explorer", "minimize", "maximize",
        "clipboard", "trash", "recycle", "click", "button",
        "mouse", "cursor", "pointing", "pointer", "hovering", "hovered",
        "keyboard", "hotkey", "shortcut",
        "media", "music", "song", "volume", "mute", "pause",
        "play", "video", "videos", "watch", "trailer", "shorts", "stream",
        "tab", "tabs", "todo", "todos", "settings", "bluetooth", "wifi", "focus",
    }
    if tokens.intersection(desktop_keywords):
        return True
    if tokens.intersection({"downloads", "documents", "onedrive"}) and tokens.intersection(
        {"open", "find", "locate", "show", "reveal", "read", "inspect", "check", "inside", "summarize", "explain", "research", "list", "search", "whats", "what"}
    ):
        return True
    if re.search(r"[A-Za-z]:[\\/]", user_prompt) and tokens.intersection(
        {"open", "show", "reveal", "launch", "find", "locate", "read", "inspect", "check", "summarize", "explain"}
    ):
        return True
    _repo_tokens = {"workspace", "repo", "repository", "codebase", "src", "backend", "frontend", "server", "api", "git"}
    if (
        tokens.intersection({"open", "show", "reveal", "read", "inspect", "inside"})
        and tokens.intersection({"folder", "folders"})
        and not tokens.intersection(_repo_tokens)
    ):
        return True
    _code_tokens = {"codebase", "repo", "repository", "file", "files", "server", "api", "git", "pr", "diff"}
    if (
        tokens.intersection({"open", "launch", "start"})
        and not tokens.intersection(_code_tokens)
        and not re.search(r"\.(?:py|js|jsx|ts|tsx|json|md|txt|yaml|yml|toml|html|css|sql)\b", user_prompt, re.IGNORECASE)
    ):
        return True
    low = user_prompt.lower()
    if any(w in tokens for w in ("gmail", "inbox", "calendar", "browser", "tab")) and any(
        v in tokens for v in ("check", "summarize", "summary", "read", "see", "show", "whats", "what")
    ):
        return True
    if any(w in tokens for w in ("close", "quit")) and any(
        w in tokens for w in ("app", "window", "windows", "chrome", "edge", "notepad", "spotify", "discord", "code", "vscode")
    ):
        return True
    if (
        any(p in low for p in ("what is inside", "what's inside", "whats inside", "look at this"))
        and not tokens.intersection(_repo_tokens | {"bug", "error", "traceback", "stack", "test", "tests", "function", "class", "module"})
        and not re.search(r"\.(?:py|js|jsx|ts|tsx|json|md|txt|yaml|yml|toml|html|css|sql)\b", low)
    ):
        return True
    return any(
        phrase in low
        for phrase in (
            "what is on my",
            "read my screen",
            "pointing at",
            "hovering over",
            "under my cursor",
            "under the cursor",
            "at my cursor",
            "on my cursor",
            "inspect cursor",
            "look at this on",
            "what is this on my screen",
            "open app",
            "find file on my pc",
            "search my pc",
            "type text",
            "scroll up",
            "scroll down",
            "double click",
            "right click",
            "to-do",
            "todo list",
            "add note",
            "take a note",
            "ms-settings:",
            "fill field",
            "fill input",
            "work mode",
            "focus mode",
            "system info",
            "system status",
            "pc health",
            "system health",
            "cpu usage",
            "ram usage",
        )
    )


def detect_jarvis_intent(user_prompt: str) -> bool:
    tokens = _tokens(user_prompt)
    jarvis_keywords = {
        "web", "internet", "online", "search", "latest", "news", "weather",
        "remember", "memory", "forget", "profile", "speak", "voice", "say",
        "jarvis", "ultron", "megatron", "browse", "browser", "url", "website", "site", "sites", "webpage", "fetch",
        "gmail", "email", "mail", "inbox", "calendar", "schedule", "agenda",
        "meeting", "meetings", "event", "events", "research", "youtube",
        "github", "drive", "google", "summarize", "summary", "morning", "brief", "briefing",
        "play", "video", "videos", "watch", "trailer", "shorts", "stream",
        "chrome", "edge", "firefox", "tab", "tabs",
    }
    if tokens.intersection(jarvis_keywords):
        return True
    low = user_prompt.lower()
    return ("http://" in low or "https://" in low or "look up" in low or "search online" in low)


def detect_browser_intent(user_prompt: str) -> bool:
    tokens = _tokens(user_prompt)
    browser_keywords = {
        "browser", "webpage", "website", "navigate", "click", "fill", "form",
        "login", "gmail", "email", "inbox", "calendar", "youtube", "github",
        "google", "drive", "tab", "tabs", "scroll", "page", "link", "automate",
        "chrome", "edge", "firefox", "play", "video", "videos", "watch", "trailer", "shorts", "stream",
    }
    if tokens.intersection(browser_keywords):
        return True
    if "search" in tokens and not tokens.intersection({"file", "files", "repo", "repository", "codebase"}):
        return True
    if "open" in tokens and tokens.intersection({"url", "site", "website", "web", "online", "page", "link"}):
        return True
    low = user_prompt.lower()
    return "http://" in low or "https://" in low


def is_conversational(user_prompt: str) -> bool:
    prompt_lower = user_prompt.strip().lower()
    clean = prompt_lower.rstrip(".!?").strip()
    greetings = {
        "hi", "hello", "hey", "who are you", "what can you do", "help", "thanks", "thank you",
        "hey jarvis", "hi jarvis", "hello jarvis", "yo jarvis", "good morning", "good evening",
        "good morning jarvis", "good evening jarvis", "how are you", "what are you doing",
        "hey jarvis what are you doing", "hi jarvis what are you doing",
        "hey megatron", "hi megatron", "hello megatron",
        "hey ultron", "hi ultron", "hello ultron",
    }
    if prompt_lower in greetings or clean in greetings:
        return True
    if re.match(
        r"^(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening))\s+(?:jarvis|ultron|megatron|aura)(?:[,!\s]+(?:how are you|what are you doing|whats up|what's up|who are you))?$",
        clean,
    ):
        return True
    if re.match(
        r"^(?:which|what)\s+m[io]d[ea]l(?:\s+\w{1,4}){0,2}\s+(?:are\s+you|are|is\s+(?:running|this|active)|you\s+are)(?:\s+using|\s+running)?$",
        clean,
    ):
        return True
    return False


class TaskScope:
    """Immutable per-task routing decision shared by policy functions."""

    def __init__(self, task: str, agent_role: str | None = None) -> None:
        prefixed_role, cleaned_task = parse_agent_prefix(task)
        effective_role = normalize_agent_role(agent_role) if agent_role else (prefixed_role or "auto")
        eval_task = cleaned_task if prefixed_role else task

        self.agent_role: str = effective_role
        self.prompt_lower = eval_task.lower()
        self.git = detect_git_intent(eval_task) or effective_role in ("planner", "tester", "debugger", "reviewer")
        self.backend = detect_backend_intent(eval_task)
        self.frontend = detect_frontend_intent(eval_task)
        self.desktop = detect_desktop_intent(eval_task) or effective_role == "jarvis"
        self.jarvis = detect_jarvis_intent(eval_task) or effective_role in ("researcher", "jarvis")
        self.browser = detect_browser_intent(eval_task) or effective_role == "jarvis"
        self.conversational = False if effective_role != "auto" else is_conversational(eval_task)
        self.diagnosis = (detect_diagnosis_intent(eval_task) or effective_role == "debugger") and not self.conversational

    def expand_for_steer(self, steer_text: str) -> bool:
        """Unlock additional non-destructive tool scopes requested by a mid-run steer.

        Returns True if any scope capability was newly enabled.
        """
        text = (steer_text or "").strip()
        if not text:
            return False
        before = (
            self.git, self.backend, self.frontend,
            self.desktop, self.jarvis, self.browser,
            self.conversational, self.diagnosis,
        )
        self.prompt_lower = f"{self.prompt_lower}\n{text.lower()}".strip()
        if detect_git_intent(text):
            self.git = True
        if detect_desktop_intent(text):
            self.desktop = True
        if detect_jarvis_intent(text):
            self.jarvis = True
        if detect_browser_intent(text):
            self.browser = True
        if detect_diagnosis_intent(text):
            self.diagnosis = True
        self.conversational = False
        after = (
            self.git, self.backend, self.frontend,
            self.desktop, self.jarvis, self.browser,
            self.conversational, self.diagnosis,
        )
        return after != before

    def describe(self) -> str:
        flags = []
        if self.agent_role != "auto":
            flags.append(f"agent:{self.agent_role}")
        if self.conversational:
            flags.append("conversational")
        if self.git:
            flags.append("git")
        if self.backend:
            flags.append("backend")
        if self.frontend:
            flags.append("frontend")
        if self.desktop:
            flags.append("desktop")
        if self.jarvis:
            flags.append("jarvis")
        if self.browser:
            flags.append("browser")
        if self.diagnosis:
            flags.append("diagnosis")
        return "+".join(flags) if flags else "general-filesystem"


def select_active_tools(all_tools: list[Any], scope: TaskScope) -> list[Any]:
    """
    Tool gating:
    - conversational tasks: no tools at all
    - specialist agent role (planner/researcher/coder/tester/debugger/reviewer): strict role whitelist
    - git intent: git_* tools unlocked
    - desktop intent: desktop_* tools unlocked
    - jarvis intent: jarvis_* tools unlocked
    - browser intent: browser_* tools unlocked
    - otherwise: filesystem tools only
    """
    if scope.conversational:
        return []

    agent_role = getattr(scope, "agent_role", "auto")
    spec = SPECIALIST_AGENTS.get(agent_role)
    allowed_names = spec.get("tools") if spec else None
    if allowed_names is not None:
        return [t for t in all_tools if getattr(t, "name", None) in allowed_names]

    active = []
    for tool in all_tools:
        if tool.name.startswith("git_"):
            if scope.git:
                active.append(tool)
        elif tool.name.startswith("desktop_"):
            if scope.desktop:
                active.append(tool)
        elif tool.name.startswith("jarvis_"):
            if scope.jarvis:
                active.append(tool)
        elif tool.name.startswith("browser_"):
            if scope.browser:
                active.append(tool)
        else:
            active.append(tool)
    return active