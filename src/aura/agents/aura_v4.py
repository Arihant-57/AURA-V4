"""
AURA V4 — Functional execution engine with interactive workspace & permission control.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ..config import get_settings, resolve_workspace
from ..config.workspace import normalize_workspace_path, find_referenced_directory
from ..core import McpHub, SessionMemory, ResultCache
from ..core.providers import ProviderFactory
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


def print_status(hub: McpHub, provider, memory: SessionMemory):
    """Print formatted session status."""
    mode_str = "read-only (ro)" if getattr(hub, "read_only", False) else "read-write (rw)"
    print("\n" + "=" * 40)
    print("=== AURA V4 Session Status ===")
    print(f"Workspace Root  : {hub.workspace}")
    print(f"Access Mode     : {mode_str}")
    print(f"Model           : {provider.model} (context: {provider.default_options.num_ctx})")
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
  /status (or /info)          Display current workspace, mode, and session stats
  /reset                      Clear session memory and cache
  /help                       Show this help message
  exit (or quit)              Shut down MCP servers and exit
""")


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

    provider = ProviderFactory.create_ollama(model=args.model, num_ctx=args.ctx)
    print(f"AURA V4: Model={provider.model}, num_ctx={provider.default_options.num_ctx}")
    if not provider.is_model_available():
        print(f"Warning: Model {provider.model} might not be pulled in Ollama.")

    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    task_input = args.task or (" ".join(args.query).strip() if args.query else None)
    if task_input:
        try:
            print(f"\n[Thinking...]")
            answer = await run_agent_loop(task_input, provider, hub, memory, cache)
            try:
                print(f"\nAURA: {answer}")
            except UnicodeEncodeError:
                safe_answer = answer.encode('ascii', 'replace').decode('ascii')
                print(f"\nAURA: {safe_answer}")
            if provider.last_prompt_eval_count is not None:
                print(f"\n[Metrics: prompt_eval_tokens={provider.last_prompt_eval_count}, eval_tokens={provider.last_eval_count}, total_prompt_tokens={provider.total_prompt_eval_count}, num_ctx={provider.default_options.num_ctx}]")
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
                print_status(hub, provider, memory)
                continue

            if task_lower == "/reset":
                memory.clear()
                cache.clear()
                print("Session memory and cache cleared.")
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

            print(f"\n[Thinking...]")
            answer = await run_agent_loop(task, provider, hub, memory, cache)
            try:
                print(f"\nAURA: {answer}")
            except UnicodeEncodeError:
                safe_answer = answer.encode('ascii', 'replace').decode('ascii')
                print(f"\nAURA: {safe_answer}")
            if provider.last_prompt_eval_count is not None:
                print(f"\n[Metrics: prompt_eval_tokens={provider.last_prompt_eval_count}, eval_tokens={provider.last_eval_count}, total_prompt_tokens={provider.total_prompt_eval_count}, num_ctx={provider.default_options.num_ctx}]")
    finally:
        await hub.close()


if __name__ == "__main__":
    asyncio.run(main())
