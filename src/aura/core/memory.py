"""
Session Memory: Lightweight cross-task memory within an interactive session.

Tracks files and directories actually observed in successful tool calls
so the agent can reference known paths directly without re-listing directories.
"""

from __future__ import annotations

import time

SESSION_MEMORY_MAX_ENTRIES = 40


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/") or "."


class SessionMemory:
    """
    Lightweight cross-task memory: files/directories observed this session.
    Only successful tool results are recorded. Cleared with 'reset'.
    """

    def __init__(self, max_entries: int = SESSION_MEMORY_MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self.files: dict[str, float] = {}
        self.dirs: dict[str, float] = {}
        self.modified: set[str] = set()

    def _cap(self, store: dict[str, float]) -> None:
        while len(store) > self.max_entries:
            oldest = min(store, key=store.get)
            del store[oldest]

    def note_file(self, path: str) -> None:
        norm = _normalize_path(path)
        self.files[norm] = time.time()
        self._cap(self.files)

    def note_modified(self, path: str) -> None:
        norm = _normalize_path(path)
        self.files[norm] = time.time()
        self._cap(self.files)
        self.modified.add(norm)

    def note_dir(self, path: str) -> None:
        norm = _normalize_path(path)
        self.dirs[norm] = time.time()
        self._cap(self.dirs)

    def clear(self) -> None:
        self.files.clear()
        self.dirs.clear()
        self.modified.clear()

    def render(self) -> str:
        if not self.files and not self.dirs:
            return ""
        lines = [
            "\nKNOWN FROM EARLIER TASKS IN THIS SESSION "
            "(reuse directly; do NOT re-list the root to rediscover them; "
            "may be stale if files were edited):"
        ]
        for path in self.dirs:
            lines.append(f"- directory: {path}")
        for path in self.files:
            lines.append(f"- file: {path}")
        return "\n".join(lines) + "\n"