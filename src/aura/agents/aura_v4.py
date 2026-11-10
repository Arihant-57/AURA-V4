"""
AURA V4 — Functional execution engine with interactive workspace & permission control.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from ..config import get_settings, resolve_workspace
from ..config.workspace import normalize_workspace_path, find_referenced_directory
from ..core import McpHub, SessionMemory, ResultCache
from ..core.providers import ProviderFactory
from ..core.model_registry import ModelRegistry, ModelRole
from ..core.model_router import ModelRouter, classify_task_role, needs_multi_stage, TaskState, TaskStage
from ..core.scope import TaskScope
from ..core.orchestrator import run_multi_stage
from .loop import run_agent_loop


def parse_args():
    parser = argparse.ArgumentParser(description="AURA V4 AI Agent")
    parser.add_argument("--workspace", "-w", type=str, help="Workspace path")
    parser.add_argument("--mode", choices=["ro", "rw"], default=None, help="Access mode: ro (read-only) or rw (read-write)")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-confirm authorization prompts")
    parser.add_argument("--model", "-m", type=str, help="Model name override")
    parser.add_argument("--ctx", "-c", type=int, help="Context window size override (num_ctx)")
    parser.add_argument("--task", "-t", type=str, help="Single task to execute non-interactively")
    parser.add_argument("query", nargs="*", help="Positional task query")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--selftest", action="store_true", help="Run sanity check")
    return parser.parse_args()


def prompt_confirm(prompt_text: str, auto_confirm: bool = False) -> bool:
    """Prompt user for confirmation with [y/N] pattern."""
    if auto_confirm:
        print(f"{prompt_text} [y/N]: y (auto-confirmed)")
        return True
    try:
        ans = input(f"{prompt_text} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def print_status(hub: McpHub, provider, memory: SessionMemory, router: ModelRouter | None = None):
    """Print formatted session status."""
    mode_str = "read-only (ro)" if getattr(hub, "read_only", False) else "read-write (rw)"
    print("\n" + "=" * 40)
    print("=== AURA V4 Session Status ===")
    print(f"Workspace Root  : {hub.workspace}")
    print(f"Access Mode     : {mode_str}")
    print(f"Model           : {provider.model} (context: {provider.default_options.num_ctx})")
    if router:
        print(f"Available Models: {', '.join(router.registry.available_names) or 'unknown'}")
    print(f"Connected Tools : {len(hub.tools)} ({', '.join(sorted(hub.clients.keys()))})")
    dirs = sorted(memory.dirs.keys())
    print(f"Inspected Dirs  : {len(dirs)} ({', '.join(dirs[:5]) + ('...' if len(dirs) > 5 else '') or 'none'})")
    files = sorted(memory.files.keys())
    print(f"Inspected Files : {len(files)} ({', '.join(files[:5]) + ('...' if len(files) > 5 else '') or 'none'})")
    mods = sorted(memory.modified)
    print(f"Modified Files  : {len(mods)} ({', '.join(mods[:5]) + ('...' if len(mods) > 5 else '') or 'none'})")
    print("=" * 40 + "\n")


def print_help():
    """Print interactive command help."""
    print("""
Available Commands:
  /workspace <path> (or /cd)  Switch active workspace (prompts for authorization)
  /mode [ro|rw]               Toggle access mode (ro = read-only, rw = read-write)
  /models                     Show available models and role assignments
  /status (or /info)          Display current workspace, mode, and session stats
  /reset                      Clear session memory and cache
  /help                       Show this help message
  exit (or quit)              Shut down MCP servers and exit
""")


def format_task_metrics(providers: list[Any]) -> str:
    """Format token evaluation metrics from the providers actually used in the task."""
    if not providers:
        return "[Metrics: unavailable]"

    unique_providers = []
    seen = set()
    for p in providers:
        if p is not None and id(p) not in seen:
            seen.add(id(p))
            unique_providers.append(p)

    if not unique_providers:
        return "[Metrics: unavailable]"

    if len(unique_providers) == 1:
        p = unique_providers[0]
        prompt_eval = p.last_prompt_eval_count if getattr(p, "last_prompt_eval_count", None) is not None else "unavailable"
        eval_tok = p.last_eval_count if getattr(p, "last_eval_count", None) is not None else "unavailable"
        total_prompt = getattr(p, "total_prompt_eval_count", 0)
        opts = getattr(p, "default_options", None)
        ctx = getattr(opts, "num_ctx", "unknown") if opts else "unknown"
        m_name = getattr(p, "model", "unknown")
        return (
            f"[Metrics ({m_name}): prompt_eval_tokens={prompt_eval}, "
            f"eval_tokens={eval_tok}, total_prompt_tokens={total_prompt}, num_ctx={ctx}]"
        )

    total_prompt = sum(getattr(p, "total_prompt_eval_count", 0) for p in unique_providers)
    total_eval = sum(getattr(p, "total_eval_count", 0) for p in unique_providers)

    breakdowns = []
    for p in unique_providers:
        m_name = getattr(p, "model", "unknown")
        p_prompt = getattr(p, "total_prompt_eval_count", 0)
        p_eval = getattr(p, "total_eval_count", 0)
        breakdowns.append(f"{m_name}(prompt={p_prompt}, eval={p_eval})")

    return (
        f"[Metrics (multi-stage): total_prompt_tokens={total_prompt}, "
        f"total_eval_tokens={total_eval}, breakdown=[{', '.join(breakdowns)}]]"
    )


def _select_provider_for_task(
    task: str,
    router: ModelRouter,
    default_provider: object,
    ctx_override: int | None,
) -> object:
    """Pick the right OllamaProvider for this task's classified role."""
    scope = TaskScope(task)
    role = classify_task_role(task, scope)
    model_name = router.select(role)

    # Reuse existing provider if model matches (avoids re-creating)
    if model_name == getattr(default_provider, "model", None):
        return default_provider

    return ProviderFactory.create_ollama(model=model_name, num_ctx=ctx_override)


async def _run_task(
    task: str,
    router: ModelRouter,
    default_provider: object,
    hub: McpHub,
    memory: SessionMemory,
    cache: ResultCache,
    ctx_override: int | None,
) -> tuple[str, list[object]]:
    """Run a task using single-pass or multi-stage depending on complexity."""
    scope = TaskScope(task)
    if needs_multi_stage(task, scope):
        print(f"  [multi-stage: analysis -> coding -> verification]")
        state = TaskState(
            original_request=task,
            workspace=str(getattr(hub, "workspace", "")),
            read_only=getattr(hub, "read_only", True),
            stage=TaskStage.ANALYSIS,
        )
        answer = await run_multi_stage(
            task, router, hub, memory, cache,
            ctx_override=ctx_override,
            task_state=state,
        )
        return answer, state.used_providers
    else:
        task_provider = _select_provider_for_task(task, router, default_provider, ctx_override)
        answer = await run_agent_loop(task, task_provider, hub, memory, cache)
        return answer, [task_provider]


async def main():
    args = parse_args()

    if args.selftest:
        print("Self-test: V4 structure check complete.")
        return

    try:
        workspace_path = resolve_workspace(args.workspace)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Determine initial access mode:
    # Explicit CLI argument wins. Otherwise: external explicit workspace defaults to ro
    if args.mode:
        initial_read_only = (args.mode == "ro")
    elif args.workspace and Path(args.workspace).resolve() != Path.cwd().resolve():
        initial_read_only = True
    else:
        initial_read_only = False

    authorized_workspaces: set[Path] = {workspace_path}

    mode_label = "read-only (ro)" if initial_read_only else "read-write (rw)"
    print(f"AURA V4: Working on {workspace_path} [{mode_label}]")

    hub = McpHub(workspace_path, read_only=initial_read_only)
    await hub.start()

    # Initialize model registry and router
    registry = ModelRegistry()
    registry.refresh()
    router = ModelRouter(registry=registry)

    # CLI --model override locks all roles to that model
    cli_model = args.model
    if cli_model:
        router.set_override(cli_model)

    # Create default provider using the router's lightweight selection
    default_model = router.select(ModelRole.LIGHTWEIGHT) if not cli_model else cli_model
    provider = ProviderFactory.create_ollama(model=default_model, num_ctx=args.ctx)
    print(f"AURA V4: Model={provider.model}, num_ctx={provider.default_options.num_ctx}")
    if registry.models:
        print(f"AURA V4: {len(registry.models)} model(s) available: {', '.join(registry.available_names)}")
    if not provider.is_model_available():
        print(f"Warning: Model {provider.model} might not be pulled in Ollama.")

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    task_input = args.task or (" ".join(args.query).strip() if args.query else None)
    if task_input:
        try:
            scope = TaskScope(task_input)
            if needs_multi_stage(task_input, scope):
                print(f"\n[Multi-stage task detected]")
            print(f"\n[Thinking...]")
            answer, active_providers = await _run_task(
                task_input, router, provider, hub, memory, cache, args.ctx,
            )
            try:
                print(f"\nAURA: {answer}")
            except UnicodeEncodeError:
                safe_answer = answer.encode('ascii', 'replace').decode('ascii')
                print(f"\nAURA: {safe_answer}")
            print(f"\n{format_task_metrics(active_providers)}")
        finally:
            await hub.close()
        return

    print("Commands: '/workspace <path>', '/mode [ro|rw]', '/status', '/reset', '/help', 'exit'")
    try:
        while True:
            try:
                task = input("\nYou: ").strip()
            except (KeyboardInterrupt, EOFError):
                break

            if not task:
                continue

            # Command handling
            task_lower = task.lower()
            if task_lower in ("exit", "quit"):
                print("\nExiting AURA.")
                break

            if task_lower == "/help":
                print_help()
                continue

            if task_lower in ("/status", "/info"):
                print_status(hub, provider, memory, router)
                continue

            if task_lower == "/reset":
                memory.clear()
                cache.clear()
                print("Session memory and cache cleared.")
                continue

            if task_lower == "/models":
                print(f"\n{registry.describe()}")
                for role in ModelRole:
                    print(f"  {role.value:15s} -> {router.select(role)}")
                print()
                continue

            if task_lower.startswith("/mode"):
                parts = task.split(maxsplit=1)
                if len(parts) < 2:
                    curr = "ro" if hub.read_only else "rw"
                    print(f"Current mode: {curr}. Usage: /mode ro | /mode rw")
                    continue
                target_mode = parts[1].strip().lower()
                if target_mode == "rw":
                    if not hub.read_only:
                        print("Already in read-write (rw) mode.")
                    else:
                        if prompt_confirm(f"Enable read-write mode for '{hub.workspace}'? (Allows file creation and editing)", args.yes):
                            await hub.switch_workspace(hub.workspace, read_only=False)
                            print(f"[Access mode changed to: read-write (rw)]")
                        else:
                            print("[Mode switch canceled. Remaining in read-only mode]")
                elif target_mode == "ro":
                    if hub.read_only:
                        print("Already in read-only (ro) mode.")
                    else:
                        await hub.switch_workspace(hub.workspace, read_only=True)
                        print(f"[Access mode changed to: read-only (ro)]")
                else:
                    print("Unknown mode. Use: /mode ro | /mode rw")
                continue

            if task_lower.startswith(("/workspace", "/cd", "/open")):
                parts = task.split(maxsplit=1)
                if len(parts) < 2:
                    print(f"Current workspace: {hub.workspace}. Usage: /workspace <path>")
                    continue
                raw_target = parts[1].strip()
                try:
                    new_workspace = normalize_workspace_path(raw_target)
                except ValueError as err:
                    print(f"Error: {err}")
                    continue

                if new_workspace == hub.workspace:
                    print(f"Already in workspace: {new_workspace}")
                    continue

                # Permission approval check
                if new_workspace not in authorized_workspaces:
                    confirmed = prompt_confirm(
                        f"[SECURITY] Authorize access to external directory '{new_workspace}'? (Default: read-only)",
                        args.yes
                    )
                    if not confirmed:
                        print(f"[Authorization DENIED] Remaining in current workspace: {hub.workspace}")
                        continue
                    authorized_workspaces.add(new_workspace)

                print(f"Switching workspace to: {new_workspace} (read-only)...")
                await hub.switch_workspace(new_workspace, read_only=True)
                memory.clear()
                cache.clear()
                print(f"[Workspace switched to: {new_workspace}]")
                print(f"[Access mode: read-only (ro)]")
                print(f"[Connected tools: {len(hub.tools)} ({', '.join(sorted(hub.clients.keys()))})]")
                continue

            # Check if natural language prompt referenced an external directory
            referenced_dir = find_referenced_directory(task)
            if (
                referenced_dir is not None
                and referenced_dir != hub.workspace
                and hub.workspace not in referenced_dir.parents
            ):
                print(f"\n[NOTICE] Your query references external directory:\n  {referenced_dir}")
                if prompt_confirm(f"Authorize and switch workspace to '{referenced_dir}'? (Default: read-only)", args.yes):
                    authorized_workspaces.add(referenced_dir)
                    await hub.switch_workspace(referenced_dir, read_only=True)
                    memory.clear()
                    cache.clear()
                    print(f"[Workspace switched to: {referenced_dir}] (read-only)")

            # Run task (auto-selects single-pass or multi-stage)
            scope = TaskScope(task)
            if needs_multi_stage(task, scope):
                print(f"\n[Multi-stage task detected]")
            print(f"\n[Thinking...]")
            answer, active_providers = await _run_task(
                task, router, provider, hub, memory, cache, args.ctx,
            )
            try:
                print(f"\nAURA: {answer}")
            except UnicodeEncodeError:
                safe_answer = answer.encode('ascii', 'replace').decode('ascii')
                print(f"\nAURA: {safe_answer}")
            print(f"\n{format_task_metrics(active_providers)}")
    finally:
        await hub.close()


if __name__ == "__main__":
    asyncio.run(main())
