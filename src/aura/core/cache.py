"""
ResultCache: TTL cache for identical successful tool calls within a session.

Errors are never cached and invalidate any existing entry.
"""

from __future__ import annotations

import time
from typing import Optional


class ResultCache:
    """
    TTL cache for identical successful tool calls within the session.
    Errors are never cached and invalidate any existing entry.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, str]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, key: tuple[str, str]) -> Optional[str]:
        if not self.enabled:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        timestamp, value = entry
        if time.time() - timestamp > self.ttl:
            del self._store[key]
            return None
        return value

    def put(self, key: tuple[str, str], value: str) -> None:
        if self.enabled:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: tuple[str, str]) -> None:
        self._store.pop(key, None)

    def invalidate_path(self, path: str) -> None:
        """Invalidate all cached entries associated with a file path."""
        norm_path = path.replace("\\", "/").strip().lower()
        while norm_path.startswith("./"):
            norm_path = norm_path[2:]
        to_del = []
        for key in list(self._store.keys()):
            tool_name, args_json = key
            if tool_name in {"read_file", "write_file", "edit_file"}:
                try:
                    import json
                    args = json.loads(args_json)
                    arg_p = args.get("path", "").replace("\\", "/").strip().lower()
                    while arg_p.startswith("./"):
                        arg_p = arg_p[2:]
                    if arg_p == norm_path:
                        to_del.append(key)
                except Exception:
                    pass
        for k in to_del:
            self._store.pop(k, None)

    def clear(self) -> None:
        self._store.clear()