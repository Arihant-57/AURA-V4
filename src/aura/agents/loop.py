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
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..core import (
    McpHub, SessionMemory, ResultCache, TaskScope,
    select_active_tools, check_path_policy, sanitize_tool_result,
    mcp_tool_to_ollama, mcp_result_to_text, validate_arguments,
    normalize_tool_arguments, validate_diagnosis_answer, validate_control_flow,
    is_prefetch_candidate, collect_prefetch_evidence, evidence_is_sufficient
)
from ..core.providers import OllamaProvider
from ..core.prompts import (
    SYSTEM_PROMPT, DIAGNOSIS_NUDGE_MESSAGE, CHANGE_PLAN_NUDGE_MESSAGE,
    FALLBACK_NUDGE_MESSAGE
)

logger = logging.getLogger(__name__)


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
    except Exception as e:
        text = f"MCP TOOL ERROR: {e}"
        meta["error"] = True

    meta["duration"] = time.perf_counter() - start

    # 6. Sanitize and Cache (only cache successful operations, never cache mutating operations)
    sanitized = sanitize_tool_result(name, text, scope.prompt_lower, scope)
    if not meta["error"]:
        if name in {"write_file", "edit_file", "run_tests"}:
            # Never cache mutating or test operations; invalidate any cached reads
            if name in {"write_file", "edit_file"}:
                cache.invalidate_path(arguments.get("path", ""))
                memory.note_modified(arguments.get("path", ""))
        else:
            cache.put(signature, sanitized)
    if name == "read_file":
        memory.note_file(arguments.get("path", ""))
    elif name == "list_directory":
        memory.note_dir(arguments.get("path", "."))

    return sanitized, meta


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
        serialized.append({
            "function": {"name": str(name), "arguments": args}
        })
    return serialized


def _deduplicate_tool_calls(tool_calls: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Remove duplicate tool calls within a single turn.

    Small models sometimes emit the same call multiple times in one turn.
    Keep only the first occurrence of each (name, args) pair.
    """
    seen: set[tuple[str, str]] = set()
    unique = []
    for name, args in tool_calls:
        key = (name, json.dumps(args, sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique.append((name, args))
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
            pass

    if calls:
        return calls

    # 3. Direct JSON object with "name" and "arguments"
    stripped = content.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or ('"name":' in stripped and '"arguments":' in stripped):
        try:
            start_idx = stripped.find("{")
            end_idx = stripped.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                candidate = stripped[start_idx:end_idx + 1]
                data = json.loads(candidate)
                name = data.get("name") or data.get("tool")
                args = data.get("arguments") or data.get("parameters") or {}
                if name in active_tool_names and isinstance(args, dict):
                    calls.append((str(name), args))
        except Exception:
            pass

    return calls


def _looks_like_tool_call(content: str) -> bool:
    """Check if content looks like a malformed or text-based tool call."""
    lowered = content.strip().lower()
    return (
        "<tool_call>" in lowered
        or ("```json" in lowered and ("\"tool\"" in lowered or "\"name\"" in lowered or "\"arguments\"" in lowered))
        or (lowered.startswith("{") and ("\"name\"" in lowered or "\"tool\"" in lowered) and "\"arguments\"" in lowered)
    )


def prune_conversation_history(messages: list[dict], settings: Any) -> list[dict]:
    """Prune and compact older tool results to stay well within context budget.

    Preserves all message roles and sequence (required by tool-calling API),
    while shortening older bulky tool results when the history exceeds
    the configured token threshold or tool result count limit.
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

    # Keep the most recent `keep_recent` tool messages intact; shorten older ones
    older_indices = set(tool_indices[:-keep_recent])

    pruned = []
    for idx, msg in enumerate(messages):
        if idx in older_indices and msg.get("role") == "tool":
            content = msg.get("content") or ""
            if len(content) > shortened_limit:
                new_msg = dict(msg)
                new_msg["content"] = (
                    content[:shortened_limit]
                    + f"\n\n[PRUNED: older tool result shortened ({len(content)} chars -> {shortened_limit})]"
                )
                pruned.append(new_msg)
                continue
        pruned.append(msg)

    return pruned


async def run_agent_loop(
    task: str,
    provider: OllamaProvider,
    hub: McpHub,
    memory: SessionMemory,
    cache: ResultCache
) -> str:
    """The main reasoning loop."""
    settings = get_settings()
    scope = TaskScope(task)

    # Recover any broken MCP servers from previous tasks
    if getattr(hub, "broken", None):
        await hub.heal()

    # Prepare tools
    all_tools = list(hub.tools.values())
    active_tools = select_active_tools(all_tools, scope)
    active_tool_names = {t.name for t in active_tools}
    ollama_tools = [mcp_tool_to_ollama(t, scope) for t in active_tools]

    # Build clear system prompt from centralized source
    system_prompt = SYSTEM_PROMPT
    if active_tool_names:
        system_prompt += "\nAvailable tools: " + ", ".join(sorted(active_tool_names)) + ".\n"

    ws = getattr(hub, "workspace", None)
    if isinstance(ws, (Path, str)):
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
    empty_reply_count = 0
    diagnosis_nudge_count = 0
    control_flow_nudge_count = 0
    fallback_nudge_count = 0
    max_empty_replies = settings.agent.max_empty_reply_nudges

    # ── Deterministic Evidence Prefetch Fast Path ────────────────────────────
    if (
        is_prefetch_candidate(task, scope)
        and "list_directory" in active_tool_names
        and "read_file" in active_tool_names
    ):
        try:
            evidence_items = await collect_prefetch_evidence(task, scope, hub, cache, memory)
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
                    synth_resp = await provider.chat(synthesis_messages, tools=None)
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
                            return synth_answer
                except Exception as e:
                    logger.warning("Prefetch single-synthesis failed: %s; continuing in agent loop.", e)

    for step in range(settings.agent.max_steps):
        # Call model with retry on transient errors only
        response = None
        messages_to_send = prune_conversation_history(messages, settings)
        for attempt in range(settings.agent.model_call_attempts):
            try:
                response = await provider.chat(messages_to_send, tools=ollama_tools if active_tool_names else None)
                break
            except ConnectionError as e:
                # Transient: network connection issue
                logger.warning("Model call attempt %d failed (ConnectionError): %s", attempt + 1, e)
                if attempt < settings.agent.model_call_attempts - 1:
                    await asyncio.sleep(settings.agent.model_retry_delay_seconds)
                else:
                    return f"Error: Failed to get model response after {settings.agent.model_call_attempts} attempts: {e}"
            except asyncio.TimeoutError as e:
                # Transient: operation timeout
                logger.warning("Model call attempt %d failed (TimeoutError): %s", attempt + 1, e)
                if attempt < settings.agent.model_call_attempts - 1:
                    await asyncio.sleep(settings.agent.model_retry_delay_seconds)
                else:
                    return f"Error: Failed to get model response after {settings.agent.model_call_attempts} attempts: {e}"
            except ValueError as e:
                # Permanent: model not found, invalid config, etc.
                logger.error("Model call failed with permanent error: %s", e)
                return f"Error: {e}"
            except Exception as e:
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
                    tool_calls.append((str(name), args))
            tool_calls = _deduplicate_tool_calls(tool_calls)
        elif settings.agent.allow_text_tool_fallback:
            fallback_calls = _parse_text_tool_calls(content, active_tool_names)
            if fallback_calls:
                tool_calls = _deduplicate_tool_calls(fallback_calls)
                has_tool_calls = True

        # Append assistant message with clean serialization
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if has_tool_calls and tool_calls:
            assistant_msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in tool_calls]
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

            # Check if this turn is entirely duplicate calls that were already executed
            turn_signatures = [(name, json.dumps(args, sort_keys=True)) for name, args in tool_calls]
            is_duplicate_only = all(sig in executed_calls for sig in turn_signatures)

            if is_duplicate_only:
                consecutive_duplicate_turns += 1
            else:
                consecutive_duplicate_turns = 0

            # Execute tool calls sequentially
            turn_has_path_error = False
            last_turn_error = ""
            for name, args in tool_calls:
                sig = (name, json.dumps(args, sort_keys=True))
                res_text, meta = await execute_tool_call(
                    name, args, step,
                    active_tool_names=active_tool_names, hub=hub, cache=cache,
                    memory=memory, scope=scope
                )
                executed_calls.add(sig)
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": res_text,
                })
                lowered_res = res_text.lower()
                if "does not exist" in lowered_res or "outside the project workspace" in lowered_res:
                    turn_has_path_error = True
                    last_turn_error = res_text

            if turn_has_path_error:
                consecutive_failed_turns += 1
            else:
                consecutive_failed_turns = 0

            empty_reply_count = 0

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

            # If the model is stuck repeating tool calls it already has answers for,
            # break the loop by forcing a final answer synthesis without tools
            if consecutive_duplicate_turns >= settings.agent.max_duplicate_only_turns:
                logger.info("Duplicate tool calls threshold reached (%d); forcing synthesis.", consecutive_duplicate_turns)
                try:
                    synthesis_resp = await provider.chat(messages, tools=None)
                    synth_content = (synthesis_resp.message.content or "").strip()
                    if synth_content:
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

                return answer

            # Empty response handling with bounded nudging
            empty_reply_count += 1
            if empty_reply_count > max_empty_replies:
                return await _force_answer_from_evidence(messages, provider, scope)

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

    # If all max_steps are exhausted without returning an answer
    if settings.agent.force_final_answer:
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if tool_results:
            logger.info("Max steps reached (%d); forcing synthesis from gathered results.", settings.agent.max_steps)
            forced_answer = await _force_answer_from_evidence(messages, provider, scope)
            if forced_answer and not forced_answer.startswith("I have no information"):
                return f"Maximum steps reached. Summary of actions and results:\n{forced_answer}"
    return "Maximum steps reached without final answer."


async def _force_answer_from_evidence(
    messages: list, provider: OllamaProvider, scope: TaskScope
) -> str:
    """Generate answer from gathered tool results when model gives no response or steps expire."""
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        return "I have no information to answer this question."

    evidence = []
    for m in tool_results:
        content_preview = m.get("content", "")[:1000]
        tool_name = m.get("name", "tool")
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
