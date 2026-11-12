"""
Multi-stage task orchestrator for AURA V4.

Runs analysis → coding → verification as separate model invocations,
preserving context via TaskState handoff between stages.

Only activated for complex tasks (needs_multi_stage == True).
Simple tasks bypass this and use a single run_agent_loop call.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .model_registry import ModelRole
from .model_router import (
    ModelRouter, TaskState, TaskStage,
    classify_task_role, needs_multi_stage,
)
from .scope import TaskScope
from .providers import OllamaProvider, ProviderFactory
from ..core import McpHub, SessionMemory, ResultCache
from ..core.file_tracker import InspectionTracker

logger = logging.getLogger(__name__)


def _run_subprocess_dry_runs(state: TaskState, workspace_path: str | Path | None) -> list[str]:
    """Execute syntax and sanity dry runs on newly modified/created files in workspace."""
    if not workspace_path:
        return []

    ws = Path(workspace_path).resolve()
    if not ws.exists() or not ws.is_dir():
        return []

    defects: list[str] = []

    # 1. Identify target files from state tracking
    target_files: set[str] = set()
    for p, status in state.inspected_files.items():
        if status in {"modified", "created", "written", "identified"}:
            target_files.add(p)

    # 2. Add files detected via git status if available
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if len(line) > 3:
                    file_p = line[3:].strip()
                    target_files.add(file_p)
    except Exception:
        pass

    for rel_path in sorted(target_files):
        clean_rel = rel_path.strip().strip("'\"`")
        # Ignore hidden files, markdown, json, or non-code files
        if not clean_rel or clean_rel.startswith(".") or clean_rel.endswith((".md", ".json", ".txt", ".csv")):
            continue

        full_path = ws / clean_rel
        if not full_path.exists():
            if state.inspected_files.get(rel_path) in {"modified", "created", "written"}:
                defects.append(f"Expected file '{clean_rel}' does not exist on disk in workspace.")
            continue

        if not full_path.is_file():
            continue

        # Dry run Python files
        if full_path.suffix == ".py":
            # A. Syntax compilation check
            try:
                py_res = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(full_path)],
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if py_res.returncode != 0:
                    err_msg = py_res.stderr.strip() or py_res.stdout.strip()
                    defects.append(f"Python syntax compilation failed for '{clean_rel}':\n{err_msg}")
                    continue
            except Exception as exc:
                logger.warning("py_compile check failed on %s: %s", clean_rel, exc)

            # B. Subprocess execution check for standalone scripts/tests
            name_lower = full_path.name.lower()
            try:
                file_text = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                file_text = ""
            is_runnable = (
                name_lower.startswith(("test", "simulate", "validate", "run", "benchmark"))
                or "if __name__" in file_text
            )
            if is_runnable:
                try:
                    run_res = subprocess.run(
                        [sys.executable, str(full_path)],
                        cwd=str(ws),
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if run_res.returncode != 0:
                        err_msg = run_res.stderr.strip() or run_res.stdout.strip()
                        defects.append(f"Subprocess execution error in '{clean_rel}' (exit code {run_res.returncode}):\n{err_msg}")
                except subprocess.TimeoutExpired:
                    defects.append(f"Subprocess execution timed out (>15s) in '{clean_rel}'")
                except Exception as exc:
                    defects.append(f"Subprocess execution error in '{clean_rel}': {exc}")

        # Dry run JavaScript files
        elif full_path.suffix == ".js":
            # A. Syntax check via node --check
            try:
                node_res = subprocess.run(
                    ["node", "--check", str(full_path)],
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if node_res.returncode != 0:
                    err_msg = node_res.stderr.strip() or node_res.stdout.strip()
                    defects.append(f"Node.js syntax check failed for '{clean_rel}':\n{err_msg}")
                    continue
            except FileNotFoundError:
                pass  # node not available
            except Exception as exc:
                logger.warning("node syntax check failed on %s: %s", clean_rel, exc)

            # B. Subprocess execution check for test runners
            name_lower = full_path.name.lower()
            if name_lower.startswith(("test", "run_test", "validate", "simulate")):
                try:
                    node_run = subprocess.run(
                        ["node", str(full_path)],
                        cwd=str(ws),
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if node_run.returncode != 0:
                        err_msg = node_run.stderr.strip() or node_run.stdout.strip()
                        defects.append(f"Node.js execution failed for '{clean_rel}' (exit code {node_run.returncode}):\n{err_msg}")
                except subprocess.TimeoutExpired:
                    defects.append(f"Node.js execution timed out (>15s) in '{clean_rel}'")
                except Exception as exc:
                    defects.append(f"Node.js execution error in '{clean_rel}': {exc}")

    return defects

async def run_agent_loop(*args: Any, **kwargs: Any) -> str:
    """Wrapper around loop.run_agent_loop to break circular import and permit mocking."""
    from ..agents.loop import run_agent_loop as _real_loop
    return await _real_loop(*args, **kwargs)

# Max correction cycles before giving up
MAX_CORRECTION_CYCLES = 2

# Stage-specific system prompt suffixes
ANALYSIS_INSTRUCTIONS = (
    "\n\n## CURRENT STAGE: ANALYSIS\n"
    "You are in the ANALYSIS stage. Your job:\n"
    "1. Understand the user's task.\n"
    "2. Inspect relevant source files using tools.\n"
    "3. Identify actual problems with evidence.\n"
    "4. List files that need changes.\n"
    "5. Propose specific changes.\n"
    "6. Identify relevant tests.\n\n"
    "DO NOT modify any files. Only read and analyze.\n"
    "End your response with a structured summary:\n"
    "FINDINGS: (bullet list of discoveries)\n"
    "FILES: (files that need changes)\n"
    "CHANGES: (specific proposed changes)\n"
    "TESTS: (relevant test files)\n"
    "REMAINING: (open questions)\n"
)

CODING_INSTRUCTIONS_TEMPLATE = (
    "\n\n## CURRENT STAGE: CODING\n"
    "You are in the CODING stage. Previous analysis found:\n\n"
    "{handoff}\n\n"
    "Your job:\n"
    "1. Verify key analysis findings against actual source before changing code.\n"
    "2. Implement the proposed changes.\n"
    "3. Preserve existing architecture.\n"
    "4. Record what you changed.\n\n"
    "If analysis findings are incorrect, note the correction and proceed.\n"
    "End your response with:\n"
    "IMPLEMENTED: (what you changed)\n"
    "MODIFIED_FILES: (exact files changed)\n"
    "NEEDS_TESTING: (what should be verified)\n"
)

VERIFICATION_INSTRUCTIONS_TEMPLATE = (
    "\n\n## CURRENT STAGE: VERIFICATION\n"
    "You are in the VERIFICATION stage. Previous work:\n\n"
    "{handoff}\n\n"
    "Your job:\n"
    "1. Inspect the git diff or modified files.\n"
    "2. Run relevant tests.\n"
    "3. Verify the implementation matches the original task.\n"
    "4. Check permissions were respected.\n\n"
    "If you find a defect, describe it precisely.\n"
    "End your response with:\n"
    "VERDICT: PASS or FAIL\n"
    "DETAILS: (what was checked)\n"
    "DEFECTS: (if FAIL, specific issues)\n"
)


async def run_multi_stage(
    task: str,
    router: ModelRouter,
    hub: McpHub,
    memory: SessionMemory,
    cache: ResultCache,
    *,
    ctx_override: int | None = None,
    task_state: TaskState | None = None,
) -> str:
    """Run a multi-stage analysis→coding→verification pipeline.

    Returns the final combined result string.
    """
    scope = TaskScope(task)
    tracker = InspectionTracker()
    state = task_state if task_state is not None else TaskState(
        original_request=task,
        workspace=str(getattr(hub, "workspace", "")),
        read_only=getattr(hub, "read_only", True),
        stage=TaskStage.ANALYSIS,
    )

    results: list[str] = []

    try:
        # ── Stage 1: Analysis ───────────────────────────────────────────────
        analysis_model = router.select(ModelRole.ANALYSIS)
        analysis_provider = ProviderFactory.create_ollama(
            model=analysis_model, num_ctx=ctx_override,
        )
        state.used_providers.append(analysis_provider)
        logger.info("Multi-stage: ANALYSIS with %s", analysis_model)
        print(f"  [Stage 1: ANALYSIS with {analysis_model}]")

        analysis_task = task + ANALYSIS_INSTRUCTIONS
        analysis_result = await run_agent_loop(
            analysis_task, analysis_provider, hub, memory, cache,
            task_state=state, inspection_tracker=tracker,
        )

        # Parse structured findings from analysis output
        _extract_findings(analysis_result, state)
        state.completed_work.append(f"Analysis completed with {analysis_model}")
        state.stage = TaskStage.CODING
        results.append(f"## Analysis ({analysis_model})\n{analysis_result}")

        # Evict analysis model from GPU memory before loading coding model
        if hasattr(analysis_provider, "unload"):
            analysis_provider.unload()

        # ── Stage 2: Coding (skip if read-only) ─────────────────────────────
        coding_provider = None
        coding_model = None
        if state.read_only:
            logger.info("Multi-stage: skipping CODING (read-only workspace).")
            print("  [Stage 2: CODING skipped (read-only workspace)]")
            state.stage = TaskStage.VERIFICATION
            state.completed_work.append("Coding skipped (read-only)")
        else:
            coding_model = router.select(ModelRole.CODING)
            coding_provider = ProviderFactory.create_ollama(
                model=coding_model, num_ctx=ctx_override,
            )
            state.used_providers.append(coding_provider)
            logger.info("Multi-stage: CODING with %s", coding_model)
            print(f"  [Stage 2: CODING with {coding_model}]")

            handoff = state.build_handoff()
            coding_task = task + CODING_INSTRUCTIONS_TEMPLATE.format(handoff=handoff)
            coding_result = await run_agent_loop(
                coding_task, coding_provider, hub, memory, cache,
                task_state=state, inspection_tracker=tracker,
            )

            _extract_implementation(coding_result, state)
            state.completed_work.append(f"Coding completed with {coding_model}")
            state.stage = TaskStage.VERIFICATION
            results.append(f"## Implementation ({coding_model})\n{coding_result}")

            # Evict coding model from GPU memory before loading verification model
            if hasattr(coding_provider, "unload"):
                coding_provider.unload()

        # ── Stage 3: Verification ───────────────────────────────────────────
        # Subprocess dry runs on modified/created workspace files
        dry_run_defects = _run_subprocess_dry_runs(state, getattr(hub, "workspace", None))
        for df in dry_run_defects:
            state.add_defect(df)

        verify_model = router.select(ModelRole.VERIFICATION)
        verify_provider = ProviderFactory.create_ollama(
            model=verify_model, num_ctx=ctx_override,
        )
        state.used_providers.append(verify_provider)
        logger.info("Multi-stage: VERIFICATION with %s", verify_model)
        print(f"  [Stage 3: VERIFICATION with {verify_model}]")

        handoff = state.build_handoff()
        if dry_run_defects:
            handoff += "\n\n### Automated Subprocess Dry-Run Defects\n" + "\n".join(f"- {d}" for d in dry_run_defects)

        verify_task = task + VERIFICATION_INSTRUCTIONS_TEMPLATE.format(handoff=handoff)
        verify_result = await run_agent_loop(
            verify_task, verify_provider, hub, memory, cache,
            task_state=state, inspection_tracker=tracker,
        )

        state.completed_work.append(f"Verification completed with {verify_model}")
        results.append(f"## Verification ({verify_model})\n{verify_result}")

        # ── Bounded correction cycle ────────────────────────────────────────
        needs_correction = not state.read_only and (_verdict_is_fail(verify_result) or bool(state.defects))
        if needs_correction and coding_provider is not None:
            for cycle in range(MAX_CORRECTION_CYCLES):
                logger.info("Multi-stage: correction cycle %d", cycle + 1)
                defects = _extract_defects(verify_result)
                for df in state.defects:
                    if df not in defects:
                        defects.append(df)
                if not defects:
                    break

                # Evict verification model before loading coding model
                if hasattr(verify_provider, "unload"):
                    verify_provider.unload()

                state.stage = TaskStage.CODING
                state.errors.extend(defects[:5])
                correction_handoff = state.build_handoff()
                correction_task = (
                    f"Fix the following defects found during verification:\n"
                    + "\n".join(f"- {d}" for d in defects)
                    + CODING_INSTRUCTIONS_TEMPLATE.format(handoff=correction_handoff)
                )
                coding_result = await run_agent_loop(
                    correction_task, coding_provider, hub, memory, cache,
                    task_state=state, inspection_tracker=tracker,
                )
                _extract_implementation(coding_result, state)
                state.completed_work.append(f"Correction cycle {cycle + 1}")

                # Evict coding model before re-verifying
                if hasattr(coding_provider, "unload"):
                    coding_provider.unload()

                # Re-run dry runs
                state.defects.clear()
                new_dry_defects = _run_subprocess_dry_runs(state, getattr(hub, "workspace", None))
                for df in new_dry_defects:
                    state.add_defect(df)

                # Re-verify
                state.stage = TaskStage.VERIFICATION
                correction_verify_handoff = state.build_handoff()
                if new_dry_defects:
                    correction_verify_handoff += "\n\n### Automated Subprocess Dry-Run Defects\n" + "\n".join(f"- {d}" for d in new_dry_defects)
                re_verify_task = task + VERIFICATION_INSTRUCTIONS_TEMPLATE.format(
                    handoff=correction_verify_handoff
                )
                verify_result = await run_agent_loop(
                    re_verify_task, verify_provider, hub, memory, cache,
                    task_state=state, inspection_tracker=tracker,
                )
                results.append(f"## Correction {cycle + 1} + Re-verification\n{verify_result}")

                if not _verdict_is_fail(verify_result) and not state.defects:
                    break

        if _verdict_is_fail(verify_result) or state.defects:
            defects = _extract_defects(verify_result)
            for d in defects:
                state.add_defect(d)
            if not state.defects:
                state.add_defect("Verification returned FAIL")

        state.stage = TaskStage.COMPLETE

        return _build_final_multi_stage_report(task, state, tracker, results)
    finally:
        # Guarantee all loaded models are unloaded from VRAM
        for p in state.used_providers:
            if hasattr(p, "unload"):
                p.unload()


def _build_final_multi_stage_report(
    task: str,
    state: TaskState,
    tracker: InspectionTracker,
    results: list[str],
) -> str:
    """Build a compact structured report distinguishing notices, errors, and defects."""
    status = state.stage.value.upper()
    if state.defects:
        status = "FAILED"

    lines = [
        f"# Multi-Stage Task Complete",
        f"**Task:** {task}",
        f"**Status:** {status}",
        f"**Stages:** {' -> '.join(w for w in state.completed_work)}",
        f"**Inspected Files Count:** {len(tracker.files)}",
    ]

    # Inspected files
    if tracker.files:
        files_summary = [
            f"- {p} ({fi.coverage:.0%} read)" if fi.total_lines else f"- {p}"
            for p, fi in tracker.files.items()
        ]
        lines.append("\n### Inspected Files\n" + "\n".join(files_summary[:15]))
    elif state.inspected_files:
        lines.append("\n### Inspected Files\n" + "\n".join(f"- {p} ({st})" for p, st in list(state.inspected_files.items())[:15]))

    # Analysis Findings (crucial: must survive even when coding is skipped!)
    if state.findings:
        lines.append("\n### Analysis Findings\n" + "\n".join(f"- {f}" for f in state.findings[:15]))
    elif results and results[0]:
        lines.append("\n### Analysis Findings\n" + results[0][:500].strip())

    # Coding results (when applicable)
    if not state.read_only:
        coding_items = [w for w in state.completed_work if "coding" in w.lower() or "correction" in w.lower()]
        if coding_items:
            lines.append("\n### Coding Results\n" + "\n".join(f"- {c}" for c in coding_items))
        if state.proposed_changes:
            lines.append("\n### Proposed Changes\n" + "\n".join(f"- {c}" for c in state.proposed_changes[:10]))
    else:
        lines.append("\n### Coding Results\n- Skipped (workspace is in read-only mode)")

    # Verification results
    if results:
        lines.append("\n### Verification Results\n" + results[-1].strip())

    # Remaining work
    if state.remaining_work:
        lines.append("\n### Remaining Work\n" + "\n".join(f"- {r}" for r in state.remaining_work[:10]))

    # 1. Inspection notices (expected exploration outcomes: missing candidate files, etc.)
    if getattr(state, "inspection_notices", None):
        lines.append("\n### Inspection Notices\n" + "\n".join(f"- {n}" for n in state.inspection_notices[:10]))

    # 2. Execution errors (genuine permission, boundary, server, or I/O errors)
    if getattr(state, "execution_errors", None):
        lines.append("\n### Execution Errors\n" + "\n".join(f"- {e}" for e in state.execution_errors[:5]))

    # 3. Actual task defects (verification defects, test failures)
    if getattr(state, "defects", None):
        lines.append("\n### Task Defects\n" + "\n".join(f"- {d}" for d in state.defects[:5]))
    elif getattr(state, "errors", None) and not getattr(state, "execution_errors", None):
        lines.append("\n### Task Defects\n" + "\n".join(f"- {e}" for e in state.errors[:5]))

    return "\n".join(lines)


# ── Parsing helpers ─────────────────────────────────────────────────────

def _extract_findings(text: str, state: TaskState) -> None:
    """Pull structured findings from analysis output into TaskState."""
    for section, target in [
        ("FINDINGS:", state.findings),
        ("CHANGES:", state.proposed_changes),
        ("REMAINING:", state.remaining_work),
    ]:
        _extract_bullet_section(text, section, target)

    # Extract FILES section into inspected_files
    files_section = _get_section_text(text, "FILES:")
    if files_section:
        for line in files_section.splitlines():
            line = line.strip().lstrip("- ")
            if line and not line.startswith("("):
                state.inspected_files[line] = "identified"


def _extract_implementation(text: str, state: TaskState) -> None:
    """Pull implementation details from coding output into TaskState."""
    _extract_bullet_section(text, "IMPLEMENTED:", state.completed_work)

    files_section = _get_section_text(text, "MODIFIED_FILES:")
    if files_section:
        for line in files_section.splitlines():
            line = line.strip().lstrip("- ")
            if line:
                state.inspected_files[line] = "modified"


def _verdict_is_fail(text: str) -> bool:
    """Check if verification output contains VERDICT: FAIL."""
    lower = text.lower()
    # Look for explicit FAIL verdict
    if "verdict:" in lower:
        idx = lower.index("verdict:")
        snippet = lower[idx:idx + 30]
        return "fail" in snippet
    return False


def _extract_defects(text: str) -> list[str]:
    """Extract defect descriptions from verification output."""
    items: list[str] = []
    _extract_bullet_section(text, "DEFECTS:", items)
    return items


def _extract_bullet_section(text: str, header: str, target: list[str]) -> None:
    """Extract bullet items following a header like 'FINDINGS:'."""
    section = _get_section_text(text, header)
    if not section:
        return
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            item = line[2:].strip()
            if item and len(target) < 50:
                target.append(item)


def _get_section_text(text: str, header: str) -> str:
    """Get text between *header* and the next all-caps header or end."""
    upper = text.upper()
    header_upper = header.upper()
    idx = upper.find(header_upper)
    if idx == -1:
        return ""
    start = idx + len(header)
    # Find next section header (line starting with uppercase word followed by colon)
    import re
    remaining = text[start:]
    m = re.search(r"\n[A-Z][A-Z_]+:", remaining)
    if m:
        return remaining[:m.start()].strip()
    return remaining.strip()
