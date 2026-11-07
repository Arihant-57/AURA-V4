"""
Evidence Models and Extraction logic for AURA V4.

Handles evidence item representation, prefetch read planning, and
structural source skeleton extraction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from ..config import get_settings
from .evidence_constants import (
    MANIFEST_NAMES, LOCK_FILES, PREFETCH_MAX_READS,
    PREFETCH_SKELETON_CHARS, PREFETCH_HEAD_CHARS, PREFETCH_TAIL_CHARS,
    _SOURCE_FILTER_PATTERNS, _ANALYSIS_VERB_TOKENS, _QUESTION_STARTERS,
    _DEPENDENCY_QUESTION_TOKENS, PrefetchPolicy, BACKEND_PREFETCH_POLICY,
    FRONTEND_PREFETCH_POLICY, ROOT_MANIFEST_POLICY
)
from .tools import check_path_policy, sanitize_tool_result, mcp_result_to_text

if TYPE_CHECKING:
    from .scope import TaskScope
    from .cache import ResultCache
    from .memory import SessionMemory
    from .mcp_hub import McpHub

logger = logging.getLogger(__name__)


@dataclass
class EvidenceItem:
    """One verified observation from an MCP tool call."""
    source: str            # MCP server name, e.g. "filesystem"
    tool: str              # tool that produced it, e.g. "read_file"
    path: str              # path as actually accessed
    content: str           # observed content (or error text)
    complete: bool         # False if truncated or structurally extracted
    retrieval_method: str  # "prefetch-listing" | "prefetch-read" | "prefetch-skeleton"
    note: str = ""


def _listing_entries(text: str) -> list[str]:
    """Extract entry names from a directory listing."""
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("(") or "TRUNCATED" in stripped:
            continue
        for token in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*", stripped):
            token = token.rstrip("/")
            if token and token not in {"FILE", "DIR"}:
                names.append(token)
    return names


def plan_prefetch_reads(listing_text: str, base_dir: str, policy: "PrefetchPolicy") -> list[str]:
    """Choose files to read from an ACTUAL listing."""
    entries = _listing_entries(listing_text)
    seen: dict[str, str] = {}
    for entry in entries:
        seen.setdefault(entry.lower(), entry)

    manifests: list[str] = []
    entrypoints: list[str] = []
    sources: list[str] = []
    for low, original in seen.items():
        base = low.rsplit("/", 1)[-1]
        if base in LOCK_FILES:
            continue
        if base in MANIFEST_NAMES or base.endswith(".csproj"):
            manifests.append(original)
        elif base in policy.entrypoint_names:
            entrypoints.append(original)
        elif policy.source_suffixes and base.endswith(policy.source_suffixes):
            sources.append(original)

    picks = manifests + entrypoints
    if not entrypoints:
        picks += sources[:2 if not manifests else 1]
    picks = picks[:PREFETCH_MAX_READS]

    prefix = "" if base_dir == "." else base_dir.rstrip("/") + "/"
    return [p if "/" in p else prefix + p for p in picks]


def extract_source_skeleton(text: str, budget: int = PREFETCH_SKELETON_CHARS) -> tuple[str, bool]:
    """Deterministic structural extraction for oversized source files."""
    if len(text) <= budget:
        return text, True
    head = text[:PREFETCH_HEAD_CHARS]
    tail = text[-PREFETCH_TAIL_CHARS:]
    signal_lines = []
    compiled = [re.compile(p, re.IGNORECASE) for p in _SOURCE_FILTER_PATTERNS]
    for i, line in enumerate(text.splitlines()):
        if line.strip() and any(r.search(line) for r in compiled):
            signal_lines.append(f"  L{i + 1}: {line.strip()[:160]}")
    skeleton = (
        f"[HEAD of file — first {PREFETCH_HEAD_CHARS} chars]\n{head}\n\n"
        f"[SIGNAL LINES — imports/exports/routes/definitions/bootstrap, "
        f"{len(signal_lines)} matched, with original line numbers]\n"
        + ("\n".join(signal_lines) if signal_lines else "(none matched)")
        + f"\n\n[TAIL of file — last {PREFETCH_TAIL_CHARS} chars]\n{tail}"
    )
    if len(skeleton) > budget:
        skeleton = skeleton[:budget] + "\n[skeleton trimmed to budget]"
    return skeleton, False


def evidence_is_sufficient(items: list[EvidenceItem]) -> bool:
    """Minimum bar for the single-synthesis path."""
    def ok(item: EvidenceItem) -> bool:
        return (
            not item.content.startswith("MCP ")
            and not item.content.startswith("ERROR")
            and bool(item.content.strip())
        )

    listings = [i for i in items if i.tool == "list_directory" and ok(i)]
    reads = [i for i in items if i.tool == "read_file" and ok(i)]
    return bool(listings and reads)


def select_prefetch_policy(scope: "TaskScope") -> PrefetchPolicy:
    """Select the prefetch policy based on task scope."""
    if scope.backend and not scope.frontend:
        return BACKEND_PREFETCH_POLICY
    if scope.frontend and not scope.backend:
        return FRONTEND_PREFETCH_POLICY
    return ROOT_MANIFEST_POLICY


def is_prefetch_candidate(task: str, scope: "TaskScope") -> bool:
    """Determine if a task is a candidate for evidence prefetching."""
    settings = get_settings()
    if not settings.prefetch.enabled:
        return False
    if scope.conversational or scope.git:
        return False

    task_lower = task.lower()

    # Mutation / coding tasks requiring changes or running tests must enter the agent loop
    if re.search(r"\b(fix\s+the|fix\s+it|please\s+fix|modify|edit|write|patch|implement|create\s+file|run\s+tests?)\b", task_lower):
        return False

    has_analysis_verb = any(v in task_lower for v in _ANALYSIS_VERB_TOKENS)
    has_question = any(task_lower.startswith(q) or f" {q} " in task_lower for q in _QUESTION_STARTERS)
    has_dep_token = any(d in task_lower for d in _DEPENDENCY_QUESTION_TOKENS)

    repo_keywords = {
        "repo", "repository", "codebase", "project", "architecture",
        "structure", "overview", "stack", "framework"
    }
    has_repo_target = any(k in task_lower for k in repo_keywords)

    if scope.backend or scope.frontend:
        return bool(has_analysis_verb or has_question or has_dep_token or has_repo_target)

    return bool((has_analysis_verb or has_question or has_dep_token) and (has_repo_target or has_dep_token))


async def collect_prefetch_evidence(
    task: str,
    scope: "TaskScope",
    hub: "McpHub",
    cache: "ResultCache",
    memory: "SessionMemory",
) -> list[EvidenceItem]:
    """
    Collect evidence via MCP filesystem tools for a candidate task.
    Enforces safe_path, TaskScope, check_path_policy, and result sanitization.
    """
    settings = get_settings()
    items: list[EvidenceItem] = []

    fs_client = hub.tool_clients.get("list_directory")
    read_client = hub.tool_clients.get("read_file")
    if not fs_client or not read_client:
        return items

    policy = select_prefetch_policy(scope)

    # Determine candidate directories to inspect
    candidate_dirs = list(policy.candidate_dirs)
    if "." not in candidate_dirs:
        candidate_dirs.append(".")

    successful_listings: dict[str, str] = {}

    for dir_path in candidate_dirs:
        policy_err = check_path_policy("list_directory", {"path": dir_path}, scope)
        if policy_err:
            continue

        cache_key = ("list_directory", json.dumps({"path": dir_path}, sort_keys=True))
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            raw_text = cached_result
        else:
            try:
                res = await fs_client.call_tool("list_directory", {"path": dir_path})
                raw_text = mcp_result_to_text(res)
            except Exception as exc:
                logger.debug("Prefetch list_directory('%s') exception: %s", dir_path, exc)
                continue

        if raw_text.startswith("ERROR") or raw_text.startswith("MCP TOOL ERROR"):
            continue

        sanitized_listing = sanitize_tool_result("list_directory", raw_text, scope.prompt_lower, scope)
        cache.put(cache_key, sanitized_listing)
        memory.note_dir(dir_path)

        items.append(EvidenceItem(
            source="filesystem",
            tool="list_directory",
            path=dir_path,
            content=sanitized_listing,
            complete=True,
            retrieval_method="prefetch-listing"
        ))
        successful_listings[dir_path] = raw_text

        if len(successful_listings) >= 2:
            break

    if not successful_listings:
        return items

    # Plan file reads from listings
    planned_reads: list[str] = []
    for dir_path, raw_listing in successful_listings.items():
        picks = plan_prefetch_reads(raw_listing, dir_path, policy)
        for pick in picks:
            if pick not in planned_reads:
                planned_reads.append(pick)

    planned_reads = planned_reads[:settings.prefetch.max_reads]

    for file_path in planned_reads:
        policy_err = check_path_policy("read_file", {"path": file_path}, scope)
        if policy_err:
            continue

        cache_key = ("read_file", json.dumps({"path": file_path}, sort_keys=True))
        cached_read = cache.get(cache_key)
        if cached_read is not None:
            raw_content = cached_read
        else:
            try:
                res = await read_client.call_tool("read_file", {"path": file_path})
                raw_content = mcp_result_to_text(res)
            except Exception as exc:
                logger.debug("Prefetch read_file('%s') exception: %s", file_path, exc)
                continue

        if raw_content.startswith("ERROR") or raw_content.startswith("MCP TOOL ERROR"):
            continue

        sanitized_content = sanitize_tool_result("read_file", raw_content, scope.prompt_lower, scope)
        cache.put(cache_key, sanitized_content)
        memory.note_file(file_path)

        if len(sanitized_content) > settings.prefetch.skeleton_chars:
            content_to_use, complete = extract_source_skeleton(
                sanitized_content,
                budget=settings.prefetch.skeleton_chars
            )
            retrieval = "prefetch-skeleton"
        else:
            content_to_use = sanitized_content
            complete = True
            retrieval = "prefetch-read"

        items.append(EvidenceItem(
            source="filesystem",
            tool="read_file",
            path=file_path,
            content=content_to_use,
            complete=complete,
            retrieval_method=retrieval
        ))

    return items
