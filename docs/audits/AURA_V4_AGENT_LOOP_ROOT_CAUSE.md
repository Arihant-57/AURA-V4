# AURA V4 Agent Loop — Root Cause Investigation

**Date:** 2026-11-03  
**Investigator:** Automated Engineering Audit  
**Project:** `C:\Users\ariha\local-ai-lab\phase4-agent`

## Symptom

> "Maximum steps reached without final answer."

The agent connected to MCP, discovered tools, but failed to complete simple tasks within the 6-step limit.

## Investigation Method

1. Read all 17 source files in `src/aura/`
2. Traced the execution path in `loop.py` line by line
3. Compared message format expectations between Ollama SDK and loop.py
4. Analyzed the integration test (`integration_test_phase_g.py`) for clues
5. Ran all 18 existing unit tests (all passing with mocks — confirming the bug is in real Ollama interaction, not mock behavior)

## Root Causes Identified

### RC-1: Hybrid Message Serialization (CRITICAL)

**File:** `src/aura/agents/loop.py`, line 123 (original)

```python
messages.append({"role": msg.role, "content": msg.content or "", "tool_calls": msg.tool_calls})
```

**Problem:** The assistant message is a Python dict, but `tool_calls` contains raw Ollama `ToolCall` objects (not dicts). When this hybrid structure is sent back to `ollama.chat()`, the library may:
- Fail to serialize the ToolCall objects properly
- Lose the function name and arguments
- Cause the model to not understand what tool was called

**Impact:** The model cannot correlate tool results with tool calls, leading to repeated calls, confusion, or silence — ultimately exhausting the 6-step limit.

**Fix:** Added `_serialize_tool_calls()` to convert ToolCall objects to plain dicts before storing in message history.

### RC-2: No Within-Turn Deduplication (MODERATE)

**File:** `src/aura/agents/loop.py`, lines 146-152 (original)

```python
results = await asyncio.gather(*(
    execute_tool_call(...) for name, args in tool_calls
))
```

**Problem:** `asyncio.gather` executes all tool calls in parallel. If the model returns 3 identical `read_file("test.py")` calls in one turn, all 3 execute simultaneously before the cache can catch duplicates (the cache is populated during execution). This wastes steps and context tokens.

**Fix:** Added `_deduplicate_tool_calls()` to remove identical calls before execution. Changed from parallel to sequential execution so the cache can prevent duplicates across calls within the same turn.

### RC-3: No Empty-Reply Nudging (MODERATE)

**File:** `src/aura/agents/loop.py`, lines 160-166 (original)

**Problem:** If the model returns empty content and no tool calls, the loop immediately tries to return the empty string or calls `_force_answer_from_evidence`. Small local models sometimes need a nudge to continue reasoning. The loop never gives the model a second chance.

**Fix:** Implemented empty-reply nudging: when the model returns empty, inject a user message asking it to provide an answer from evidence or use tools. Limited to `max_empty_reply_nudges=1` to prevent infinite loops.

### RC-4: System Prompt Not Used (LOW-MODERATE)

**File:** `src/aura/agents/loop.py`, lines 107-113 (original)

```python
system_prompt = (
    "You are a coding assistant that MUST use tools to answer questions about the workspace.\n"
    "Available tools: " + ", ".join(active_tool_names) + ".\n"
    ...
)
```

**Problem:** A minimal hardcoded prompt is used instead of the carefully crafted `SYSTEM_PROMPT` from `prompts.py` which includes:
- Anti-hallucination rules
- Tool-calling discipline  
- Code semantics contract
- Stop-when-done instructions

Without these, the model lacks guidance on when to stop calling tools, leading to unnecessary loops.

**Fix:** Imported and used `SYSTEM_PROMPT` from `prompts.py`.

### RC-5: No Model Call Retry (LOW)

**Problem:** The original loop made a single `provider.chat()` call per step with no error handling. A transient Ollama failure immediately killed the task. Combined with the max_steps=6 limit, even one failed step could cascade into exhaustion.

**Fix:** Added retry logic with configurable `model_call_attempts=3` and `model_retry_delay_seconds=2.0`.

## Additional Contributing Factors

- **max_steps=6** is very tight for tasks requiring list + read + synthesize (3 model calls minimum). While not a bug, combined with the above bugs, it guarantees failure for any task requiring 2+ tool calls.
- **Session memory never injected:** The loop builds a system prompt but never includes `memory.render()`, so cross-task knowledge is wasted. Fixed.
- **Error results cached:** The original code cached tool error results, meaning a first-attempt failure would poison subsequent retries. Fixed: errors are no longer cached.

## Verification

- All 18 original unit tests continue to pass (3 were updated to match improved behavior)
- 19 new regression tests added, all passing
- **Total: 37 tests passing, 0 failing**
