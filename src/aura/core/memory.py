"""
Session Memory: Lightweight cross-task memory within an interactive session.

Tracks files and directories actually observed in successful tool calls
so the agent can reference known paths directly without re-listing directories.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable

SESSION_MEMORY_MAX_ENTRIES = 40
WORKSPACE_MEMORY_MAX_FILES = 20
WORKSPACE_MEMORY_MAX_COMMANDS = 10
WORKSPACE_MEMORY_MAX_FACTS = 12

_COMMAND_PATTERN = re.compile(
    r"(?:`|\b)((?:python\s+-m\s+)?pytest(?:\s+[^\n`]+)?|npm\s+(?:test|run\s+[a-zA-Z0-9:_-]+)|node\s+[^\s`]+|cargo\s+test|go\s+test(?:\s+[^\n`]+)?)(?:`|$)",
    re.IGNORECASE,
)


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


def _merge_unique(existing: list[str], incoming: Iterable[str], limit: int) -> list[str]:
    """Append non-empty items keeping the most recent *limit* unique entries."""
    merged = [x for x in existing if isinstance(x, str) and x.strip()]
    for raw in incoming:
        item = " ".join(str(raw).split()).strip()
        if not item:
            continue
        if item in merged:
            merged.remove(item)
        merged.append(item)
    return merged[-limit:]


class WorkspaceMemoryStore:
    """Lightweight cross-chat SSD memory scoped per workspace path."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from ..config.settings import get_default_storage_dir
            root = get_default_storage_dir() / "workspace_memory"
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _workspace_key(workspace: str | Path) -> tuple[str, str]:
        try:
            norm = str(Path(workspace).expanduser().resolve())
        except Exception:
            norm = str(workspace).strip()
        digest = hashlib.sha256(norm.lower().encode("utf-8")).hexdigest()[:16]
        return norm, digest

    def _path_for(self, workspace: str | Path) -> tuple[str, Path]:
        norm, digest = self._workspace_key(workspace)
        return norm, self.root / f"{digest}.json"

    def load(self, workspace: str | Path) -> dict[str, Any]:
        norm, path = self._path_for(workspace)
        empty = {"workspace": norm, "updated_at": 0.0, "key_files": [], "commands": [], "facts": []}
        if not path.exists():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return empty
            return {
                "workspace": data.get("workspace", norm),
                "updated_at": data.get("updated_at", 0.0),
                "key_files": list(data.get("key_files", [])),
                "commands": list(data.get("commands", [])),
                "facts": list(data.get("facts", [])),
            }
        except Exception:
            return empty

    @staticmethod
    def extract_commands(text: str) -> list[str]:
        """Extract concise test/build commands mentioned in text."""
        found: list[str] = []
        for m in _COMMAND_PATTERN.finditer(text or ""):
            cmd = " ".join(m.group(1).strip().rstrip(".,;:)").split())
            if 4 <= len(cmd) <= 80 and cmd not in found:
                found.append(cmd)
        return found[:5]

    def record_turn(
        self,
        workspace: str | Path,
        *,
        key_files: Iterable[str] = (),
        commands: Iterable[str] = (),
        fact: str = "",
    ) -> dict[str, Any]:
        norm, path = self._path_for(workspace)
        data = self.load(workspace)
        data["workspace"] = norm
        data["updated_at"] = time.time()

        norm_files = [_normalize_path(f) for f in key_files if str(f).strip()]
        data["key_files"] = _merge_unique(data["key_files"], norm_files, WORKSPACE_MEMORY_MAX_FILES)
        data["commands"] = _merge_unique(data["commands"], commands, WORKSPACE_MEMORY_MAX_COMMANDS)

        clean_fact = " ".join(fact.split()).strip()
        if clean_fact:
            if len(clean_fact) > 180:
                clean_fact = clean_fact[:177] + "..."
            data["facts"] = _merge_unique(data["facts"], [clean_fact], WORKSPACE_MEMORY_MAX_FACTS)

        if data["key_files"] or data["commands"] or data["facts"]:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def clear_workspace(self, workspace: str | Path) -> bool:
        _, path = self._path_for(workspace)
        if path.exists():
            path.unlink()
            return True
        return False

    def build_context(self, workspace: str | Path, max_chars: int = 700) -> str:
        """Render concise cross-chat facts for prompt injection."""
        data = self.load(workspace)
        lines: list[str] = []
        if data["key_files"]:
            lines.append("- Key files: " + ", ".join(data["key_files"][-8:]))
        if data["commands"]:
            lines.append("- Known commands: " + "; ".join(data["commands"][-4:]))
        if data["facts"]:
            for item in data["facts"][-4:]:
                lines.append(f"- Note: {item}")
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[-max_chars:]
        return f"[Workspace Memory (persisted on SSD across chats)]\n{body}"
