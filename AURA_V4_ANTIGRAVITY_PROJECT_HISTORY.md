# AURA V4 — Antigravity Independent Project History, Technical Audit & Roadmap

**Author:** Antigravity (AI Pair Programmer & Coding Specialist)  
**Project Workspace:** `C:\Users\ariha\local-ai-lab\phase4-agent`  
**Current Branch:** `feature/multi-model-routing`  
**Document Creation Date:** 2026-11-09 00:55 IST  

---

## A. Project Goal

AURA V4 is designed to be an autonomous, personal, local-first AI engineering assistant capable of end-to-end software engineering workflows:
1. **Intelligent Intake & Classification:** Accurately understand developer prompts and determine whether a task requires a direct response, read-only file exploration, lightweight research, coding implementation, or a multi-stage project workflow.
2. **Repository-Level Understanding:** Inspect codebases, trace dependencies, read definitions and configurations, analyze architectures, and diagnose bugs without hallucinating source code or external dependencies.
3. **Authorized Local Execution:** Perform file operations, edits, creations, and command execution strictly within authorized workspace boundaries. Enforce read-only (`ro`) and read-write (`rw`) security policies.
4. **Autonomous Multi-Stage Pipeline:** Coordinate multi-model execution through structured stages (**Analysis → Coding → Verification → Bounded Correction**), ensuring that code modifications are validated against automated tests before completion.
5. **Local-First Model Execution:** Run primarily on local Ollama models tailored to available hardware (specifically calibrating for 6GB VRAM on RTX 3050 Laptop GPU) with zero requirement for paid cloud APIs.
6. **Honest, Evidence-Grounded Reporting:** Clearly distinguish between verified facts, test outcomes, inspection notices, execution errors, and task defects. Never claim a test passed or a feature works without terminal verification output.

---

## B. Original Architecture (Baseline Before Improvements)

When this phase of work began, the baseline codebase (`ba56a28` / `d8b2e4b`) consisted of:
- **`src/aura/agents/loop.py`:** A single-model, multi-turn reasoning loop executing up to a fixed `max_steps` (10 turns). It used naive string matching (`"does not exist" in lowered_res`) for path errors and forced an evidence synthesis when max steps were reached.
- **`src/aura/agents/aura_v4.py`:** An interactive CLI entrypoint supporting `/workspace`, `/mode`, `/status`, and direct positional task queries. It instantiated a single `OllamaProvider` locked to `--model` (or default `qwen3.5:4b`) and reported tokens only from that initial provider.
- **`src/aura/core/mcp_hub.py`:** Connection manager for two Model Context Protocol (MCP) stdio servers:
  - `filesystem_server.py`: Providing `read_file`, `list_directory`, `search_files`, `write_file`, `edit_file`, `get_file_diff`, and `run_tests`.
  - `git_server.py`: Providing `git_status`, `git_diff`, `git_log`, `git_commit`, `git_branch`.
- **`src/aura/core/tools.py` & `scope.py`:** Task intent classification (backend, frontend, git, conversational) and path access policy guardrails.
- **`src/aura/config/settings.py`:** Configuration dataclasses for model defaults, prefetch, cache, and MCP timeouts (`30s`).
- **Limitation of Baseline:** The system had no dynamic model registry, no capability to switch between specialist models for analysis versus coding, no progress-aware loop controller (it blindly counted 10 turns), and no structured separation between exploratory missing-file notices and true execution defects.

---

## C. Changes Implemented

The following components were designed, implemented, and verified in the repository:

| File | Status | Nature of Change & Technical Behavior |
| :--- | :--- | :--- |
| [`src/aura/core/model_registry.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/model_registry.py) | **Implemented** (Untracked) | **Dynamic Model Discovery & Role Prioritization:** Discovers installed models via `http://127.0.0.1:11434/api/tags`, extracts parameter size, context length, and tool-calling support. Defines default role maps (`LIGHTWEIGHT`, `ANALYSIS`, `CODING`, `VERIFICATION`). Calibrated to prioritize `qwen3.5:4b` (3.4GB) for analysis/verification and `qwen2.5-coder:7b` (4.7GB) for coding to fit 100% in 6GB VRAM. |
| [`src/aura/core/model_router.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/model_router.py) | **Implemented** (Untracked) | **Task Classification & TaskState Handoff:** Implements `classify_task_role` using regex word-boundary matching (`\b[a-z0-9_]+\b`). Implements `needs_multi_stage` ensuring commands under 8 words remain single-pass while complex requests trigger multi-stage. Implements `TaskState` capturing findings, proposed changes, inspection notices, execution errors, defects, and provider metrics for stage handoffs. |
| [`src/aura/core/orchestrator.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/orchestrator.py) | **Implemented** (Untracked) | **Multi-Stage Orchestration & Bounded Correction:** Coordinates Stage 1 (Analysis with `qwen3.5:4b`), Stage 2 (Coding with `qwen2.5-coder:7b`), and Stage 3 (Verification with `qwen3.5:4b`). Skips coding in read-only mode. Executes bounded correction loop (`MAX_CORRECTION_CYCLES = 2`) on `VERDICT: FAIL`. Implements `_build_final_multi_stage_report` partitioning notices, errors, and defects. |
| [`src/aura/core/execution_controller.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/execution_controller.py) | **Implemented** (Untracked) | **Progress-Aware Loop Control:** Replaces blind turn counter with `tick(useful, repeated, failed)`. Extends turn budget by up to 5 steps if progress ratio exceeds 30%; triggers early termination on 3 consecutive duplicate turns or 3 consecutive failures. |
| [`src/aura/core/file_tracker.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/file_tracker.py) | **Implemented** (Untracked) | **File Inspection Coverage Tracking:** Merges overlapping line ranges read by `read_file` to calculate percentage of file inspected across stages. |
| [`src/aura/core/error_classifier.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/core/error_classifier.py) | **Implemented** (Untracked) | **Structured Error Categorization:** Defines `ErrorCategory` (`INSPECTION_NOTICE`, `EXECUTION_ERROR`, `TASK_DEFECT`). Classifies tool responses so missing candidate files during read/list are recorded as notices rather than defects or turn failures. |
| [`src/aura/agents/loop.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/agents/loop.py) | **Modified** (Tracked) | Integrated `ExecutionController`, `InspectionTracker`, and `classify_tool_result`. Ensured missing path checks do not mark turns as failed unless they represent genuine boundary escapes. |
| [`src/aura/agents/aura_v4.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/src/aura/agents/aura_v4.py) | **Modified** (Tracked) | Added dynamic routing via `_run_task`, `/models` CLI command, ASCII console code-page formatting (replaced Unicode `→` with `->`), and `format_task_metrics` aggregating multi-provider token usage. |
| [`.gitignore`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/.gitignore) | **Modified** (Tracked) | Added `*.py[cod]` exclusion to prevent tracked `.pyc` modifications. |
| [`tests/unit/test_error_classification.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/tests/unit/test_error_classification.py) | **Implemented** (Untracked) | 11 unit tests covering missing files, permissions, boundary violations, MCP failures, I/O errors, and report partitioning. |
| [`tests/unit/test_orchestrator.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/tests/unit/test_orchestrator.py) | **Implemented** (Untracked) | 37 tests covering parsing helpers, handoffs, correction cycles, report generation, punctuation-insensitive classification, and token metrics. |
| [`tests/unit/test_multi_model.py`](file:///C:/Users/ariha/local-ai-lab/phase4-agent/tests/unit/test_multi_model.py) | **Implemented** (Untracked) | 44 unit tests covering `ModelCapability`, `ModelRegistry`, `ModelRouter`, `ExecutionController`, `FileInspection`, and `TaskState`. |

---

## D. Work Completed vs Reported vs Unverified

### 1. Fully Implemented & Independently Verified
- **Multi-Model Role Assignment:** `qwen3.5:4b` for triage/analysis/verification; `qwen2.5-coder:7b` for code generation.
- **Context Handoff Mechanism:** Structured markdown transfer (`FINDINGS:`, `FILES:`, `CHANGES:`, `IMPLEMENTED:`, `DEFECTS:`) across stages.
- **Error Classification Pipeline:** Missing files during read operations are categorized as `INSPECTION_NOTICE` and isolated from defects.
- **Git Bytecode Cleanliness:** Untracked 30 `.pyc` files from Git index; running tests leaves working tree clean of bytecode diffs.
- **Direct-Task Operations:** 6/6 Phase 2 scenarios verified live (simple QA, search, read/summarize, missing file notice, file creation, file verification).

### 2. Verified in Sandbox Execution
- **Project Workflow (Phase 3):** Executed full analysis, coding, and verification in `test_sandbox_p3`. Function `subtract` was implemented, tested, and verified with all tests passing.
- **Single-Pass Code Generation (Phase 4):** Model `qwen2.5-coder:7b` wrote a complete `compute_tax` function in `test_sandbox_p4` that passed all 3 unit tests (`3 passed in 0.02s`).

### 3. Reported But NOT Yet Live-Verified
- **Live Automatic Recovery Loop:** The bounded correction loop (`run_multi_stage` lines 181–217) is proven via mocked unit tests (`test_orchestrator.py`), but **has never completed live in real Ollama execution**. In all live tests, the coding model passed verification on the first attempt, meaning a genuine failure never triggered the correction cycle live.
- **Recovery Under Failure:** How Ollama models behave when receiving actual pytest tracebacks during live correction remains unverified against live models.

### 4. Known Incomplete / Stalled
- **Model Call Timeout:** No timeout exists on `provider.chat()`. When a socket drops or stalls at the HTTP/TCP layer, the test process hangs indefinitely.

---

## E. Test History & Actual Test Evidence

All automated test runs were executed against the actual workspace and verified via exit code and terminal output:

| Test Run | Command | Total | Pass | Fail | Skip | Exit Code | Duration | Nature |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Initial Suite** | `python -m pytest tests/` | 239 | 239 | 0 | 0 | 0 | 19.82s | Mocked & Unit |
| **Multi-Model Suite** | `python -m pytest tests/` | 250 | 250 | 0 | 0 | 0 | 20.75s | Mocked & Unit |
| **Post-Version Run** | `python -m pytest tests/` | 251 | 251 | 0 | 0 | 0 | 20.87s | Mocked & Unit |
| **Error-Classification** | `python -m pytest tests/unit/test_error_classification.py` | 11 | 11 | 0 | 0 | 0 | 1.25s | Unit & Integration |
| **Full Regression Suite** | `python -m pytest tests/ -v` | 262 | 262 | 0 | 0 | 0 | 22.87s | Complete Suite |
| **Re-Verification Suite** | `python -m pytest tests/ -q` | 262 | 262 | 0 | 0 | 0 | 24.08s | Complete Suite |
| **Phase 2 Direct Tasks** | `python scratch/test_phase2_direct_tasks.py` | 6 | 6 | 0 | 0 | 0 | 38.10s | **100% Live Ollama Models** |

---

## F. Latest Live Test Audit (Phase 2, Phase 3, Phase 4)

Executed script: `scratch/test_phase3_and_phase4.py` (Background Task `task-3202`, Process PID `31308`).

### 1. Phase 2 (Direct Tasks)
- **Result:** **PASSED (6/6).**
- **Evidence:** `scratch/phase2_results.json`. Handled simple questions without coding model (`qwen3.5:4b`, 1 tool call); performed file search; read and summarized `pytest.ini`; handled missing file as an inspection notice without errors; created `test_tmp_phase2/hello.txt` via multi-stage; verified file content.

### 2. Phase 3 (Project Workflow)
- **Result:** **PASSED.**
- **Evidence:** Executed inside `test_sandbox_p3`. Analysis model identified requirements in `math_util.py` and `test_math.py`. Coding model implemented `subtract`. Verification model confirmed tests passed. Sandbox cleaned up at 00:10:50.

### 3. Phase 4 (Live Failure & Recovery)
- **Result:** **STALLED / UNRESOLVED.**
- **What Executed:**
  - `test_sandbox_p4` created with stub `math_service.py` and strict test suite `test_math.py` (requiring 15% tax, 2-decimal rounding, and `ValueError` on negative amounts).
  - Stage 1 (Analysis with `qwen3.5:4b`): Discovered all 3 test requirements.
  - Stage 2 (Coding with `qwen2.5-coder:7b`): At 00:13:08, wrote complete code:
    ```python
    def compute_tax(amount: float) -> float:
        if amount < 0:
            raise ValueError('Amount cannot be negative')
        return round(amount * 0.15, 2)
    ```
  - Stage 3 (Verification with `qwen3.5:4b`): At 00:13:10, called `run_tests()`. Pytest compiled bytecode and returned `3 passed in 0.02s`.
- **Where It Hung:**
  - At 00:13:11, after receiving tool results, the agent loop invoked `provider.chat()` to request the final synthesis verdict.
  - The TCP socket (`127.0.0.1:63031 -> 127.0.0.1:11434`) remained in state `ESTABLISHED`.
  - Ollama unloaded all models (`/api/ps` returned `{"models": []}`), but the socket was not closed with an EOF.
  - Because `provider.chat()` has **no timeout**, PID `31308` remained suspended in `socket.recv()` at 0% CPU for over 20 minutes until cancelled.
- **Recovery Status:** **Recovery never started** because the coding model passed all tests on its initial attempt. No `VERDICT: FAIL` was generated.

---

## G. Problems Discovered & Current Status

| Issue | Root Cause | Impact | Status |
| :--- | :--- | :--- | :--- |
| **1. Missing Files Treated as Defects** | `loop.py` naively checked `"does not exist" in lowered_res` and called `task_state.add_error(...)`. | Normal exploratory reads appeared under `### Defects / Errors` in successful task reports. | **RESOLVED:** Implemented `error_classifier.py` and partitioned reports into notices vs defects. |
| **2. Bytecode Tracked by Git** | 30 `.pyc` files were tracked in Git index in early commits. | Running pytest caused continuous modified `.pyc` diffs in `git status`. | **RESOLVED:** Added `*.py[cod]` to `.gitignore` and ran `git rm --cached` on tracked `.pyc` files. |
| **3. Windows Console Code Page Crashes** | Console code page `cp1252` failed on Unicode right arrow `→`. | Threw `UnicodeEncodeError` in terminal output. | **RESOLVED:** Replaced all console arrows with standard ASCII `->`. |
| **4. Unbounded Model Call Inference** | `provider.chat()` invokes `asyncio.to_thread(chat, **kwargs)` without `asyncio.wait_for` or HTTP socket timeout. `settings.py` has no `model_call_timeout_seconds`. | Stalled Ollama streams or dropped HTTP connections cause AURA to hang indefinitely. | **IDENTIFIED — REQUIRES FIX (Priority 1).** |
| **5. Buffered Test Output** | Test runner ran without Python unbuffered mode (`-u`). | Output logs remained 0 bytes while running, preventing real-time monitoring. | **IDENTIFIED — REQUIRES FIX (Priority 1).** |
| **6. Live Recovery Unverified** | Tasks given to the coding model were solved correctly on the first pass, bypassing correction cycles. | Live multi-model failure handoff and correction loop remain unverified in production. | **IDENTIFIED — REQUIRES DETERMINISTIC TEST (Priority 3).** |

---

## H. Current Git State

Inspected in `C:\Users\ariha\local-ai-lab\phase4-agent`:
- **Current Branch:** `feature/multi-model-routing`
- **Tracked Modifications (unstaged):**
  - `.gitignore` (added `*.py[cod]`)
  - `src/aura/agents/aura_v4.py` (routing, metrics formatting, ASCII printing)
  - `src/aura/agents/loop.py` (controller, file tracker, error classification)
  - `src/aura/core/__init__.py` (exports for new modules)
  - `tests/unit/test_agent_loop.py` (assertion updates for controller summaries)
- **Staged for Deletion (from index only):**
  - 30 `.pyc` bytecode files across `src/` and `tests/`
- **Untracked Additions:**
  - `src/aura/core/error_classifier.py`
  - `src/aura/core/execution_controller.py`
  - `src/aura/core/file_tracker.py`
  - `src/aura/core/model_registry.py`
  - `src/aura/core/model_router.py`
  - `src/aura/core/orchestrator.py`
  - `src/aura/version.py` (created during live test, retained)
  - `test_sandbox_p4/` (disposable sandbox from Phase 4)
  - `tests/unit/test_error_classification.py`
  - `tests/unit/test_multi_model.py`
  - `tests/unit/test_orchestrator.py`
- **Safety Compliance:** No commits were created; no branches were merged; working tree changes remain intact.

---

## I. Current Project Plan (Antigravity's Recommended Execution Order)

1. **Current Priority:** **Reliability Foundation (Timeout & Clean Process Lifecycle).**
   - *Why:* Until model inference calls have a strict timeout (`model_call_timeout_seconds`), any subsequent live test can hang indefinitely on Ollama socket drops. We cannot reliably test live recovery without bulletproof timeouts.
2. **What Must Be Completed First:**
   - Remove leftover disposable directory `test_sandbox_p4/`.
   - Add `model_call_timeout_seconds: int = 120` to `ModelSettings` in `src/aura/config/settings.py`.
   - Wrap `await provider.chat(...)` in `src/aura/agents/loop.py` with `asyncio.wait_for(..., timeout=timeout)` and catch `asyncio.TimeoutError` cleanly.
   - Run tests with unbuffered output (`python -u`) and log flushing.
3. **What Should NOT Be Implemented Yet:**
   - Do NOT add cloud model integrations or paid APIs.
   - Do NOT build voice or speech interfaces.
   - Do NOT add browser automation or computer operation tools.
   - Focus exclusively on making local coding, testing, and recovery rock-solid.

---

## J. Future Roadmap

### Priority 1: Reliability Foundation (Immediate Next Step)
- Model-call timeout and clean cancellation.
- Timed, unbuffered test logging.
- Clean cleanup of sandbox artifacts.

### Priority 2: End-to-End Workflow Verification
- Direct-task single-pass execution (QA, search, read, write) with zero multi-stage overhead.
- Project-task execution verifying stage decisions reflect actual task complexity.

### Priority 3: Genuine Live Recovery Verification
- Construct a deterministic test where initial verification is guaranteed to fail (e.g. strict pre-existing unit test asserting behavior that initial code lacks).
- Verify that `VERDICT: FAIL` is emitted, defects are extracted and handed to `qwen2.5-coder:7b`, correction is applied, re-verification runs, and `VERDICT: PASS` is attained.

### Priority 4: Local Model Benchmarking
- Evaluate installed models (`qwen3.5:4b`, `qwen2.5-coder:7b`, `qwen3.5:9b`, `nemotron-3-nano:4b`) on standard tasks.
- Measure VRAM usage, token throughput, and tool-calling reliability.

### Priority 5: Optional Cloud & Free Providers (Future)
- Explicit opt-in only; enforce rate limits, strict privacy, and zero paid fallback.

### Priority 6: Multimodal, Voice & Computer Operation (Future)
- Image input handling for vision-capable models (screenshot debugging).
- Speech-to-text / text-to-speech bridge to the existing AURA controller.
- Sandboxed browser automation for web verification.

---

## K. Chronological History of Significant Decisions & Plan Changes

1. **Decision 1: From Rigid Chains to Role-Based Capability Routing**
   - *Initial Approach:* Monolithic single-model loop or fixed multi-agent chains.
   - *Change:* Implemented `ModelRouter` with 4 clear capabilities: `LIGHTWEIGHT`, `ANALYSIS`, `CODING`, `VERIFICATION`.
   - *Reason:* Simple questions wasted tokens when forced into coding workflows; complex tasks failed when single models attempted to plan and code simultaneously.
   - *User Approval:* Approved.
2. **Decision 2: VRAM Calibration for Hardware Limits**
   - *Initial Approach:* Defaulting analysis to `qwen3.5:9b`.
   - *Change:* Set default `ANALYSIS` and `VERIFICATION` to `qwen3.5:4b` (3.4GB) and `CODING` to `qwen2.5-coder:7b` (4.7GB).
   - *Reason:* RTX 3050 has 6GB VRAM. `qwen3.5:9b` (6.6GB) forced CPU offload and 60+ second inference times. Swapping between 4B and 7B fits 100% in VRAM.
   - *User Approval:* Approved.
3. **Decision 3: Word-Boundary Task Classification**
   - *Initial Approach:* Split-based keyword matching (`"fix" in task.split()`).
   - *Change:* Replaced with regex `\b[a-z0-9_]+\b` and word-count threshold (`< 8 words` stay single-pass).
   - *Reason:* Punctuation (e.g., `"fix, authentication bug"`, `"implement."`) broke keyword matching or misrouted short commands.
   - *User Approval:* Approved.
4. **Decision 4: Multi-Stage Final Report Aggregation**
   - *Initial Approach:* `run_multi_stage` returned only `results[-1]`.
   - *Change:* Built `_build_final_multi_stage_report` combining findings, inspected files, coding actions, and verification verdicts.
   - *Reason:* When coding was skipped in read-only mode, all analysis findings were discarded.
   - *User Approval:* Approved.
5. **Decision 5: Separation of Inspection Notices from Defects**
   - *Initial Approach:* Any tool result containing `"does not exist"` called `task_state.add_error(...)`.
   - *Change:* Created `error_classifier.py` and partitioned `TaskState` into `inspection_notices`, `execution_errors`, `defects`.
   - *Reason:* Checking whether a candidate file exists is normal exploratory behavior, not a defect.
   - *User Approval:* Approved.

---

## L. Remaining Work & Known Defects

1. **Unbounded Model Call Inference:**
   - *Defect:* `provider.chat()` lacks an `asyncio.wait_for` timeout.
   - *Remedy:* Add `model_call_timeout_seconds` in settings and wrap model chat invocations.
2. **Unverified Live Recovery:**
   - *Defect:* Live correction loop has not been exercised against a real test failure.
   - *Remedy:* Run a deterministic test where initial verification produces `VERDICT: FAIL`, triggering correction.
3. **Artifact Cleanup:**
   - Remove `test_sandbox_p4/` and decide whether to delete or expose `src/aura/version.py`.
4. **Git Staging & Commit:**
   - Working tree has clean, verified changes on `feature/multi-model-routing` awaiting staging and commit.

---

## M. Single Best Next Action

**Implement `model_call_timeout_seconds: int = 120` in `src/aura/config/settings.py` and wrap `provider.chat()` in `src/aura/agents/loop.py` with `asyncio.wait_for`.**

*Why this must happen first:*  
This is the root cause of the latest test hang. Without a bounded timeout on model calls, any subsequent test or user session is vulnerable to indefinite socket hangs. Fixing this provides the reliable execution foundation required for all future live tests and recovery demonstrations.
