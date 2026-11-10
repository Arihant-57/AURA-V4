"""
File inspection tracking for AURA V4.

Tracks what ranges of each file have actually been read, so the agent
knows what it has seen and what's missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileInspection:
    """Tracks inspection status of a single file."""
    path: str
    total_lines: int | None = None  # None = unknown until first read
    read_ranges: list[tuple[int, int]] = field(default_factory=list)  # (start, end) inclusive

    @property
    def lines_read(self) -> int:
        """Count of distinct lines read (merging overlapping ranges)."""
        merged = _merge_ranges(self.read_ranges)
        return sum(end - start + 1 for start, end in merged)

    @property
    def is_complete(self) -> bool:
        if self.total_lines is None or self.total_lines == 0:
            return len(self.read_ranges) > 0
        return self.lines_read >= self.total_lines

    @property
    def coverage(self) -> float:
        if self.total_lines is None or self.total_lines == 0:
            return 1.0 if self.read_ranges else 0.0
        return min(1.0, self.lines_read / self.total_lines)

    def record_read(self, start: int, end: int, total: int | None = None) -> None:
        self.read_ranges.append((start, end))
        if total is not None:
            self.total_lines = total

    @property
    def missing_ranges(self) -> list[tuple[int, int]]:
        """Return ranges not yet read. Requires total_lines to be known."""
        if self.total_lines is None:
            return []
        merged = _merge_ranges(self.read_ranges)
        missing = []
        cursor = 1
        for start, end in merged:
            if start > cursor:
                missing.append((cursor, start - 1))
            cursor = end + 1
        if cursor <= self.total_lines:
            missing.append((cursor, self.total_lines))
        return missing


@dataclass
class InspectionTracker:
    """Tracks file inspections across the task."""
    files: dict[str, FileInspection] = field(default_factory=dict)

    def record(self, path: str, start: int, end: int, total: int | None = None) -> None:
        norm = path.replace("\\", "/").strip()
        if norm not in self.files:
            self.files[norm] = FileInspection(path=norm)
        self.files[norm].record_read(start, end, total)

    def get(self, path: str) -> FileInspection | None:
        return self.files.get(path.replace("\\", "/").strip())

    def is_fully_read(self, path: str) -> bool:
        fi = self.get(path)
        return fi is not None and fi.is_complete

    def summary(self) -> str:
        if not self.files:
            return "No files inspected."
        lines = []
        for fi in self.files.values():
            pct = f"{fi.coverage:.0%}" if fi.total_lines else "partial"
            lines.append(f"  {fi.path}: {pct} ({fi.lines_read} lines)")
        return "Inspected files:\n" + "\n".join(lines)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged = [sorted_r[0]]
    for start, end in sorted_r[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged
