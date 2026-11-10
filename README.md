# AURA V4 — Local-First Autonomous AI Engineering Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests Passing](https://img.shields.io/badge/tests-262%20passed-brightgreen.svg)]()
[![Inference](https://img.shields.io/badge/inference-local%20Ollama-orange.svg)](https://ollama.com/)
[![Protocol](https://img.shields.io/badge/MCP-FastMCP-purple.svg)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**AURA V4** is a local-first, autonomous AI software engineering agent designed to run end-to-end coding workflows on consumer hardware. Operating over local Ollama models and Model Context Protocol (MCP) servers, AURA inspects codebases, identifies defects, applies surgical modifications, and verifies solutions against automated test suites — with **zero required cloud API dependencies** and **strict deterministic security guardrails**.

---

## Key Capabilities

- **Local-First & Privacy-Preserving:** Runs entirely on local infrastructure via Ollama (`http://127.0.0.1:11434`). Source code, proprietary logic, and environment data never leave your local machine.
- **Hardware-Calibrated Dual-Model Pipeline:** Calibrated specifically for 6GB VRAM GPUs (e.g. NVIDIA RTX 3050 Laptop GPU). Dynamically switches between:
  - `qwen3.5:4b` (~3.4GB) for triage, repository analysis, and test verification.
  - `qwen2.5-coder:7b` (~4.7GB) for code generation, refactoring, and surgical edits.
- **Multi-Stage Orchestration Pipeline:**
  1. **Stage 1: Analysis** — Explores file trees, traces dependencies, extracts requirements and defect root causes.
  2. **Stage 2: Coding** — Generates minimal, surgical changes adhering to codebase conventions. Skipped automatically in read-only (`ro`) mode.
  3. **Stage 3: Verification** — Executes automated test suites (`pytest`), validates outcomes, and synthesizes an evidence-grounded report.
  4. **Bounded Self-Correction Loop** — When verification detects failures, defects are extracted and handed back to the coding specialist for up to 2 correction cycles.
- **FastMCP Subprocess Architecture:** Decoupled filesystem and Git tooling communicating via JSON-RPC stdio pipes:
  - `filesystem_server`: `read_file` (with line-slicing), `list_directory`, `search_files`, `write_file`, `edit_file`, `get_file_diff`, `run_tests`.
  - `git_server`: `git_status`, `git_diff`, `git_log`, `git_commit`, `git_branch`.
- **Deterministic Security Boundary:**
  - Strict workspace sandboxing (`safe_path`) preventing directory traversal escapes (`../`).
  - Read-Only (`ro`) and Read-Write (`rw`) mode enforcement outside LLM prompt context.
  - Automatic shielding of sensitive files (`.env`, private keys, certificates, credentials).
  - Shell command injection blocking during test execution.
- **Progress-Aware Execution Controller:** Prevents infinite LLM loops by tracking progress ratios, detecting repeated duplicate tool calls, and enforcing consecutive failure thresholds.
- **Structured Error Classification:** Automatically partitions tool outputs into `INSPECTION_NOTICE` (e.g. candidate file checks), `EXECUTION_ERROR` (e.g. syntax bugs), and `TASK_DEFECT` (e.g. failing assertions), preventing false alarms.

---

## Architecture Overview

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

---

## Hardware Calibration & Recommended Models

AURA V4 is designed to run efficiently on consumer laptop and desktop hardware without cloud offloading:

| Hardware Component | Minimum Requirement | Tested Configuration |
| :--- | :--- | :--- |
| **GPU / VRAM** | 6GB VRAM | NVIDIA GeForce RTX 3050 Laptop GPU (6GB VRAM) |
| **System RAM** | 16 GB | 16 GB DDR4/DDR5 |
| **Storage** | 20 GB free space | NVMe SSD |
| **OS** | Windows 11 / Linux / macOS | Windows 11 Pro 64-bit |

### Model Roles

| Role | Default Model | Parameter Size | Memory Footprint | Primary Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Lightweight / Triage** | `qwen3.5:4b` | 4.7B | ~3.4 GB | Simple Q&A, workspace status, path exploration |
| **Analysis** | `qwen3.5:4b` | 4.7B | ~3.4 GB | Architecture analysis, defect diagnosis |
| **Coding** | `qwen2.5-coder:7b` | 7.6B | ~4.7 GB | Code generation, surgical refactoring, bug fixes |
| **Verification** | `qwen3.5:4b` | 4.7B | ~3.4 GB | Reviewing diffs, analyzing test outputs, verdict |

---

## Getting Started

### Prerequisites

1. **Python 3.11+** installed (`python --version`)
2. **Git** installed and available in `PATH`
3. **Ollama** installed and running:
   ```bash
   ollama serve
   ```
4. Pull the calibrated local models:
   ```bash
   ollama pull qwen3.5:4b
   ollama pull qwen2.5-coder:7b
   ```

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Arihant-57/AURA-V4.git
   cd AURA-V4
   ```

2. Create and activate a Python virtual environment:
   ```bash
   # Windows (PowerShell)
   python -m venv .venv
   .venv\Scripts\Activate.ps1

   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements-dev.txt
   ```

4. Configure environment settings:
   ```bash
   cp .env.example .env
   ```

---

## Usage

### 1. Verification Sanity Check
Confirm that MCP subprocesses, tools, and the local Ollama provider connect properly:
```bash
python -m src.aura.agents.aura_v4 --selftest
```

### 2. Interactive REPL Session
Start an interactive engineering session on an authorized workspace:
```bash
python -m src.aura.agents.aura_v4 --workspace "C:\path\to\your\project" --mode ro
```

Within the interactive REPL:
- `/workspace <path>` — Switch active workspace (prompts for authorization).
- `/mode [ro|rw]` — Toggle between read-only (`ro`) and read-write (`rw`) modes.
- `/models` — Display discovered Ollama models and role assignments.
- `/status` — View current workspace, access mode, inspected files, and metrics.
- `/reset` — Clear session memory and deterministic result cache.
- `exit` or `quit` — Cleanly exit the session.

### 3. Headless / Single Task Execution
Execute a non-interactive task and receive a structured markdown report:
```bash
# Read-only exploration
python -m src.aura.agents.aura_v4 --workspace "./my-project" --mode ro --task "Analyze the authentication flow in src/auth.py"

# Read-write modification with verification
python -m src.aura.agents.aura_v4 --workspace "./my-project" --mode rw --yes --task "Fix the edge case in calculate_tax() and run tests"
```

---

## Running Tests

AURA V4 includes a comprehensive test suite of **262 automated unit and integration tests** validating tool contracts, path sandboxing, loop stability, multi-model routing, and multi-stage orchestration:

```bash
# Run all tests
python -m pytest tests/ -v

# Run unit tests only
python -m pytest tests/unit/ -v

# Run integration tests only
python -m pytest tests/integration/ -v

# Run with quiet summary
python -m pytest tests/ -q
```

All 262 tests run with mocked Ollama/MCP subprocess fixtures and execute in ~23 seconds with zero external network access required.

---

## Security Model & Sandboxing

AURA V4 enforces defense-in-depth at trust boundaries:

- **Path Sandboxing:** All filesystem operations resolve through `safe_path()`. Access outside the authorized workspace root raises access violations.
- **Access Mode Guardrails:** In `ro` mode, `write_file`, `edit_file`, and mutating operations are rejected by server policy before reaching the filesystem.
- **Credential Protection:** Files matching `.env*`, `*.pem`, `*.key`, `id_rsa`, `*credentials*`, and `*secret*` are shielded from inspection.
- **Diff Redaction:** File diff tools automatically redact sensitive tokens before returning output to the model context.
- **Test Command Sanitization:** Test execution strictly validates options, blocking shell parameter injection.

---

## Project Status & Roadmap

- [x] FastMCP Filesystem and Git stdio servers.
- [x] Deterministic workspace isolation and security policy enforcement.
- [x] Single-pass and multi-stage multi-model routing.
- [x] VRAM-calibrated role assignment (`qwen3.5:4b` + `qwen2.5-coder:7b`).
- [x] Progress-aware execution controller and error classification.
- [x] 262/262 passing unit and integration tests.
- [ ] Model inference socket timeout & clean cancellation handler.
- [ ] Deterministic live recovery benchmark harness.
- [ ] Optional cloud model fallback provider (opt-in only).

> **Note on Cloud Readiness:** AURA V4 is currently built and calibrated as a **local-first** agent running on local Ollama hardware. While provider abstractions exist for future cloud integrations, production deployment currently assumes a local Ollama daemon.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
