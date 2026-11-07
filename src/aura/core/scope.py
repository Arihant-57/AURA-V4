"""
TaskScope: Deterministic intent routing and tool gating boundaries.

Evaluates user queries to determine routing flags (git, backend,
frontend, conversational, diagnosis) and selects active tool sets.
"""

from __future__ import annotations

import re
from typing import Any


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


def is_conversational(user_prompt: str) -> bool:
    prompt_lower = user_prompt.strip().lower()
    greetings = {"hi", "hello", "hey", "who are you", "what can you do", "help", "thanks", "thank you"}
    if prompt_lower in greetings or prompt_lower.rstrip(".!?") in greetings:
        return True
    return False


class TaskScope:
    """Immutable per-task routing decision shared by policy functions."""

    def __init__(self, task: str) -> None:
        self.prompt_lower = task.lower()
        self.git = detect_git_intent(task)
        self.backend = detect_backend_intent(task)
        self.frontend = detect_frontend_intent(task)
        self.conversational = is_conversational(task)
        self.diagnosis = detect_diagnosis_intent(task) and not self.conversational

    def describe(self) -> str:
        flags = []
        if self.conversational:
            flags.append("conversational")
        if self.git:
            flags.append("git")
        if self.backend:
            flags.append("backend")
        if self.frontend:
            flags.append("frontend")
        if self.diagnosis:
            flags.append("diagnosis")
        return "+".join(flags) if flags else "general-filesystem"


def select_active_tools(all_tools: list[Any], scope: TaskScope) -> list[Any]:
    """
    Tool gating:
    - conversational tasks: no tools at all
    - git intent: git_* tools + filesystem tools
    - otherwise: filesystem tools only (git_* hidden AND non-executable)
    """
    if scope.conversational:
        return []
    active = []
    for tool in all_tools:
        if tool.name.startswith("git_"):
            if scope.git:
                active.append(tool)
        else:
            active.append(tool)
    return active