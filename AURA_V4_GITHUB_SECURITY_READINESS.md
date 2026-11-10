# AURA V4 — GitHub Security & Open-Source Readiness Report

**Execution Date:** 2026-11-10  
**Target Repository:** `https://github.com/Arihant-57/AURA-V4`  
**Verified Project Directory:** `C:\Users\ariha\local-ai-lab\phase4-agent`  
**Current Git Branch:** `feature/multi-model-routing`  
**Repository Remote:** `origin -> https://github.com/Arihant-57/AURA-V4.git`  
**Status:** **READY FOR UPLOAD (AWAITING USER AUTHORIZATION — NO CODE PUSHED)**

---

## 1. Verified Project Directory

- **Active Directory:** `C:\Users\ariha\local-ai-lab\phase4-agent`
- **Verification Method:**
  - Located via file tree audit of `C:\Users\ariha\` and `C:\Users\ariha\local-ai-lab\`.
  - Confirmed against project documentation (`AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md`, `AURA_V4_FINAL_VERIFICATION_REPORT.md`).
  - Confirmed that `phase3-agent` is an older, superseded Phase 3 prototype with separate dependencies.
  - Confirmed that `phase4-agent` is the genuine, complete, and actively developed AURA V4 project codebase.
  - Confirmed that `C:\Users\ariha\Documents` is a parent workspace holding unrelated projects (Hostel Complaint Management System, Smart Helmet, TalentHub, AISD, etc.) which are strictly external test targets or separate projects.

---

## 2. Actual AURA V4 Architecture

AURA V4 is an autonomous, privacy-first local AI engineering agent operating over local Ollama models and Model Context Protocol (MCP) servers:

```
                      +---------------------------------------+
                      |       AURA V4 CLI / REPL Layer        |
                      |     (Interactive & Headless Task)     |
                      +---------------------------------------+
                                          |
                      +---------------------------------------+
                      |             Model Router              |
                      |   - Regex Intent Classification       |
                      |   - Single-Pass vs Multi-Stage        |
                      +---------------------------------------+
                                          |
         +--------------------------------+--------------------------------+
         |                                                                 |
[Single-Pass Query]                                               [Multi-Stage Pipeline]
         |                                                                 |
         v                                                                 v
+------------------+                                      +---------------------------------+
|   Agent Loop     |                                      |          Orchestrator           |
| (Lightweight 4B) |                                      | Stage 1: Analysis (4B)          |
+------------------+                                      | Stage 2: Coding (7B)            |
         |                                                | Stage 3: Verification (4B)      |
         |                                                | Bounded Correction (Max 2 cyc)  |
         |                                                +---------------------------------+
         |                                                                 |
         +--------------------------------+--------------------------------+
                                          |
                      +---------------------------------------+
                      |         Execution Controller          |
                      |   - Dynamic Turn Budget Control       |
                      |   - Duplicate & Failure Guards        |
                      +---------------------------------------+
                                          |
                      +---------------------------------------+
                      |                MCP Hub                |
                      |  SessionMemory  *  ResultCache        |
                      +---------------------------------------+
                                          |
                    +---------------------+---------------------+
                    | JSON-RPC (stdio)                          | JSON-RPC (stdio)
                    v                                           v
      +---------------------------+               +---------------------------+
      |  FastMCP Filesystem Server|               |    FastMCP Git Server     |
      | - safe_path sandboxing    |               | - git_status, git_diff    |
      | - line slicing, test exec |               | - git_log, git_commit     |
      +---------------------------+               +---------------------------+
```

### Core Architecture Components:
1. **Interactive CLI / REPL Layer (`src/aura/agents/aura_v4.py`)**:
   - Supports interactive terminal commands (`/workspace`, `/mode ro|rw`, `/models`, `/status`, `/reset`, `exit`).
   - Supports non-interactive single-task execution via `--task "..."` and positional queries.
   - Provides sanity verification via `--selftest`.
2. **Dynamic Model Registry (`src/aura/core/model_registry.py`)**:
   - Auto-discovers installed Ollama models via `http://127.0.0.1:11434/api/tags`.
   - Prioritizes models calibrated for 6GB VRAM (NVIDIA RTX 3050 Laptop GPU):
     - `qwen3.5:4b` (~3.4GB) for triage, repository analysis, and test verification.
     - `qwen2.5-coder:7b` (~4.7GB) for code generation, surgical refactoring, and bug fixes.
3. **Model Router & TaskState (`src/aura/core/model_router.py`)**:
   - Classifies developer intent using regex word-boundary pattern matching (`\b[a-z0-9_]+\b`).
   - Evaluates task complexity: queries under 8 words remain single-pass; complex tasks trigger multi-stage.
   - Carries structured findings, proposed changes, inspection notices, errors, and defects in `TaskState`.
4. **Multi-Stage Orchestrator (`src/aura/core/orchestrator.py`)**:
   - Coordinates Stage 1 (Analysis) -> Stage 2 (Coding) -> Stage 3 (Verification).
   - Skips code generation in read-only mode (`ro`).
   - Implements bounded self-correction loop (`MAX_CORRECTION_CYCLES = 2`) on verification failure (`VERDICT: FAIL`).
   - Generates partitioned final reports distinguishing notices from defects.
5. **Execution Controller & Error Classifier (`src/aura/core/execution_controller.py`, `error_classifier.py`)**:
   - Progress-aware turn loop tracking useful vs failed vs duplicate steps.
   - Extends turn budget dynamically up to 5 steps when progress is demonstrated; halts early on loops.
   - Classifies tool results into `INSPECTION_NOTICE`, `EXECUTION_ERROR`, and `TASK_DEFECT`.
6. **FastMCP Subprocesses (`src/aura/mcp/`)**:
   - `filesystem_server.py`: `read_file` (with line slicing), `list_directory`, `search_files`, `write_file`, `edit_file`, `get_file_diff`, `run_tests`.
   - `git_server.py`: `git_status`, `git_diff`, `git_log`, `git_commit`, `git_branch`.
7. **Deterministic Security Boundary (`src/aura/config/workspace.py`, `src/aura/core/evidence_constants.py`)**:
   - Enforces `safe_path` containment within the authorized workspace.
   - Rejects writes in read-only mode before filesystem interaction.
   - Shields `.env`, private keys, certificates, and credentials from inspection.
   - Redacts sensitive tokens from file diffs and prevents shell parameter injection.

---

## 3. Files Selected for Upload

All selected files genuinely belong to AURA V4 and are necessary for its operation, documentation, and reproducibility:

### A. Core AURA V4 Source Code (18 Modules)
- `src/aura/__init__.py`
- `src/aura/version.py`
- `src/aura/agents/__init__.py`
- `src/aura/agents/aura_v4.py`
- `src/aura/agents/loop.py`
- `src/aura/config/__init__.py`
- `src/aura/config/settings.py`
- `src/aura/config/workspace.py`
- `src/aura/core/__init__.py`
- `src/aura/core/cache.py`
- `src/aura/core/error_classifier.py`
- `src/aura/core/evidence.py`
- `src/aura/core/evidence_constants.py`
- `src/aura/core/execution_controller.py`
- `src/aura/core/file_tracker.py`
- `src/aura/core/mcp_hub.py`
- `src/aura/core/memory.py`
- `src/aura/core/model_registry.py`
- `src/aura/core/model_router.py`
- `src/aura/core/orchestrator.py`
- `src/aura/core/prompts.py`
- `src/aura/core/providers.py`
- `src/aura/core/scope.py`
- `src/aura/core/tools.py`
- `src/aura/core/validators.py`
- `src/aura/mcp/__init__.py`
- `src/aura/mcp/filesystem_server.py`
- `src/aura/mcp/git_server.py`

### B. Project Configuration & Build Files
- `.gitignore` (Production-grade open-source rules)
- `.env.example` (Sanitized configuration template)
- `requirements.txt` (Core dependencies: `ollama`, `mcp`, `pydantic`)
- `requirements-dev.txt` (Dev dependencies: `pytest`, `pytest-asyncio`)
- `pytest.ini` (Pytest configuration)
- `README.md` (Complete architectural, installation, and usage documentation)

### C. Automated Test Suite (18 Files, 262 Tests)
- `tests/conftest.py`
- `tests/test_agent_interaction.py`
- `tests/test_mcp_direct.py`
- `tests/integration/test_mcp_servers.py`
- `tests/integration/test_workspace.py`
- `tests/unit/test_agent_loop.py`
- `tests/unit/test_coding_tools.py`
- `tests/unit/test_diagnosis_validation.py`
- `tests/unit/test_error_classification.py`
- `tests/unit/test_evidence_prefetch.py`
- `tests/unit/test_filesystem_error_handling.py`
- `tests/unit/test_git_subdirectory.py`
- `tests/unit/test_loop_fixes.py`
- `tests/unit/test_multi_model.py`
- `tests/unit/test_orchestrator.py`
- `tests/unit/test_regression_mcp_flow.py`
- `tests/unit/test_security_path_policy.py`
- `tests/unit/test_workspace_and_permissions.py`

### D. Documentation & Verification Reports
- `docs/AURA_V4_AUTONOMOUS_IMPROVEMENT_REPORT.md`
- `docs/AURA_V4_FINAL_VERIFICATION_REPORT.md`
- `docs/audits/AURA_V4_AGENT_LOOP_ROOT_CAUSE.md`
- `docs/audits/AURA_V4_FINAL_VERIFICATION.md`
- `AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md`
- `AURA_V4_FINAL_VERIFICATION_REPORT.md`
- `AURA_V4_DEVELOPMENT_HANDOVER_2026-11-01.md`

### E. Reproducible Benchmark Tooling
- `benchmarks/run_context_benchmark.py` (Made path-portable via dynamic `Path(__file__)`)
- `benchmarks/context_benchmark_results.json`

---

## 4. Files Excluded from Upload

The following files are strictly excluded from the GitHub repository:

| File / Pattern | Location | Reason for Exclusion | Exclusion Mechanism |
| :--- | :--- | :--- | :--- |
| `src.zip` (167KB) | Root | Baseline source backup archive | `git rm --cached` + `*.zip` |
| 38 `*.pyc` files | `src/**`, `tests/**` | Compiled Python bytecode | `git rm --cached` + `*.py[cod]` |
| `test_sandbox_p4/` | Root | Ephemeral live test sandbox | Ignored by `test_sandbox*/` |
| `test-sandboxes/` | Root | Ephemeral project sandbox (`hcms_sandbox`) | Ignored by `test-sandboxes/` |
| `.claude/` | Root | Local Claude Code permissions config | Ignored by `.claude/` |
| `.pytest_cache/` | Root | Local pytest test execution cache | Ignored by `.pytest_cache/` |
| `traces/`, `reports/` | Root | Empty local logging / trace directories | Ignored by `traces/`, `reports/` |
| `phase3-agent/` | Parent (`local-ai-lab`) | Superseded Phase 3 codebase | Outside repository directory |
| `phase5-git-test/` | Parent (`local-ai-lab`) | Phase 5 scratch experiment | Outside repository directory |
| `phase3-agent.zip`, `phase4-agent.zip` | Parent (`local-ai-lab`) | Local backup archives | Outside repository directory |
| Local Ollama models | User profile (`.ollama/`) | GGUF model weights | Outside repository directory |

---

## 5. Phase 3 / Old Experiment Findings

- Inspected `C:\Users\ariha\local-ai-lab\phase3-agent`.
- Confirmed that Phase 3 was an earlier, superseded single-agent experimental prototype with older virtual environments and monolithic loops.
- Verified that `phase4-agent` contains zero runtime imports or dependencies on `phase3-agent`.
- Confirmed that Phase 3 code must NOT be included in AURA V4.

---

## 6. Scratch Experiment Findings

- Located `test_sandbox_p4/` in `phase4-agent`: created during Phase 4 live testing with stubs `math_service.py` and `test_math.py`. Untracked and excluded via `.gitignore`.
- Located `test-sandboxes/hcms_sandbox/server.js`: created during live user testing against Hostel Complaint Management System. Untracked and excluded via `.gitignore`.
- Located `integration_test_phase_g.py`: sanitized to use dynamic `Path(__file__).resolve().parent` rather than hardcoded local absolute paths.
- Confirmed that no scratch or temporary files will pollute the candidate upload.

---

## 7. Security Audit Findings

An automated AST and regex scan was performed across all candidate repository files:

- **Private Keys & Certificates:** 0 detected (`-----BEGIN RSA PRIVATE KEY-----`).
- **Generic API Keys & Tokens:** 0 detected.
- **Provider Credentials (OpenAI, Anthropic, GitHub, AWS, Slack):** 0 detected.
- **Database Credentials & Connection Strings:** 0 detected.
- **Passwords:** 0 hardcoded passwords detected.
- **Email Addresses:** Only dummy fixture emails detected (`test@aura.local` in `tests/conftest.py` and `tests/unit/test_git_subdirectory.py`).
- **Local User Paths:** Sanitized in `benchmarks/run_context_benchmark.py` and `integration_test_phase_g.py` by replacing hardcoded `C:\Users\ariha\local-ai-lab\phase4-agent` with dynamic environment-aware paths.
- **Environment Template:** `.env.example` contains only placeholder values with explicit instructions for local hardware configuration.

---

## 8. Git History Findings

- **Commit History:** 4 existing commits on the repository:
  - `ba56a28`: baseline: initial commit of phase4-agent before multi-agent testing and fixes
  - `d8b2e4b`: fix(aura): handle path traversal errors cleanly, fix git typing and subdirectory detection, add .gitignore
  - `c7450e2`: docs: comprehensive final verification report for AURA V4
  - `2503376`: chore: remove tracked bytecode caches
- **Git Commit Log Audit:** Full `git log -p --all` security inspection confirmed that **zero API keys, private tokens, or credentials were ever committed to Git history**.
- **Bytecode Index Cleanup:** 30 `.pyc` files that were tracked in commit `ba56a28` were unstaged (`git rm --cached`) and are staged for removal.
- **Archive Index Cleanup:** `src.zip` was unstaged (`git rm --cached`) and is staged for removal.
- **Remote Configuration:** Added `origin` pointing to `https://github.com/Arihant-57/AURA-V4.git`. Verified via `git ls-remote` that the remote repository is completely empty.

---

## 9. Changes Made During Preparation

1. **Production `.gitignore`:**
   Created a comprehensive `.gitignore` covering Python bytecode, virtual environments, `.env` variants, IDE configs, ephemeral test sandboxes (`test-sandboxes/`, `test_sandbox*/`), traces, reports, archives (`*.zip`), models, and SQLite databases.
2. **Untracked `src.zip` from Git Index:**
   Executed `git rm --cached src.zip` to prevent the 167KB baseline archive from being uploaded, while preserving the local file on disk.
3. **Environment Template (`.env.example`):**
   Created `.env.example` specifying Ollama model defaults, context windows, sampling parameters, caching TTL, and hardware calibration notes without credentials.
4. **Dependency Specifications (`requirements.txt`, `requirements-dev.txt`):**
   Defined clean, minimal dependency trees (`ollama>=0.4.0`, `mcp>=1.2.0`, `pydantic>=2.0.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`).
5. **Project README (`README.md`):**
   Crafted full documentation covering architecture diagrams, VRAM calibration tables, installation, CLI usage, interactive REPL commands, testing instructions, and honest local-first status.
6. **Path Neutralization:**
   Updated `benchmarks/run_context_benchmark.py` and `integration_test_phase_g.py` to use dynamic paths (`Path(__file__).resolve().parent`) instead of hardcoded local machine paths.

---

## 10. Test Results

The test suite was executed against the actual prepared workspace:

- **Command Executed:** `python -m pytest tests/ -v`
- **Total Tests Collected:** 262
- **Passed Tests:** **262 (100%)**
- **Failed Tests:** **0**
- **Skipped Tests:** **0**
- **Execution Duration:** 22.91 seconds
- **CLI Sanity Check:** `python -m src.aura.agents.aura_v4 --selftest` exited with code 0 (`Self-test: V4 structure check complete.`).
- **Limitations:** Automated tests utilize mocked Ollama and MCP fixtures. Live Ollama model calls were not executed during this regression run to avoid unbounded model inference hangs.

---

## 11. Git Status

```
On branch feature/multi-model-routing
Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
	deleted:    src.zip
	deleted:    src/aura/agents/__pycache__/aura_v4.cpython-313.pyc
	deleted:    src/aura/agents/__pycache__/loop.cpython-313.pyc
	deleted:    src/aura/config/__pycache__/__init__.cpython-313.pyc
	deleted:    src/aura/config/__pycache__/settings.cpython-313.pyc
	deleted:    src/aura/config/__pycache__/workspace.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/__init__.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/cache.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/evidence.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/evidence_constants.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/mcp_hub.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/memory.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/prompts.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/providers.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/scope.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/tools.cpython-313.pyc
	deleted:    src/aura/core/__pycache__/validators.cpython-313.pyc
	deleted:    tests/__pycache__/conftest.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/__pycache__/conftest.cpython-313.pyc
	deleted:    tests/__pycache__/test_agent_interaction.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/__pycache__/test_mcp_direct.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_agent_loop.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_agent_loop.cpython-313.pyc
	deleted:    tests/unit/__pycache__/test_coding_tools.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_diagnosis_validation.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_evidence_prefetch.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_loop_fixes.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_regression_mcp_flow.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_regression_mcp_flow.cpython-313.pyc
	deleted:    tests/unit/__pycache__/test_security_path_policy.cpython-313-pytest-9.2.0.pyc
	deleted:    tests/unit/__pycache__/test_workspace_and_permissions.cpython-313-pytest-9.2.0.pyc

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   .gitignore
	modified:   benchmarks/run_context_benchmark.py
	modified:   integration_test_phase_g.py
	modified:   src/aura/agents/aura_v4.py
	modified:   src/aura/agents/loop.py
	modified:   src/aura/core/__init__.py
	modified:   tests/unit/test_agent_loop.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	.env.example
	AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md
	README.md
	requirements-dev.txt
	requirements.txt
	src/aura/core/error_classifier.py
	src/aura/core/execution_controller.py
	src/aura/core/file_tracker.py
	src/aura/core/model_registry.py
	src/aura/core/model_router.py
	src/aura/core/orchestrator.py
	src/aura/version.py
	tests/unit/test_error_classification.py
	tests/unit/test_multi_model.py
	tests/unit/test_orchestrator.py
```

---

## 12. Git Diff Summary

```
 .gitignore                          |  74 +++++++++++++++++-
 benchmarks/run_context_benchmark.py |   2 +-
 integration_test_phase_g.py         |   8 +-
 src/aura/agents/aura_v4.py          | 149 +++++++++++++++++++++++++++++++++---
 src/aura/agents/loop.py             |  95 +++++++++++++++++++++--
 src/aura/core/__init__.py           |  26 +++++++
 tests/unit/test_agent_loop.py       |   6 +-
 7 files changed, 334 insertions(+), 26 deletions(-)
```

---

## 13. Remaining Risks & Candid Architecture Notes

1. **Local Ollama Requirement:**
   AURA V4 is designed specifically around local Ollama models (`qwen3.5:4b` and `qwen2.5-coder:7b`). It is **not** currently cloud-hosted or serverless-ready out of the box. Users cloning the repository must have Ollama installed with the required models pulled.
2. **Model Call Socket Timeout:**
   As documented in `AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md`, `provider.chat()` invokes `asyncio.to_thread(chat, **kwargs)` without an explicit `asyncio.wait_for` timeout. If the local Ollama daemon drops a socket without sending an EOF, the call can suspend indefinitely. Implementing `model_call_timeout_seconds` is the primary next enhancement.
3. **Branch Alignment on Target Remote:**
   The remote GitHub repository is currently completely empty. The local development branch is `feature/multi-model-routing`. Upon your approval, we will merge or branch cleanly to `main` before pushing to `origin main`.

---

## 14. Exact Proposed Upload Commands

Once you approve the candidate file list and readiness report, the exact commands to stage, commit, and push to GitHub are:

```powershell
# 1. Switch to project directory
Set-Location "C:\Users\ariha\local-ai-lab\phase4-agent"

# 2. Stage all verified candidate files (untracked bytecode & sandboxes are blocked by .gitignore)
git add .gitignore .env.example README.md requirements.txt requirements-dev.txt
git add AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md
git add benchmarks/run_context_benchmark.py integration_test_phase_g.py
git add src/aura/
git add tests/

# 3. Create a clean commit on feature/multi-model-routing
git commit -m "feat(aura-v4): complete multi-model routing, orchestrator, and open-source release readiness"

# 4. Create and checkout the 'main' branch pointing to the verified release
git checkout -B main

# 5. Push the verified repository to the newly created GitHub repository
git push -u origin main
```

---

## Final Candidate File List for Verification

- `.env.example`
- `.gitignore`
- `AURA_V4_ANTIGRAVITY_PROJECT_HISTORY.md`
- `AURA_V4_DEVELOPMENT_HANDOVER_2026-11-01.md`
- `AURA_V4_FINAL_VERIFICATION_REPORT.md`
- `PERMISSION_APPROVAL_PLAN.md`
- `pytest.ini`
- `README.md`
- `requirements-dev.txt`
- `requirements.txt`
- `TEST_AUDIT_REPORT.md`
- `benchmarks/context_benchmark_results.json`
- `benchmarks/run_context_benchmark.py`
- `docs/AURA_V4_AUTONOMOUS_IMPROVEMENT_REPORT.md`
- `docs/AURA_V4_FINAL_VERIFICATION_REPORT.md`
- `docs/audits/AURA_V4_AGENT_LOOP_ROOT_CAUSE.md`
- `docs/audits/AURA_V4_FINAL_VERIFICATION.md`
- `integration_test_phase_g.py`
- `src/aura/__init__.py`
- `src/aura/version.py`
- `src/aura/agents/__init__.py`
- `src/aura/agents/aura_v4.py`
- `src/aura/agents/loop.py`
- `src/aura/config/__init__.py`
- `src/aura/config/settings.py`
- `src/aura/config/workspace.py`
- `src/aura/core/__init__.py`
- `src/aura/core/cache.py`
- `src/aura/core/error_classifier.py`
- `src/aura/core/evidence.py`
- `src/aura/core/evidence_constants.py`
- `src/aura/core/execution_controller.py`
- `src/aura/core/file_tracker.py`
- `src/aura/core/mcp_hub.py`
- `src/aura/core/memory.py`
- `src/aura/core/model_registry.py`
- `src/aura/core/model_router.py`
- `src/aura/core/orchestrator.py`
- `src/aura/core/prompts.py`
- `src/aura/core/providers.py`
- `src/aura/core/scope.py`
- `src/aura/core/tools.py`
- `src/aura/core/validators.py`
- `src/aura/mcp/__init__.py`
- `src/aura/mcp/filesystem_server.py`
- `src/aura/mcp/git_server.py`
- `tests/conftest.py`
- `tests/test_agent_interaction.py`
- `tests/test_mcp_direct.py`
- `tests/integration/test_mcp_servers.py`
- `tests/integration/test_workspace.py`
- `tests/unit/test_agent_loop.py`
- `tests/unit/test_coding_tools.py`
- `tests/unit/test_diagnosis_validation.py`
- `tests/unit/test_error_classification.py`
- `tests/unit/test_evidence_prefetch.py`
- `tests/unit/test_filesystem_error_handling.py`
- `tests/unit/test_git_subdirectory.py`
- `tests/unit/test_loop_fixes.py`
- `tests/unit/test_multi_model.py`
- `tests/unit/test_orchestrator.py`
- `tests/unit/test_regression_mcp_flow.py`
- `tests/unit/test_security_path_policy.py`
- `tests/unit/test_workspace_and_permissions.py`

**Total Candidate Files:** 63 files (18 source modules, 18 test files, 15 documentation/benchmark files, 6 project config/environment files, and 6 audit/handover docs).  
**Zero secrets, zero credentials, zero `.pyc` bytecode, zero zip archives, zero unrelated experiments.**
