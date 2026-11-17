"""
Agent Execution Loop for AURA V4.

Coordinates:
- Intent scoping
- MCP tool discovery
- Tool execution boundary (active tools)
- Result sanitization
- Multi-turn model conversation
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import queue
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..config import get_settings
from ..core import (
    McpHub, SessionMemory, ResultCache, TaskScope,
    select_active_tools, check_path_policy, sanitize_tool_result,
    mcp_tool_to_ollama, mcp_result_to_text, validate_arguments,
    normalize_tool_arguments, validate_diagnosis_answer, validate_control_flow,
    is_prefetch_candidate, collect_prefetch_evidence, evidence_is_sufficient,
    ExecutionController, InspectionTracker, LoopGuard, LoopGuardConfig,
    ErrorCategory, classify_tool_result, is_tool_error_text,
)
from ..core.file_tracker import _merge_ranges
from ..core.providers import (
    OllamaProvider,
    create_local_qwen_fallback,
    ensure_ollama_running,
    is_cloud_model_spec,
)
from ..core.scope import detect_browser_intent, detect_desktop_intent, detect_jarvis_intent
from ..core.model_router import TaskState, TaskStage
from ..core.prompts import (
    SYSTEM_PROMPT, JARVIS_CONVERSATIONAL_PROMPT, JARVIS_ASSISTANT_PROMPT,
    CODING_RULES, SPECIALIST_ROLE_PROMPTS,
    DIAGNOSIS_NUDGE_MESSAGE, CHANGE_PLAN_NUDGE_MESSAGE,
    FALLBACK_NUDGE_MESSAGE
)

logger = logging.getLogger(__name__)

# Pure read-only tools that can safely execute concurrently within a single turn.
_PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset({
    "read_file", "list_directory", "search_files", "file_outline", "scan_repository", "get_file_diff",
    "git_status", "git_branch", "git_log", "git_diff", "git_ls_files",
    "jarvis_web_search", "jarvis_fetch_url", "desktop_list_windows", "desktop_list_monitors",
    "desktop_system_info", "desktop_find_files", "desktop_inspect_cursor", "desktop_inspect_path",
})

# Poll/snapshot/OCR tools subject to LoopGuard poll budgeting and stale-noise compaction.
_POLL_TOOLS: frozenset[str] = frozenset({
    "desktop_read_screen_ocr", "desktop_ui_see", "desktop_list_windows",
    "desktop_inspect_cursor", "browser_snapshot",
})
_NOISY_SUPERSEDABLE_TOOLS: frozenset[str] = _POLL_TOOLS | frozenset({
    "list_directory", "search_files", "desktop_find_files",
})


async def execute_tool_call(
    name: str,
    arguments: dict,
    step: int,
    *,
    active_tool_names: set[str],
    hub: McpHub,
    cache: ResultCache,
    memory: SessionMemory,
    scope: TaskScope,
) -> tuple[str, dict]:
    """Execute a single MCP tool call with policy checks."""
    meta = {"duplicate": False, "blocked": False, "cache_hit": False, "duration": 0.0, "error": False}

    # 0. Canonicalize model argument aliases
    arguments = normalize_tool_arguments(name, arguments)

    # 1. Boundary check
    if name not in active_tool_names:
        meta["error"] = True
        return f"MCP ERROR: tool '{name}' not permitted.", meta

    # 2. Path policy
    blocked = check_path_policy(name, arguments, scope)
    if blocked:
        meta["blocked"] = True
        return blocked, meta

    # 3. Cache check
    signature = (name, json.dumps(arguments, sort_keys=True))
    hit = cache.get(signature)
    if hit:
        meta["cache_hit"] = True
        meta["duplicate"] = True
        return hit, meta

    # 4. Schema validation
    schema = hub.tool_schemas.get(name)
    problem = validate_arguments(schema, arguments)
    if problem:
        meta["error"] = True
        return problem, meta

    # 5. Execution with timeout
    client = hub.tool_clients.get(name)
    if client is None:
        meta["error"] = True
        return f"MCP ERROR: no client for tool '{name}'.", meta

    start = time.perf_counter()
    try:
        settings = get_settings()
        timeout = settings.agent.mcp_tool_timeout_seconds
        result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout)
        text = mcp_result_to_text(result)
    except asyncio.TimeoutError:
        text = f"MCP TOOL ERROR: tool '{name}' timed out after {timeout}s."
        meta["error"] = True
        if hasattr(hub, "mark_broken") and hasattr(hub, "tool_server"):
            hub.mark_broken(hub.tool_server.get(name, ""))
    except Exception as e:
        text = f"MCP TOOL ERROR: {e}"
        meta["error"] = True
        if hasattr(hub, "mark_broken") and hasattr(hub, "tool_server"):
            hub.mark_broken(hub.tool_server.get(name, ""))

    meta["duration"] = time.perf_counter() - start

    # 6. Sanitize and Cache (only cache successful operations, never cache mutating operations)
    sanitized = sanitize_tool_result(name, text, scope.prompt_lower, scope)
    if not meta["error"]:
        if name in {"write_file", "edit_file", "run_tests"}:
            # Never cache mutating or test operations; invalidate any cached reads
            if name in {"write_file", "edit_file"}:
                cache.invalidate_path(arguments.get("path", ""))
                if hasattr(cache, "invalidate_mutation"):
                    cache.invalidate_mutation()
                memory.note_modified(arguments.get("path", ""))
        else:
            cache.put(signature, sanitized)
        if name == "read_file":
            memory.note_file(arguments.get("path", ""))
        elif name == "list_directory":
            memory.note_dir(arguments.get("path", "."))

    return sanitized, meta


def _extract_tool_call_meta(tc: Any) -> dict[str, Any]:
    """Extract optional OpenAI/Gemini tool-call metadata (id, thought_signature, extra_content)."""
    meta: dict[str, Any] = {}
    is_dict = isinstance(tc, dict)
    for key in ("id", "extra_content", "thought_signature", "provider_specific_fields"):
        val = tc.get(key) if is_dict else getattr(tc, key, None)
        if val is not None:
            meta[key] = val
    return meta


def _serialize_tool_calls(tool_calls: list) -> list[dict]:
    """Convert Ollama ToolCall objects to plain dicts for message history.

    This prevents the hybrid Message-object/dict problem that causes
    the model to lose track of tool calls across turns.
    """
    serialized = []
    for tc in tool_calls:
        func = getattr(tc, "function", None)
        if func is None:
            continue
        name = getattr(func, "name", "")
        args = getattr(func, "arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        elif not isinstance(args, dict):
            args = {}
        entry: dict[str, Any] = {"function": {"name": str(name), "arguments": args}}
        entry.update(_extract_tool_call_meta(tc))
        serialized.append(entry)
    return serialized


def _deduplicate_tool_calls(tool_calls: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Remove duplicate tool calls within a single turn.

    Small models sometimes emit the same call multiple times in one turn
    (occasionally using parameter aliases like ``file_path`` vs ``path``).
    Normalize arguments and keep only the first occurrence of each (name, args) pair.
    """
    seen: set[tuple[str, str]] = set()
    unique = []
    for name, args in tool_calls:
        norm_args = normalize_tool_arguments(name, args if isinstance(args, dict) else {})
        key = (name, json.dumps(norm_args, sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique.append((name, norm_args))
    return unique


def _parse_text_tool_calls(content: str, active_tool_names: set[str]) -> list[tuple[str, dict]]:
    """Extract tool calls written as text/JSON in model replies."""
    if not content or not content.strip():
        return []

    calls = []
    # 1. <tool_call> tags
    tag_matches = re.findall(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL | re.IGNORECASE)
    for block in tag_matches:
        try:
            data = json.loads(block.strip())
            name = data.get("name") or data.get("tool")
            args = data.get("arguments") or data.get("parameters") or {}
            if name in active_tool_names and isinstance(args, dict):
                calls.append((str(name), args))
        except Exception:
            pass

    if calls:
        return calls

    # 2. Markdown json code blocks
    code_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    for block in code_matches:
        try:
            data = json.loads(block.strip())
            name = data.get("name") or data.get("tool")
            args = data.get("arguments") or data.get("parameters") or {}
            if name in active_tool_names and isinstance(args, dict):
                calls.append((str(name), args))
        except Exception:
            import ast
            try:
                data = ast.literal_eval(block.strip())
                if isinstance(data, dict):
                    name = data.get("name") or data.get("tool")
                    args = data.get("arguments") or data.get("parameters") or {}
                    if name in active_tool_names and isinstance(args, dict):
                        calls.append((str(name), args))
            except Exception:
                pass

    if calls:
        return calls

    # 3. Direct JSON object with "name" and "arguments" (or single quotes)
    stripped = content.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or ('"name":' in stripped or "'name':" in stripped):
        start_idx = stripped.find("{")
        end_idx = stripped.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = stripped[start_idx:end_idx + 1]
            try:
                data = json.loads(candidate)
                name = data.get("name") or data.get("tool")
                args = data.get("arguments") or data.get("parameters") or {}
                if name in active_tool_names and isinstance(args, dict):
                    calls.append((str(name), args))
            except Exception:
                import ast
                try:
                    data = ast.literal_eval(candidate)
                    if isinstance(data, dict):
                        name = data.get("name") or data.get("tool")
                        args = data.get("arguments") or data.get("parameters") or {}
                        if name in active_tool_names and isinstance(args, dict):
                            calls.append((str(name), args))
                except Exception:
                    pass

    if calls:
        return calls

    # 4. Action / Tool block pattern (e.g. Action: read_file \n Action Input: {"path": "server.js"})
    action_match = re.search(r"(?:Action|Tool):\s*([a-zA-Z0-9_-]+)\s*\n+(?:Action Input|Arguments|Input):\s*(.*)", content, re.IGNORECASE | re.DOTALL)
    if action_match:
        name = action_match.group(1).strip()
        arg_str = action_match.group(2).strip()
        if name in active_tool_names:
            try:
                if arg_str.startswith("{"):
                    end_b = arg_str.find("}")
                    if end_b != -1:
                        arg_str = arg_str[:end_b+1]
                args = json.loads(arg_str)
                if isinstance(args, dict):
                    calls.append((name, args))
            except Exception:
                clean_arg = arg_str.strip().strip("'\"`")
                if "\n" in clean_arg:
                    clean_arg = clean_arg.splitlines()[0].strip().strip("'\"`")
                if name in {"read_file", "file_outline", "list_directory", "scan_repository"}:
                    calls.append((name, {"path": clean_arg}))
                elif name == "search_files":
                    calls.append((name, {"query": clean_arg}))
    if calls:
        return calls

    # 5. Function call syntax: tool_name(path="server.js") or tool_name("server.js")
    for name in active_tool_names:
        fn_pattern = rf"\b{re.escape(name)}\s*\(\s*(.*?)\s*\)"
        for match in re.finditer(fn_pattern, content):
            raw_args = match.group(1).strip()
            args = {}
            if not raw_args:
                if name in {"list_directory", "scan_repository", "get_file_diff", "run_tests", "git_status", "git_branch", "git_log", "git_diff", "git_ls_files", "desktop_system_info"}:
                    calls.append((name, {}))
                continue
            kw_matches = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(["\'](?:\\.|[^"\'])*["\']|\d+|True|False)', raw_args)
            if kw_matches:
                for k, v in kw_matches:
                    v_clean = v.strip()
                    if (v_clean.startswith('"') and v_clean.endswith('"')) or (v_clean.startswith("'") and v_clean.endswith("'")):
                        args[k] = v_clean[1:-1]
                    elif v_clean.isdigit():
                        args[k] = int(v_clean)
                    elif v_clean.lower() == "true":
                        args[k] = True
                    elif v_clean.lower() == "false":
                        args[k] = False
                    else:
                        args[k] = v_clean
                calls.append((name, args))
            else:
                val = raw_args.strip().strip('"\'`')
                if name in {"read_file", "file_outline", "list_directory", "scan_repository"}:
                    calls.append((name, {"path": val}))
                elif name == "search_files":
                    calls.append((name, {"query": val}))
                elif name == "search_code":
                    calls.append((name, {"pattern": val}))
                elif name == "run_tests":
                    calls.append((name, {"test_path": val} if val else {}))
                elif name == "get_file_diff":
                    calls.append((name, {"path": val} if val else {}))
        if calls:
            return calls

    # 6. Colon syntax: tool_name: <arg> (e.g. `read_file: server.js` or `read_file: {"path": "server.js"}`)
    for line in content.splitlines():
        line = line.strip().strip("`")
        for name in active_tool_names:
            prefix = f"{name}:"
            if line.startswith(prefix):
                rest = line[len(prefix):].strip()
                if rest.startswith("{") and rest.endswith("}"):
                    try:
                        data = json.loads(rest)
                        if isinstance(data, dict):
                            calls.append((name, data))
                            continue
                    except Exception:
                        pass
                clean_val = rest.strip().strip("\"'`")
                if clean_val:
                    if name in {"read_file", "file_outline", "list_directory", "scan_repository"}:
                        calls.append((name, {"path": clean_val}))
                    elif name == "search_files":
                        calls.append((name, {"query": clean_val}))
                    elif name == "search_code":
                        calls.append((name, {"pattern": clean_val}))
                    elif name == "run_tests":
                        calls.append((name, {"test_path": clean_val}))
                    elif name == "get_file_diff":
                        calls.append((name, {"path": clean_val}))
        if calls:
            return calls

    # 7. Code block naming tool, e.g. ```read_file\nserver.js\n```
    for name in active_tool_names:
        block_match = re.search(rf"```{re.escape(name)}\s*\n(.*?)\n```", content, re.DOTALL | re.IGNORECASE)
        if block_match:
            inner = block_match.group(1).strip()
            if "path:" in inner.lower():
                m = re.search(r"path:\s*(.*)", inner, re.IGNORECASE)
                if m:
                    calls.append((name, {"path": m.group(1).strip().strip("\"'`")}))
            elif inner and "\n" not in inner:
                calls.append((name, {"path": inner.strip().strip("\"'`")}))

    return calls


def _looks_like_tool_call(content: str) -> bool:
    """Check if content looks like a malformed or text-based tool call."""
    lowered = content.strip().lower()
    tool_keywords = (
        "read_file", "write_file", "edit_file", "list_directory", "scan_repository",
        "file_outline", "search_files", "search_code", "run_tests", "get_file_diff",
        "git_status", "git_diff", "git_log", "jarvis_open_website", "desktop_system_info",
    )
    if "<tool_call>" in lowered:
        return True
    if "```json" in lowered and ("\"tool\"" in lowered or "\"name\"" in lowered or "\"arguments\"" in lowered):
        return True
    if lowered.startswith("{") and ("\"name\"" in lowered or "\"tool\"" in lowered or "'name'" in lowered or "'tool'" in lowered):
        return True
    for tk in tool_keywords:
        if f"{tk}:" in lowered or f"{tk}(" in lowered or f"```{tk}" in lowered:
            return True
        if re.search(rf"\b(?:action|tool):\s*{tk}\b", lowered):
            return True
    return False


def prune_conversation_history(messages: list[dict], settings: Any) -> list[dict]:
    """Prune and compact older tool results to stay well within context budget.

    Preserves all message roles and sequence (required by tool-calling API),
    compacts superseded noisy snapshots/listings, and shortens older bulky
    tool results when history exceeds configured thresholds.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep_recent = getattr(settings.agent, "keep_recent_tool_results", 4)
    shortened_limit = getattr(settings.mcp, "shortened_tool_result_chars", 600)

    chars_per_tok = getattr(settings.agent, "chars_per_token_estimate", 3.2)
    total_chars = sum(len(m.get("content") or "") for m in messages)
    est_tokens = total_chars / chars_per_tok
    max_safe_tokens = settings.model.num_ctx * settings.agent.context_warn_ratio

    needs_pruning = est_tokens > max_safe_tokens or len(tool_indices) > keep_recent

    if not needs_pruning or len(tool_indices) <= keep_recent:
        return messages

    latest_idx_by_tool: dict[str, int] = {}
    for idx in tool_indices:
        tname = messages[idx].get("name") or ""
        if tname:
            latest_idx_by_tool[tname] = idx

    # Keep the most recent `keep_recent` tool messages intact; shorten older ones
    older_indices = set(tool_indices[:-keep_recent])

    pruned = []
    for idx, msg in enumerate(messages):
        if idx in older_indices and msg.get("role") == "tool":
            content = msg.get("content") or ""
            tname = msg.get("name") or ""
            is_superseded_noise = (
                tname in _NOISY_SUPERSEDABLE_TOOLS
                and latest_idx_by_tool.get(tname, -1) > idx
            )
            target_limit = min(shortened_limit, 240) if is_superseded_noise else shortened_limit
            compact_content = re.sub(r"\n{3,}", "\n\n", content) if is_superseded_noise else content
            if len(compact_content) > target_limit or is_superseded_noise and len(content) > target_limit:
                new_msg = dict(msg)
                note = f"superseded older {tname}, " if is_superseded_noise else ""
                new_msg["content"] = (
                    compact_content[:target_limit]
                    + f"\n\n[PRUNED: older tool result shortened ({note}{len(content)} chars -> {target_limit})]"
                )
                pruned.append(new_msg)
                continue
        pruned.append(msg)

    return pruned


def _coerce_steer_items(raw: Any) -> list[str]:
    """Normalize pending steer values into non-empty stripped strings."""
    if not raw:
        return []
    if isinstance(raw, str):
        clean = raw.strip()
        return [clean] if clean else []
    items: list[str] = []
    if isinstance(raw, (list, tuple, deque)):
        for elem in raw:
            if isinstance(elem, str) and elem.strip():
                items.append(elem.strip())
    return items


def _drain_steer_queue(steer_queue: Any) -> list[str]:
    """Non-blockingly drain any pending steer instructions from a queue/deque/list."""
    if steer_queue is None:
        return []
    drained: list[str] = []
    try:
        if hasattr(steer_queue, "get_nowait"):
            while True:
                try:
                    item = steer_queue.get_nowait()
                except (queue.Empty, asyncio.QueueEmpty):
                    break
                drained.extend(_coerce_steer_items(item))
        elif isinstance(steer_queue, deque):
            while steer_queue:
                drained.extend(_coerce_steer_items(steer_queue.popleft()))
        elif isinstance(steer_queue, list):
            if steer_queue:
                snapshot = list(steer_queue)
                steer_queue.clear()
                drained.extend(_coerce_steer_items(snapshot))
        elif hasattr(steer_queue, "pop_pending_steers") and callable(steer_queue.pop_pending_steers):
            drained.extend(_coerce_steer_items(steer_queue.pop_pending_steers()))
    except Exception as exc:
        logger.debug("Failed to drain steer_queue cleanly: %s", exc)
    return drained


async def _drain_pending_steers(
    steer_queue: Any = None,
    get_pending_steer: Callable[[], Any] | None = None,
    memory: Any = None,
) -> list[str]:
    """Drain FIFO mid-run user steer instructions from explicit hooks or memory fallback."""
    collected: list[str] = []
    if callable(get_pending_steer):
        try:
            raw = get_pending_steer()
            if inspect.isawaitable(raw):
                raw = await raw
            collected.extend(_coerce_steer_items(raw))
        except Exception as exc:
            logger.debug("get_pending_steer failed cleanly: %s", exc)
    if steer_queue is not None:
        collected.extend(_drain_steer_queue(steer_queue))
    if get_pending_steer is None and steer_queue is None and memory is not None:
        mem_getter = getattr(memory, "get_pending_steer", None) or getattr(memory, "drain_steers", None)
        if callable(mem_getter):
            try:
                raw = mem_getter()
                if inspect.isawaitable(raw):
                    raw = await raw
                collected.extend(_coerce_steer_items(raw))
            except Exception as exc:
                logger.debug("memory.get_pending_steer failed cleanly: %s", exc)
        mem_queue = getattr(memory, "steer_queue", None)
        if mem_queue is not None:
            collected.extend(_drain_steer_queue(mem_queue))
    return collected


def _classify_terminal_tool_outcome(res_text: str) -> str | None:
    """Detect authoritative blocking or terminal tool responses for fast-exit decisions."""
    stripped = (res_text or "").lstrip()
    if stripped.startswith(("SSRF BLOCKED:", "SECURITY BLOCKED:")):
        return "security_blocked"
    if stripped.startswith("READ-ONLY BLOCKED:"):
        return "ro_blocked"
    if stripped.startswith("NOT FOUND:"):
        return "not_found"
    return None


def _check_already_inspected_read(
    tracker: InspectionTracker,
    arguments: dict,
) -> str | None:
    """Return a concise notice when a new read_file call overlaps already-read complete ranges."""
    path = arguments.get("path", "")
    if not isinstance(path, str) or not path.strip():
        return None
    fi = tracker.get(path)
    if fi is None:
        return None

    req_start = arguments.get("start_line")
    req_end = arguments.get("end_line")
    if req_start is None and req_end is None:
        if fi.total_lines is not None and fi.is_complete:
            return (
                f"ALREADY INSPECTED: '{path}' was already read in full "
                f"({fi.lines_read}/{fi.total_lines} lines). Use the contents already in context."
            )
        return None

    if isinstance(req_start, int) or isinstance(req_end, int):
        rs = req_start if isinstance(req_start, int) and req_start > 0 else 1
        re_line = req_end if isinstance(req_end, int) and req_end >= rs else (fi.total_lines or rs)
        for m_start, m_end in _merge_ranges(fi.read_ranges):
            if m_start <= rs and re_line <= m_end:
                return (
                    f"ALREADY INSPECTED: '{path}' lines {rs}-{re_line} are already in context "
                    f"(covered by read range {m_start}-{m_end})."
                )
    return None


async def _emit_loop_event(on_event: Callable[[dict[str, Any]], Any] | None, **event: Any) -> None:
    if on_event is None:
        return
    try:
        res = on_event(event)
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


async def run_agent_loop(
    task: str,
    provider: OllamaProvider,
    hub: McpHub,
    memory: SessionMemory,
    cache: ResultCache,
    *,
    task_state: TaskState | None = None,
    inspection_tracker: InspectionTracker | None = None,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    steer_queue: Any = None,
    get_pending_steer: Callable[[], Any] | None = None,
) -> str:
    """The main reasoning loop."""
    settings = get_settings()
    routing_task = task.split("[Current Request]\n", 1)[-1] if "[Current Request]\n" in task else task
    active_agent = None
    if task_state is not None and getattr(task_state, "active_agent", "auto") != "auto":
        active_agent = task_state.active_agent
    scope = TaskScope(routing_task, agent_role=active_agent)
    if (
        "[Current Request]\n" in task
        and scope.agent_role == "auto"
        and not scope.conversational
        and not (scope.git or scope.backend or scope.frontend)
        and not (scope.desktop or scope.jarvis or scope.browser)
    ):
        prior_context = task.split("[Current Request]\n", 1)[0]
        if (
            detect_desktop_intent(prior_context)
            or detect_jarvis_intent(prior_context)
            or detect_browser_intent(prior_context)
        ):
            scope.desktop = True
            scope.jarvis = True
            scope.browser = True
    controller = ExecutionController(
        max_steps=settings.agent.max_steps,
        extension_budget=max(3, settings.agent.max_steps // 2),
    )
    loop_guard = LoopGuard(
        LoopGuardConfig(
            max_identical_calls=3,
            ping_pong_window=8,
        )
    )
    if inspection_tracker is None:
        inspection_tracker = InspectionTracker()
    if task_state is None:
        task_state = TaskState(
            original_request=task,
            workspace=str(getattr(hub, "workspace", "")),
            read_only=getattr(hub, "read_only", True),
        )

    # Recover any broken MCP servers from previous tasks
    if getattr(hub, "broken", None):
        await hub.heal()

    # Prepare tools
    all_tools = list(hub.tools.values())
    active_tools = select_active_tools(all_tools, scope)
    active_tool_names = {t.name for t in active_tools}
    ollama_tools = [mcp_tool_to_ollama(t, scope) for t in active_tools]

    # Build clear system prompt from centralized source
    active_provider = provider
    switched_to_qwen = False
    model_label = str(getattr(active_provider, "model", "") or "local-ollama")

    async def _try_switch_to_qwen(reason: Exception) -> bool:
        nonlocal active_provider, switched_to_qwen, model_label
        if (
            switched_to_qwen
            or os.environ.get("AURA_CLOUD_STRICT") == "1"
            or not is_cloud_model_spec(getattr(active_provider, "model", None))
        ):
            return False
        num_ctx = getattr(getattr(active_provider, "default_options", None), "num_ctx", None)
        fb = create_local_qwen_fallback(num_ctx=num_ctx)
        available = await asyncio.to_thread(fb.is_model_available)
        if not available and not os.environ.get("PYTEST_CURRENT_TEST"):
            if await asyncio.to_thread(ensure_ollama_running, 4.0):
                available = await asyncio.to_thread(fb.is_model_available)
        if not available:
            return False
        switched_to_qwen = True
        active_provider = fb
        model_label = str(getattr(fb, "model", "qwen3.5:4b"))
        if task_state is not None and fb not in task_state.used_providers:
            task_state.used_providers.append(fb)
        logger.warning("Cloud provider unavailable (%s); falling back to %s.", reason, model_label)
        await _emit_loop_event(
            on_event,
            event="stage",
            stage="fallback",
            model=model_label,
            message=f"Gemini limit/outage — switched to {model_label}...",
        )
        return True

    if scope.conversational:
        system_prompt = (
            JARVIS_CONVERSATIONAL_PROMPT
            + f"\nActive model: {model_label} running inside the AURA V4 Agent framework.\n"
        )
    elif (scope.jarvis or scope.desktop or scope.browser) and not (scope.git or scope.backend or scope.frontend):
        has_repo_coding_intent = any(
            w in scope.prompt_lower
            for w in ("repo", "repository", "codebase", "scan", "implement", "fix", "edit", "write", "refactor", "test", "code", "file")
        )
        base_prompt = f"{JARVIS_ASSISTANT_PROMPT}\n{CODING_RULES}" if has_repo_coding_intent else JARVIS_ASSISTANT_PROMPT
        system_prompt = (
            base_prompt
            + f"\nActive model: {model_label} running inside the AURA V4 Agent framework.\n"
        )
    else:
        system_prompt = (
            SYSTEM_PROMPT
            + f"\nActive model: {model_label} running inside the AURA V4 Agent framework.\n"
        )
    if scope.agent_role != "auto" and scope.agent_role in SPECIALIST_ROLE_PROMPTS and not scope.conversational:
        system_prompt += SPECIALIST_ROLE_PROMPTS[scope.agent_role] + "\n"
    if active_tool_names:
        system_prompt += "\nAvailable tools: " + ", ".join(sorted(active_tool_names)) + ".\n"

    ws = getattr(hub, "workspace", None)
    if not scope.conversational and isinstance(ws, (Path, str)):
        mode_str = "read-only" if getattr(hub, "read_only", False) else "read-write"
        system_prompt += f"\nWorkspace root: {ws} (Access mode: {mode_str})\n"
        if getattr(hub, "read_only", False):
            system_prompt += "NOTE: This workspace is in READ-ONLY mode. File modifications (write_file/edit_file) are blocked.\n"

    memory_context = memory.render()
    if memory_context:
        system_prompt += memory_context

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task}
    ]

    executed_calls: set[tuple[str, str]] = set()
    consecutive_duplicate_turns = 0
    consecutive_failed_turns = 0
    ro_blocked_turns = 0
    not_found_turns = 0
    has_prior_useful_results = False
    last_poll_digests: dict[str, str] = {}
    poll_stall_counts: dict[str, int] = {}
    last_partial_output = ""
    extra_steer_steps = 0
    empty_reply_count = 0
    diagnosis_nudge_count = 0
    control_flow_nudge_count = 0
    fallback_nudge_count = 0
    max_empty_replies = settings.agent.max_empty_reply_nudges

    async def _apply_steers(
        steers: list[str],
        candidate_answer: str = "",
        *,
        append_assistant: bool = False,
    ) -> None:
        nonlocal active_tools, active_tool_names, ollama_tools
        nonlocal consecutive_duplicate_turns, consecutive_failed_turns
        nonlocal empty_reply_count, diagnosis_nudge_count, control_flow_nudge_count
        nonlocal fallback_nudge_count, ro_blocked_turns, not_found_turns
        nonlocal last_partial_output, extra_steer_steps

        partial_text = (candidate_answer or last_partial_output).strip()
        if partial_text:
            last_partial_output = partial_text
            await _emit_loop_event(on_event, event="partial", text=partial_text)
        if append_assistant and candidate_answer.strip():
            messages.append({"role": "assistant", "content": candidate_answer.strip()})

        await _emit_loop_event(
            on_event,
            event="steer",
            stage=task_state.stage.value,
            instructions=list(steers),
            message=f"{task_state.stage.value}: steered -> {steers[-1][:120]}",
        )

        newly_unlocked_note = ""
        steer_joined = "\n".join(steers)
        if scope.agent_role == "auto" and scope.expand_for_steer(steer_joined):
            prev_names = set(active_tool_names)
            active_tools = select_active_tools(all_tools, scope)
            active_tool_names = {t.name for t in active_tools}
            ollama_tools = [mcp_tool_to_ollama(t, scope) for t in active_tools]
            added_names = sorted(active_tool_names - prev_names)
            if added_names:
                newly_unlocked_note = f"\n[Unlocked tools now available: {', '.join(added_names)}]"

        steer_body = "\n".join(f"- {s}" for s in steers)
        messages.append({
            "role": "user",
            "content": (
                "[USER STEER / REDIRECT — Keep useful partial findings already collected above, "
                f"and immediately follow this new instruction:]\n{steer_body}{newly_unlocked_note}"
            ),
        })

        consecutive_duplicate_turns = 0
        consecutive_failed_turns = 0
        ro_blocked_turns = 0
        not_found_turns = 0
        empty_reply_count = 0
        diagnosis_nudge_count = 0
        control_flow_nudge_count = 0
        fallback_nudge_count = 0
        last_poll_digests.clear()
        poll_stall_counts.clear()
        controller.consecutive_failures = 0
        controller.consecutive_repeats = 0
        if extra_steer_steps < 6:
            extra_steer_steps += 2
            controller.max_steps = max(controller.max_steps, controller.step + 2)

    # ── Deterministic Evidence Prefetch Fast Path ────────────────────────────
    if (
        not (scope.desktop or scope.jarvis or scope.browser)
        and is_prefetch_candidate(routing_task, scope)
        and "list_directory" in active_tool_names
        and "read_file" in active_tool_names
    ):
        try:
            evidence_items = await collect_prefetch_evidence(routing_task, scope, hub, cache, memory)
        except Exception as e:
            logger.warning("Prefetch collection failed: %s; falling back to agent loop.", e)
            evidence_items = []

        if evidence_items:
            for item in evidence_items:
                executed_calls.add((item.tool, json.dumps({"path": item.path}, sort_keys=True)))
                messages.append({
                    "role": "tool",
                    "name": item.tool,
                    "content": f"Path: {item.path}\n{item.content}",
                })

            if evidence_is_sufficient(evidence_items):
                logger.info(
                    "Prefetch evidence is sufficient (%d items); attempting single-synthesis.",
                    len(evidence_items)
                )
                evidence_text = "\n\n---\n\n".join(
                    f"[{item.tool}: {item.path}]\n{item.content}" for item in evidence_items
                )
                try:
                    synthesis_messages = [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"Question: {task}\n\n"
                                f"Evidence gathered from workspace:\n{evidence_text}\n\n"
                                "Provide a clear, complete, and grounded answer based on this evidence."
                            ),
                        },
                    ]
                    try:
                        synth_resp = await active_provider.chat(synthesis_messages, tools=None)
                    except Exception as exc:
                        if await _try_switch_to_qwen(exc):
                            synth_resp = await active_provider.chat(synthesis_messages, tools=None)
                        else:
                            raise
                    synth_answer = (synth_resp.message.content or "").strip()
                    if synth_answer and synth_answer != "(no answer)":
                        valid = True
                        if scope.diagnosis:
                            val_err = validate_diagnosis_answer(synth_answer, True)
                            if val_err:
                                valid = False
                        else:
                            flow_err = validate_control_flow(synth_answer, evidence_text)
                            if flow_err:
                                valid = False

                        if valid:
                            pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
                            if pending_steers:
                                await _apply_steers(pending_steers, synth_answer, append_assistant=True)
                            else:
                                return synth_answer
                except Exception as e:
                    logger.warning("Prefetch single-synthesis failed: %s; continuing in agent loop.", e)

    step = 0
    while step < settings.agent.max_steps + controller.extension_budget + extra_steer_steps:
        pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
        if pending_steers:
            await _apply_steers(pending_steers)
        if not controller.should_continue:
            break
        current_step = step
        step += 1

        # Call model with retry on transient errors only
        response = None
        messages_to_send = loop_guard.compress_context(
            prune_conversation_history(messages, settings)
        )
        use_native_tools = bool(active_tool_names)
        for attempt in range(settings.agent.model_call_attempts):
            try:
                response = await active_provider.chat(messages_to_send, tools=ollama_tools if use_native_tools else None)
                break
            except ConnectionError as e:
                if await _try_switch_to_qwen(e):
                    continue
                if is_cloud_model_spec(getattr(active_provider, "model", None)):
                    return f"Error: {e}"
                # Transient: network connection issue
                logger.warning("Model call attempt %d failed (ConnectionError): %s", attempt + 1, e)
                if attempt < settings.agent.model_call_attempts - 1:
                    await asyncio.sleep(settings.agent.model_retry_delay_seconds)
                else:
                    return f"Error: Failed to get model response after {settings.agent.model_call_attempts} attempts: {e}"
            except (TimeoutError, asyncio.TimeoutError) as e:
                if await _try_switch_to_qwen(e):
                    continue
                if is_cloud_model_spec(getattr(active_provider, "model", None)):
                    return f"Error: {e}"
                # Transient: operation timeout
                logger.warning("Model call attempt %d failed (TimeoutError): %s", attempt + 1, e)
                if attempt < settings.agent.model_call_attempts - 1:
                    await asyncio.sleep(settings.agent.model_retry_delay_seconds)
                else:
                    return f"Error: Failed to get model response after {settings.agent.model_call_attempts} attempts: {e}"
            except ValueError as e:
                if await _try_switch_to_qwen(e):
                    continue
                # Permanent: model not found, invalid config, etc.
                logger.error("Model call failed with permanent error: %s", e)
                return f"Error: {e}"
            except Exception as e:
                if await _try_switch_to_qwen(e):
                    continue
                err_low = str(e).lower()
                if use_native_tools and (
                    "xml syntax error" in err_low
                    or "element <function>" in err_low
                    or ("</parameter>" in err_low and ("xml" in err_low or "syntax" in err_low or "parse" in err_low))
                    or "does not support tools" in err_low
                ):
                    logger.warning("Ollama tool-template error (%s); retrying in text-tool mode.", e)
                    use_native_tools = False
                    if active_tools and not any(
                        str(m.get("content", "")).startswith("If calling a tool, output ONLY")
                        for m in messages_to_send
                    ):
                        sigs = "; ".join(
                            f"{t.name}({', '.join(((getattr(t, 'input_schema', None) or getattr(t, 'inputSchema', None) or {}).get('properties', {})).keys())})"
                            for t in active_tools[:24]
                        )
                        messages_to_send = [
                            *messages_to_send,
                            {
                                "role": "system",
                                "content": f"If calling a tool, output ONLY ```json {{\"name\": \"<tool>\", \"arguments\": {{...}}}}```. Tools: {sigs}",
                            },
                        ]
                    if attempt < settings.agent.model_call_attempts - 1:
                        continue
                    try:
                        response = await active_provider.chat(messages_to_send, tools=None)
                        break
                    except Exception as fb_exc:
                        e = fb_exc
                if is_cloud_model_spec(getattr(active_provider, "model", None)):
                    return f"Error: {e}"
                # Other errors - retry as potentially transient
                logger.warning("Model call attempt %d failed: %s", attempt + 1, e)
                if attempt < settings.agent.model_call_attempts - 1:
                    await asyncio.sleep(settings.agent.model_retry_delay_seconds)
                else:
                    return f"Error: Failed to get model response after {settings.agent.model_call_attempts} attempts: {e}"

        if response is None:
            return "Error: No response from model."

        msg = response.message
        content = msg.content or ""
        has_tool_calls = bool(msg.tool_calls and len(msg.tool_calls) > 0)
        tool_calls: list[tuple[str, dict]] = []
        raw_tc_meta_by_sig: dict[tuple[str, str], list[dict[str, Any]]] = {}

        if has_tool_calls:
            for tc in msg.tool_calls:
                func = getattr(tc, "function", None)
                if not func:
                    continue
                name = getattr(func, "name", "")
                args = getattr(func, "arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                elif not isinstance(args, dict):
                    args = {}
                if name:
                    tc_name = str(name)
                    tool_calls.append((tc_name, args))
                    norm_a = normalize_tool_arguments(tc_name, args if isinstance(args, dict) else {})
                    sig_key = (tc_name, json.dumps(norm_a, sort_keys=True))
                    tc_meta = _extract_tool_call_meta(tc)
                    if tc_meta:
                        raw_tc_meta_by_sig.setdefault(sig_key, []).append(tc_meta)
            tool_calls = _deduplicate_tool_calls(tool_calls)
        elif settings.agent.allow_text_tool_fallback:
            fallback_calls = _parse_text_tool_calls(content, active_tool_names)
            if fallback_calls:
                tool_calls = _deduplicate_tool_calls(fallback_calls)
                has_tool_calls = True

        # Append assistant message with clean serialization (preserving cloud id/thought_signature metadata)
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        call_id_by_sig: dict[tuple[str, str], str] = {}
        if has_tool_calls and tool_calls:
            serialized_calls: list[dict[str, Any]] = []
            for n, a in tool_calls:
                entry: dict[str, Any] = {"function": {"name": n, "arguments": a}}
                sig_key = (n, json.dumps(a, sort_keys=True))
                meta_queue = raw_tc_meta_by_sig.get(sig_key)
                if meta_queue:
                    tc_meta = meta_queue.pop(0)
                    entry.update(tc_meta)
                    if tc_meta.get("id"):
                        call_id_by_sig[sig_key] = str(tc_meta["id"])
                serialized_calls.append(entry)
            assistant_msg["tool_calls"] = serialized_calls
            if content.strip():
                last_partial_output = content.strip()
                await _emit_loop_event(on_event, event="partial", text=last_partial_output)
        elif has_tool_calls:
            assistant_msg["tool_calls"] = _serialize_tool_calls(msg.tool_calls)
        messages.append(assistant_msg)

        if has_tool_calls:
            if not tool_calls:
                messages.append({
                    "role": "user",
                    "content": (
                        "Your tool call could not be parsed. Please either "
                        "call a tool correctly or give your final answer."
                    )
                })
                continue

            # Plan and deduplicate against executed calls & InspectionTracker
            planned_calls: list[tuple[str, dict, tuple[str, str], bool, Any, str | None]] = []
            for name, args in tool_calls:
                norm_args = normalize_tool_arguments(name, args if isinstance(args, dict) else {})
                sig = (name, json.dumps(norm_args, sort_keys=True))
                is_repeat = sig in executed_calls
                already_inspected = (
                    _check_already_inspected_read(inspection_tracker, norm_args)
                    if (name == "read_file" and not is_repeat)
                    else None
                )
                verdict = loop_guard.check_call(
                    name,
                    norm_args,
                    is_poll=(name in _POLL_TOOLS),
                )
                planned_calls.append((name, norm_args, sig, is_repeat, verdict, already_inspected))

            is_duplicate_only = all(
                is_repeat or (already_inspected is not None)
                for _, _, _, is_repeat, _, already_inspected in planned_calls
            )
            if is_duplicate_only:
                consecutive_duplicate_turns += 1
            else:
                consecutive_duplicate_turns = 0

            # Execute pure read-only batches concurrently; keep mutating/blocked/tracked calls deterministic
            turn_has_path_error = False
            turn_has_exec_error = False
            turn_useful = False
            last_turn_error = ""
            turn_outcomes: list[str | None] = []
            terminal_texts: list[str] = []

            can_parallel = (
                len(planned_calls) > 1
                and all(
                    v.action != "block" and already is None and n in _PARALLEL_SAFE_TOOLS
                    for n, _, _, _, v, already in planned_calls
                )
            )

            executed_batch: list[tuple[str, dict, tuple[str, str], bool, Any, str | None, str, dict]] = []
            if can_parallel:
                for name, _, _, _, _, _ in planned_calls:
                    await _emit_loop_event(
                        on_event,
                        event="tool",
                        stage=task_state.stage.value,
                        tool=name,
                        status="running",
                        message=f"{task_state.stage.value}: running {name}...",
                    )
                raw_results = await asyncio.gather(
                    *(
                        execute_tool_call(
                            name, args, current_step,
                            active_tool_names=active_tool_names, hub=hub, cache=cache,
                            memory=memory, scope=scope,
                        )
                        for name, args, _, _, _, _ in planned_calls
                    )
                )
                for item, (res_text, meta) in zip(planned_calls, raw_results):
                    executed_batch.append((*item, res_text, meta))
            else:
                for item in planned_calls:
                    name, args, _, _, verdict, already = item
                    if verdict.action == "block":
                        executed_batch.append(
                            (*item, f"BLOCKED: {verdict.reason}", {"error": True, "blocked": True})
                        )
                        continue
                    if already is not None:
                        executed_batch.append(
                            (*item, already, {"error": False, "duplicate": True, "already_inspected": True})
                        )
                        continue
                    await _emit_loop_event(
                        on_event,
                        event="tool",
                        stage=task_state.stage.value,
                        tool=name,
                        status="running",
                        message=f"{task_state.stage.value}: running {name}...",
                    )
                    res_text, meta = await execute_tool_call(
                        name, args, current_step,
                        active_tool_names=active_tool_names, hub=hub, cache=cache,
                        memory=memory, scope=scope,
                    )
                    executed_batch.append((*item, res_text, meta))

            for name, args, sig, is_repeat, verdict, already, res_text, meta in executed_batch:
                if verdict.action == "block":
                    await _emit_loop_event(
                        on_event,
                        event="tool",
                        stage=task_state.stage.value,
                        tool=name,
                        status="error",
                        message=f"{task_state.stage.value}: {name} blocked (loop guard)",
                    )
                    executed_calls.add(sig)
                    tool_msg: dict[str, Any] = {
                        "role": "tool",
                        "name": name,
                        "content": res_text,
                    }
                    if sig in call_id_by_sig:
                        tool_msg["tool_call_id"] = call_id_by_sig[sig]
                    messages.append(tool_msg)
                    turn_has_exec_error = True
                    last_turn_error = res_text
                    turn_outcomes.append(None)
                    task_state.add_execution_error(f"{name}: {verdict.reason}")
                    continue

                if verdict.action == "warn" and not meta.get("error") and not meta.get("blocked"):
                    res_text = f"{res_text}\n[LOOP GUARD: {verdict.reason}]"

                stalled_poll = False
                if (
                    name in _POLL_TOOLS
                    and not meta.get("error")
                    and not meta.get("blocked")
                    and not meta.get("duplicate")
                ):
                    digest = hashlib.sha1(
                        re.sub(r"\s+", " ", res_text).strip().encode("utf-8", errors="ignore")
                    ).hexdigest()
                    if last_poll_digests.get(name) == digest:
                        poll_stall_counts[name] = poll_stall_counts.get(name, 0) + 1
                        stalled_poll = True
                        if poll_stall_counts[name] >= 2:
                            res_text += (
                                f"\n[STALL NOTICE: {name} returned identical output repeatedly. "
                                "Stop re-polling and act on the gathered evidence or answer now.]"
                            )
                    else:
                        last_poll_digests[name] = digest
                        poll_stall_counts[name] = 0

                await _emit_loop_event(
                    on_event,
                    event="tool",
                    stage=task_state.stage.value,
                    tool=name,
                    status="error" if meta.get("error") else "done",
                    cached=bool(meta.get("cached")),
                    message=f"{task_state.stage.value}: {name} {'failed' if meta.get('error') else 'done'}",
                )
                executed_calls.add(sig)
                tool_msg = {
                    "role": "tool",
                    "name": name,
                    "content": res_text,
                }
                if sig in call_id_by_sig:
                    tool_msg["tool_call_id"] = call_id_by_sig[sig]
                messages.append(tool_msg)

                outcome = _classify_terminal_tool_outcome(res_text)
                turn_outcomes.append(outcome)
                if outcome is not None:
                    terminal_texts.append(res_text)

                lowered_res = res_text.lower()
                is_missing_path = "does not exist" in lowered_res
                is_boundary_error = (
                    "outside the project workspace" in lowered_res
                    or "absolute paths are not allowed" in lowered_res
                    or "invalid path" in lowered_res
                )

                if is_missing_path or is_boundary_error:
                    turn_has_path_error = True
                    last_turn_error = res_text

                # Classify the outcome into inspection notice, execution error, or defect
                if is_tool_error_text(res_text) or meta.get("error") or meta.get("blocked"):
                    classified = classify_tool_result(name, res_text, args, meta)
                    if classified.category == ErrorCategory.INSPECTION_NOTICE:
                        task_state.add_inspection_notice(f"{name}: {classified.message}")
                        if not is_repeat and outcome is None:
                            turn_useful = True
                    elif classified.category == ErrorCategory.EXECUTION_ERROR:
                        turn_has_exec_error = True
                        task_state.add_execution_error(f"{name}: {classified.message}")
                    elif classified.category == ErrorCategory.TASK_DEFECT:
                        task_state.add_defect(f"{name}: {classified.message}")
                else:
                    if not meta.get("error") and not is_repeat and already is None and not stalled_poll and outcome is None:
                        turn_useful = True
                        has_prior_useful_results = True

                # Track file inspection ranges or invalidate on file mutation
                if name == "read_file" and not meta.get("error") and not is_missing_path and already is None:
                    _track_read_file(res_text, args.get("path", ""), inspection_tracker)
                elif name in {"write_file", "edit_file"} and not meta.get("error") and not meta.get("blocked"):
                    mut_path = (args.get("path", "") or "").replace("\\", "/").strip()
                    if mut_path:
                        inspection_tracker.files.pop(mut_path, None)

            # Update controller
            controller.tick(
                useful=turn_useful and not turn_has_exec_error,
                repeated=is_duplicate_only,
                failed=turn_has_exec_error and not turn_useful,
            )
            task_state.record_tool_call(
                useful=turn_useful, repeated=is_duplicate_only,
            )

            if turn_has_path_error:
                consecutive_failed_turns += 1
            else:
                consecutive_failed_turns = 0

            empty_reply_count = 0

            # If a user steer arrived while tools were executing, redirect immediately before any early exit
            pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
            if pending_steers:
                await _apply_steers(pending_steers)
                continue

            # Authoritative fast exits on clean blocking or terminal tool outcomes
            if turn_outcomes and all(o is not None for o in turn_outcomes):
                if all(o == "security_blocked" for o in turn_outcomes):
                    return terminal_texts[-1]
                if all(o == "ro_blocked" for o in turn_outcomes):
                    ro_blocked_turns += 1
                    is_compound_read_research = bool(
                        scope.jarvis
                        and any(w in scope.prompt_lower for w in ("read", "research", "explain", "summarize", "inside"))
                    )
                    if ro_blocked_turns >= 2 or not is_compound_read_research:
                        return terminal_texts[-1]
                if all(o == "not_found" for o in turn_outcomes):
                    not_found_turns += 1
                    is_compound_search = bool(scope.jarvis or scope.backend or scope.frontend)
                    if not_found_turns >= 2 or (not has_prior_useful_results and not is_compound_search):
                        return terminal_texts[-1]
            else:
                ro_blocked_turns = 0
                not_found_turns = 0

            # If tool calls fail repeatedly (e.g. non-existent files/directories),
            # stop early and provide an actionable explanation rather than exhausting max steps
            if consecutive_failed_turns >= 3:
                logger.info("Consecutive tool failures threshold reached (%d); terminating turn with clear explanation.", consecutive_failed_turns)
                ws_val = getattr(hub, "workspace", "current workspace")
                return (
                    f"Unable to find the requested paths after repeated attempts in workspace:\n"
                    f"Workspace: {ws_val}\n"
                    f"Last error: {last_turn_error or 'Path not found'}\n\n"
                    f"To inspect an external project or change directory, use the command: /workspace <path>"
                )

            # If the model is stuck repeating tool calls or identical OCR/snapshot polls,
            # break the loop by forcing a final answer synthesis without tools
            max_poll_stalls = max(poll_stall_counts.values(), default=0)
            if consecutive_duplicate_turns >= settings.agent.max_duplicate_only_turns or max_poll_stalls >= 2:
                logger.info("Duplicate/stall threshold reached (dups=%d, poll_stalls=%d); forcing synthesis.", consecutive_duplicate_turns, max_poll_stalls)
                try:
                    synthesis_resp = await active_provider.chat(messages, tools=None)
                    synth_content = (synthesis_resp.message.content or "").strip()
                    if synth_content:
                        pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
                        if pending_steers:
                            await _apply_steers(pending_steers, synth_content, append_assistant=True)
                            continue
                        return synth_content
                except Exception as e:
                    logger.warning("Forced synthesis on duplicate turn failed: %s", e)

        else:
            # Check if reply resembles a failed/malformed text tool call
            if _looks_like_tool_call(content) and fallback_nudge_count < settings.agent.max_fallback_parse_nudges:
                fallback_nudge_count += 1
                logger.info("Model reply resembles a tool call but could not be parsed; nudging with FALLBACK_NUDGE_MESSAGE.")
                messages.append({
                    "role": "user",
                    "content": FALLBACK_NUDGE_MESSAGE
                })
                continue

            # No tool calls — check if this is a final answer
            answer = content.strip()
            if answer and answer != "(no answer)":
                # Check diagnosis formatting if task is a diagnosis
                if scope.diagnosis and diagnosis_nudge_count < settings.agent.max_diagnosis_nudges:
                    val_err = validate_diagnosis_answer(answer, True)
                    if val_err:
                        diagnosis_nudge_count += 1
                        logger.info("Diagnosis validation notice (%s); nudging model.", val_err)
                        messages.append({
                            "role": "user",
                            "content": DIAGNOSIS_NUDGE_MESSAGE
                        })
                        continue

                # Check control flow semantics against evidence
                if control_flow_nudge_count < settings.agent.max_change_plan_nudges:
                    tool_texts = [m.get("content", "") for m in messages if m.get("role") == "tool"]
                    if tool_texts:
                        evidence_text = "\n".join(tool_texts)
                        flow_violations = validate_control_flow(answer, evidence_text)
                        if flow_violations:
                            control_flow_nudge_count += 1
                            logger.info("Control flow validation notice (%s); nudging model.", flow_violations[0])
                            messages.append({
                                "role": "user",
                                "content": CHANGE_PLAN_NUDGE_MESSAGE
                            })
                            continue

                pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
                if pending_steers:
                    await _apply_steers(pending_steers, answer, append_assistant=False)
                    continue
                return answer

            # Empty response handling with bounded nudging
            empty_reply_count += 1
            if empty_reply_count > max_empty_replies:
                if scope.conversational and not [m for m in messages if m.get("role") == "tool"]:
                    return f"Online and ready—currently running on {model_label} inside AURA V4. How can I help?"
                forced = await _force_answer_from_evidence(messages, active_provider, scope)
                pending_steers = await _drain_pending_steers(steer_queue, get_pending_steer, memory)
                if pending_steers:
                    await _apply_steers(pending_steers, forced, append_assistant=True)
                    continue
                return forced

            tool_results = [m for m in messages if m.get("role") == "tool"]
            if tool_results:
                messages.append({
                    "role": "user",
                    "content": (
                        "You have gathered tool results above. Now provide your "
                        "final answer based on the evidence. Do not call any more tools."
                    )
                })
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        "Please answer the question. If you need information from "
                        "the workspace, use the available tools. Otherwise, provide "
                        "your answer directly."
                    )
                })

    # Loop ended — either budget exhausted or controller stopped
    reason = controller.stop_reason or "budget_exhausted"
    logger.info("Agent loop ended: %s. %s", reason, controller.summary())

    if settings.agent.force_final_answer:
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if tool_results:
            logger.info("Forcing synthesis from gathered results.")
            forced_answer = await _force_answer_from_evidence(messages, active_provider, scope)
            if forced_answer and not forced_answer.startswith("I have no information"):
                return f"[{controller.summary()}]\n{forced_answer}"
    return f"Task incomplete ({reason}). {controller.summary()}"


async def _force_answer_from_evidence(
    messages: list, provider: OllamaProvider, scope: TaskScope
) -> str:
    """Generate answer from gathered tool results when model gives no response or steps expire."""
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        return "I have no information to answer this question."

    evidence = []
    for m in tool_results:
        tool_name = m.get("name", "tool")
        limit = 1800 if tool_name in {"run_tests", "get_file_diff", "git_diff"} else 1200
        content_preview = m.get("content", "")[:limit]
        evidence.append(f"[{tool_name}] {content_preview}")

    evidence_text = "\n---\n".join(evidence)

    # Get the original user query
    user_msgs = [m for m in messages if m.get("role") == "user"]
    original_query = user_msgs[0]["content"] if user_msgs else "the task"

    try:
        response = await provider.chat([
            {
                "role": "system",
                "content": (
                    "You are an expert software engineer assistant. "
                    "Synthesize a clear, concise final summary answering the user request based on "
                    "the actions taken and tool results."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User Request: {original_query}\n\n"
                    f"Actions taken and tool results from workspace:\n{evidence_text}\n\n"
                    "Provide a complete, concise summary of the findings, changes made, and test results."
                ),
            },
        ])
        answer = (response.message.content or "").strip()
        if answer:
            return answer
    except Exception as e:
        logger.warning("Force-answer synthesis failed: %s", e)

    return f"Based on tool results:\n{evidence_text}"


def _track_read_file(result_text: str, path: str, tracker: InspectionTracker) -> None:
    """Parse read_file output to track inspection ranges."""
    if not path or not result_text:
        return
    # Matches both "Showing lines X-Y of Z" and "[Lines X to Y of Z in ...]"
    m = re.search(
        r"(?:Showing lines (\d+)-(\d+) of (\d+)|\[Lines (\d+) to (\d+) of (\d+)\b)",
        result_text,
    )
    if m:
        s = m.group(1) or m.group(4)
        e = m.group(2) or m.group(5)
        t = m.group(3) or m.group(6)
        tracker.record(path, int(s), int(e), int(t))
    else:
        # Heuristic: count newlines as a rough line count
        lines = result_text.count("\n") + 1
        tracker.record(path, 1, lines)
