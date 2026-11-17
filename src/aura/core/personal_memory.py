"""
Persistent Personal Memory Store (OpenJarvis-inspired, SSD-backed).

Stores durable user facts, preferences, and assistant personalization
on disk at `<storage_dir>/personal_facts.jsonl` with a clean human-readable
mirror at `<storage_dir>/USER_PROFILE.md`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any


TRUST_TRUSTED = "trusted"
TRUST_AUTO = "auto"
RECALLABLE_TIERS = frozenset({"", TRUST_TRUSTED, TRUST_AUTO})

_MAX_FACTS = 200
_MAX_FACT_CHARS = 220

# Basic injection guard so untrusted web/code text is never stored as personal instructions.
_UNSAFE_MEMORY_RE = re.compile(
    r"(ignore previous|system prompt|override instructions|<\|im_start\|>|powershell -enc|rm -rf)",
    re.IGNORECASE,
)

_REMEMBER_RE = re.compile(
    r"^(?:remember that|remember|note that|save to memory|don't forget)\s+(.+)$",
    re.IGNORECASE,
)
_PREF_PATTERNS = (
    re.compile(r"\b(?:my name is|call me)\s+([A-Za-z][A-Za-z0-9 _-]{1,40})", re.IGNORECASE),
    re.compile(r"\b(?:i prefer|always use|default to)\s+([^\n.!?]{3,80})", re.IGNORECASE),
)


@dataclass
class PersonalFact:
    """Single durable personal memory fact."""

    text: str
    category: str = "general"
    trust: str = TRUST_TRUSTED
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonalFact":
        return cls(
            text=str(data.get("text", "")).strip(),
            category=str(data.get("category", "general")).strip() or "general",
            trust=str(data.get("trust", TRUST_TRUSTED)).strip() or TRUST_TRUSTED,
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )


def _clean_text(text: str) -> str:
    cleaned = " ".join(str(text).split()).strip()
    if len(cleaned) > _MAX_FACT_CHARS:
        cleaned = cleaned[:_MAX_FACT_CHARS].rstrip()
    return cleaned


def extract_explicit_facts(prompt: str) -> list[tuple[str, str, str]]:
    """Deterministically extract durable facts explicitly stated by the user."""
    text = prompt.strip()
    if not text or _UNSAFE_MEMORY_RE.search(text):
        return []

    found: list[tuple[str, str, str]] = []
    m_rem = _REMEMBER_RE.match(text)
    if m_rem:
        body = _clean_text(m_rem.group(1).rstrip("."))
        if len(body) >= 3:
            found.append((body, "note", TRUST_TRUSTED))
        return found

    # Only inspect short personal messages for passive name/preference extraction.
    if len(text) <= 200 and "\n" not in text:
        for pat in _PREF_PATTERNS:
            m = pat.search(text)
            if m:
                snippet = _clean_text(m.group(0).rstrip(".!?"))
                if len(snippet) >= 6:
                    found.append((snippet, "preference", TRUST_AUTO))
    return found


_DURABLE_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("profile", re.compile(r"\b(?:my name is|call me)\s+([A-Za-z][A-Za-z .'-]{1,40})(?:[.,!?\n]|$)", re.I)),
    ("profile", re.compile(r"\b(i work (?:at|for|as)\s+[^.!?\n]{3,80})", re.I)),
    ("preference", re.compile(r"\b(i (?:prefer|always use)\s+[^.!?\n]{3,90})", re.I)),
    ("preference", re.compile(r"\b(my favorite\s+[^.!?\n]{3,60}\s+is\s+[^.!?\n]{2,60})", re.I)),
    ("project", re.compile(r"\b((?:i am building|we are working on|my goal is|our stack is)\s+[^.!?\n]{3,100})", re.I)),
)


def _clean_fact_candidate(raw: str) -> str:
    text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", str(raw or "")).strip()
    text = text.strip("`'\" ").rstrip(".")
    return re.sub(r"\s+", " ", text)


def coerce_fact_list(content: str, *, max_items: int = 8) -> list[str]:
    """Parse JSON array or markdown bullet list of facts safely (OpenJarvis port)."""
    raw = str(content or "").strip()
    if not raw:
        return []
    fenced = re.match(r"^```(?:json|markdown|md|text)?\s*(.*?)\s*```$", raw, re.I | re.S)
    if fenced:
        raw = fenced.group(1).strip()

    candidates: list[str] = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = parsed.get("facts") or parsed.get("items") or []
            if isinstance(parsed, list):
                candidates = [str(item) for item in parsed if isinstance(item, (str, int, float))]
        except Exception:
            candidates = []

    if not candidates:
        candidates = [line for line in raw.splitlines() if line.strip()]

    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        cleaned = _clean_fact_candidate(item)
        if len(cleaned) < 4 or len(cleaned) > 240:
            continue
        if _UNSAFE_MEMORY_RE.search(cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= max(1, max_items):
            break
    return out


def extract_durable_facts(user_text: str, assistant_text: str = "") -> list[tuple[str, str]]:
    """Extract durable user profile/preference/project facts from conversation turns."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(category: str, fact_text: str) -> None:
        cleaned = _clean_fact_candidate(fact_text)
        if len(cleaned) < 4 or len(cleaned) > 240 or _UNSAFE_MEMORY_RE.search(cleaned):
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        found.append((category, cleaned))

    for category, pattern in _DURABLE_FACT_PATTERNS:
        for match in pattern.finditer(user_text or ""):
            snippet = match.group(1).strip()
            if category == "profile" and pattern.pattern.startswith(r"\b(?:my name is"):
                snippet = f"User name is {snippet}"
            _add(category, snippet)

    if assistant_text:
        stripped = assistant_text.strip()
        if stripped.startswith("[") or stripped.upper().startswith("FACTS:"):
            payload = stripped[6:].strip() if stripped.upper().startswith("FACTS:") else stripped
            for item in coerce_fact_list(payload):
                _add("general", item)

    return found


class PersonalMemoryStore:
    """Thread-safe JSONL store for cross-project personal facts."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        if storage_dir is None:
            from ..config.settings import get_default_storage_dir
            storage_dir = get_default_storage_dir()
        self.storage_dir = Path(storage_dir)
        self.facts_path = self.storage_dir / "personal_facts.jsonl"
        self.profile_md_path = self.storage_dir / "USER_PROFILE.md"
        self._lock = threading.RLock()

    def _read_all_unlocked(self) -> list[PersonalFact]:
        if not self.facts_path.is_file():
            return []
        items: list[PersonalFact] = []
        try:
            for line in self.facts_path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    fact = PersonalFact.from_dict(json.loads(raw))
                    if fact.text:
                        items.append(fact)
                except Exception:
                    continue
        except OSError:
            return []
        return items

    def _write_all_unlocked(self, facts: list[PersonalFact]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        capped = facts[-_MAX_FACTS:]

        lines = [json.dumps(asdict(f), ensure_ascii=False) for f in capped]
        payload = ("\n".join(lines) + "\n") if lines else ""

        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.storage_dir, prefix=".facts_", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.facts_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        md_lines = ["# AURA Personal Memory Profile", ""]
        for f in capped:
            if f.trust in RECALLABLE_TIERS:
                md_lines.append(f"- [{f.category}] {f.text}")
        self.profile_md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    def list_facts(self, category: str | None = None) -> list[PersonalFact]:
        with self._lock:
            facts = [f for f in self._read_all_unlocked() if f.trust in RECALLABLE_TIERS]
            if category:
                cat = category.strip().lower()
                facts = [f for f in facts if f.category.lower() == cat]
            return facts

    def add_fact(self, text: str, category: str = "general", trust: str = TRUST_TRUSTED) -> PersonalFact | None:
        cleaned = _clean_text(text)
        if len(cleaned) < 3 or _UNSAFE_MEMORY_RE.search(cleaned):
            return None
        now = time.time()
        with self._lock:
            facts = self._read_all_unlocked()
            key = cleaned.lower()
            for idx, existing in enumerate(facts):
                if existing.text.lower() == key:
                    existing.updated_at = now
                    if trust == TRUST_TRUSTED:
                        existing.trust = TRUST_TRUSTED
                    if category != "general":
                        existing.category = category
                    # Move refreshed fact to end (most recent)
                    facts.append(facts.pop(idx))
                    self._write_all_unlocked(facts)
                    return existing
            fact = PersonalFact(
                text=cleaned,
                category=category.strip() or "general",
                trust=trust if trust in RECALLABLE_TIERS else TRUST_AUTO,
                created_at=now,
                updated_at=now,
            )
            facts.append(fact)
            self._write_all_unlocked(facts)
            return fact

    def remove_fact(self, query: str) -> int:
        needle = query.strip().lower()
        if not needle:
            return 0
        with self._lock:
            facts = self._read_all_unlocked()
            kept = [f for f in facts if needle not in f.text.lower()]
            removed = len(facts) - len(kept)
            if removed > 0:
                self._write_all_unlocked(kept)
            return removed

    def search_facts(self, query: str, limit: int = 8) -> list[PersonalFact]:
        tokens = {tok for tok in re.findall(r"[a-z0-9]+", query.lower()) if len(tok) >= 2}
        facts = self.list_facts()
        if not tokens:
            return facts[-limit:]
        scored: list[tuple[int, PersonalFact]] = []
        for f in facts:
            f_low = f.text.lower()
            hits = sum(1 for tok in tokens if tok in f_low)
            if hits > 0:
                scored.append((hits, f))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return [pair[1] for pair in scored[:limit]]

    def build_context(self, query: str = "", max_facts: int = 6, max_chars: int = 650) -> str:
        """Render relevant durable personal facts cleanly for prompt injection."""
        with self._lock:
            facts = self.list_facts()
        if not facts:
            return ""
        selected = self.search_facts(query, limit=max_facts) if query.strip() else []
        if len(selected) < max_facts:
            seen = {f.text.lower() for f in selected}
            for f in reversed(facts):
                if f.text.lower() not in seen:
                    selected.append(f)
                    seen.add(f.text.lower())
                if len(selected) >= max_facts:
                    break
        lines = ["[Personal Memory — User Preferences & Context]"]
        total = len(lines[0])
        for f in selected:
            line = f"- {f.text}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""
