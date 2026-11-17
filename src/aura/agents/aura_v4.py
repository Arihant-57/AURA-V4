"""
AURA V4 — Functional execution engine with interactive workspace & permission control.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..config import get_settings, resolve_workspace, load_aura_env
from ..config.workspace import normalize_workspace_path, find_referenced_directory
from ..core import McpHub, SessionMemory, ResultCache
from ..core.providers import ProviderFactory
from ..core.model_registry import ModelRegistry, ModelRole
from ..core.model_router import (
    ModelRouter, classify_task_role, needs_multi_stage, is_heavy_task, TaskState, TaskStage,
)
from ..core.scope import TaskScope, normalize_agent_role, parse_agent_prefix
from ..core.orchestrator import run_multi_stage
from .loop import run_agent_loop


def parse_args():
    parser = argparse.ArgumentParser(description="AURA V4 AI Agent")
    parser.add_argument("--workspace", "-w", type=str, help="Workspace path")
    parser.add_argument("--mode", choices=["ro", "rw"], default=None, help="Access mode: ro (read-only) or rw (read-write)")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-confirm authorization prompts")
    parser.add_argument("--model", "-m", type=str, help="Model name override")
    parser.add_argument("--low-vram", action="store_true", help="Lock all pipeline stages to qwen2.5-coder:7b to eliminate model swapping and minimize VRAM thrashing")
    parser.add_argument("--ctx", "-c", type=int, help="Context window size override (num_ctx)")
    parser.add_argument("--task", "-t", type=str, help="Single task to execute non-interactively")
    parser.add_argument("query", nargs="*", help="Positional task query")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--selftest", action="store_true", help="Run sanity check")
    parser.add_argument("--web", action="store_true", help="Launch local web UI on 127.0.0.1")
    parser.add_argument("--overlay", action="store_true", help="Also launch Windows Dynamic Island floating pill with --web")
    parser.add_argument("--port", type=int, default=8000, help="Port for --web server (default: 8000)")
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
    scope: TaskScope | None = None,
) -> object:
    """Pick the right OllamaProvider for this task's classified role."""
    task_scope = scope if scope is not None else TaskScope(task)
    role = classify_task_role(task, task_scope)
    model_name = router.select(role)

    # Reuse existing provider if model matches (avoids re-creating)
    if model_name == getattr(default_provider, "model", None):
        return default_provider

    return ProviderFactory.create_for_model(model=model_name, num_ctx=ctx_override)


async def _run_task(
    task: str,
    router: ModelRouter,
    default_provider: object,
    hub: McpHub,
    memory: SessionMemory,
    cache: ResultCache,
    ctx_override: int | None,
    unload_on_complete: bool = False,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    agent_role: str | None = None,
    steer_queue: Any = None,
    get_pending_steer: Callable[[], Any] | None = None,
) -> tuple[str, list[object]]:
    """Run a task using single-pass, specialist sub-agent, multi-stage orchestrator, or heavy cloud routing."""
    routing_task = task.split("[Current Request]\n", 1)[-1] if "[Current Request]\n" in task else task
    prefixed_agent, _ = parse_agent_prefix(routing_task)
    effective_agent = normalize_agent_role(agent_role) if agent_role else (prefixed_agent or "auto")
    scope = TaskScope(routing_task, agent_role=effective_agent)

    use_cloud_heavy = (
        getattr(router, "_override", None) is None
        and os.environ.get("AURA_CLOUD_FOR_HEAVY") == "1"
        and is_heavy_task(routing_task, scope)
        and bool(ProviderFactory.available_cloud_models())
    )
    if use_cloud_heavy:
        cloud_provider = ProviderFactory.create_cloud("cloud:auto", num_ctx=ctx_override)
        if cloud_provider.is_model_available():
            if on_event is not None:
                try:
                    res = on_event({
                        "event": "stage",
                        "stage": "cloud",
                        "agent": effective_agent,
                        "model": cloud_provider.model,
                        "message": f"Heavy task ({effective_agent}): using {cloud_provider.model} (local fallback armed)...",
                    })
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    pass
            try:
                cloud_state = (
                    TaskState(
                        original_request=routing_task,
                        workspace=str(getattr(hub, "workspace", "")),
                        read_only=getattr(hub, "read_only", True),
                        active_agent=effective_agent,
                    )
                    if effective_agent != "auto"
                    else None
                )
                ans = await run_agent_loop(
                    task, cloud_provider, hub, memory, cache,
                    task_state=cloud_state,
                    on_event=on_event,
                    steer_queue=steer_queue,
                    get_pending_steer=get_pending_steer,
                )
                if not ans.startswith(("Error: Cannot connect", "Error: Cloud API", "Error: Failed to get model response")):
                    return ans, [cloud_provider]
            except Exception:
                pass
            if on_event is not None:
                try:
                    res = on_event({
                        "event": "stage",
                        "stage": "fallback",
                        "message": "Cloud unavailable — falling back to local Ollama pipeline...",
                    })
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    pass

    if effective_agent == "auto" and needs_multi_stage(routing_task, scope):
        print(f"  [multi-stage: analysis -> coding -> verification]")
        state = TaskState(
            original_request=routing_task,
            workspace=str(getattr(hub, "workspace", "")),
            read_only=getattr(hub, "read_only", True),
            stage=TaskStage.ANALYSIS,
        )
        answer = await run_multi_stage(
            task, router, hub, memory, cache,
            ctx_override=ctx_override,
            task_state=state,
            on_event=on_event,
        )
        return answer, state.used_providers
    else:
        task_provider = _select_provider_for_task(routing_task, router, default_provider, ctx_override, scope=scope)
        stage_label = f"Running {effective_agent}" if effective_agent != "auto" else "Running single-pass"
        if on_event is not None:
            try:
                res = on_event({
                    "event": "stage",
                    "stage": "single",
                    "agent": effective_agent,
                    "model": getattr(task_provider, "model", ""),
                    "message": f"{stage_label} ({getattr(task_provider, 'model', 'model')})...",
                })
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass
        specialist_state = (
            TaskState(
                original_request=routing_task,
                workspace=str(getattr(hub, "workspace", "")),
                read_only=getattr(hub, "read_only", True),
                active_agent=effective_agent,
            )
            if effective_agent != "auto"
            else None
        )
        try:
            answer = await run_agent_loop(
                task, task_provider, hub, memory, cache,
                task_state=specialist_state,
                on_event=on_event,
                steer_queue=steer_queue,
                get_pending_steer=get_pending_steer,
            )
            return answer, [task_provider]
        finally:
            if unload_on_complete and hasattr(task_provider, "unload"):
                task_provider.unload()


def _ensure_gui_stdio() -> None:
    """Under pythonw.exe on Windows, sys.stdout/stderr may be None; redirect to log safely."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = Path.home() / ".aura"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / "aura_app.log").open("a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file
    except Exception:
        pass


def _is_aura_server_running(url: str) -> bool:
    """Return True if an active AURA V4 server is already listening at *url*."""
    try:
        import httpx

        resp = httpx.get(f"{url}/api/status", timeout=1.2)
        return resp.status_code == 200 and "mode" in resp.json()
    except Exception:
        return False


async def main():
    _ensure_gui_stdio()
    load_aura_env()
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

    if args.web:
        import threading
        import webbrowser
        import uvicorn
        from ..core.providers import ensure_ollama_running
        from ..server import create_app

        url = f"http://127.0.0.1:{args.port}"
        if not os.environ.get("PYTEST_CURRENT_TEST") and _is_aura_server_running(url):
            try:
                import httpx

                httpx.post(f"{url}/api/overlay/show", json={"expand": True}, timeout=2.0)
            except Exception:
                pass
            try:
                from ..overlay import signal_overlay_wake_event

                signal_overlay_wake_event()
            except Exception:
                pass
            print(f"AURA V4 is already running at {url} — surfaced Dynamic Island overlay.")
            return

        if not os.environ.get("PYTEST_CURRENT_TEST"):
            ensure_ollama_running(timeout=8.0)

        explicit_ws = (
            workspace_path
            if (args.workspace or os.environ.get("AURA_WORKSPACE") or args.overlay or args.mode == "rw")
            else None
        )
        if args.mode in ("ro", "rw"):
            web_read_only = args.mode == "ro"
        elif explicit_ws is not None:
            web_read_only = initial_read_only
        elif args.overlay:
            web_read_only = False
        else:
            web_read_only = True
        web_mode_label = "read-only (ro)" if web_read_only else "read-write (rw)"

        print(f"AURA V4 Web UI: {url} (workspace={explicit_ws or 'none'}, mode={web_mode_label})")
        if os.environ.get("AURA_NO_BROWSER") != "1":
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()

        server_holder: dict[str, Any] = {}
        overlay_holder: dict[str, Any] = {"proc": None}

        def _ensure_overlay_running() -> bool:
            cur = overlay_holder.get("proc")
            if cur is not None and cur.poll() is None:
                return False
            import subprocess
            from ..core.mcp_hub import win32_subprocess_kwargs

            py_exe = sys.executable
            if sys.platform == "win32":
                pythonw_candidate = Path(sys.executable).with_name("pythonw.exe")
                if pythonw_candidate.is_file():
                    py_exe = str(pythonw_candidate)
            extra = win32_subprocess_kwargs(hide_window=False)
            overlay_holder["proc"] = subprocess.Popen(
                [py_exe, "-m", "src.aura.overlay", "--server-url", url],
                cwd=str(Path(__file__).resolve().parents[3]),
                **extra,
            )
            return True

        def _request_shutdown() -> None:
            srv = server_holder.get("server")
            if srv is not None:
                srv.should_exit = True
            # Guarantee clean process exit if Windows stdio MCP transports keep keep-alive handles open
            t = threading.Timer(2.0, lambda: os._exit(0))
            t.daemon = True
            t.start()

        app = create_app(
            workspace=explicit_ws,
            read_only=web_read_only,
            model_override=args.model,
            low_vram=args.low_vram,
            ctx_override=args.ctx,
            shutdown_fn=_request_shutdown,
            ensure_overlay_fn=_ensure_overlay_running,
        )
        watcher_task = None
        if args.overlay:
            _ensure_overlay_running()
        try:
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="info",
                use_colors=bool(sys.stderr is not None and hasattr(sys.stderr, "isatty") and sys.stderr.isatty()),
                timeout_graceful_shutdown=2.0,
            )
            server = uvicorn.Server(config)
            server_holder["server"] = server

            async def _watch_overlay() -> None:
                while (
                    overlay_holder.get("proc") is not None
                    and overlay_holder["proc"].poll() is None
                    and not server.should_exit
                ):
                    await asyncio.sleep(0.4)
                if overlay_holder.get("proc") is not None and overlay_holder["proc"].poll() is not None:
                    server.should_exit = True

            if overlay_holder.get("proc") is not None:
                watcher_task = asyncio.create_task(_watch_overlay())
            await server.serve()
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
            cur_proc = overlay_holder.get("proc")
            if cur_proc is not None and cur_proc.poll() is None:
                cur_proc.terminate()
                try:
                    cur_proc.wait(timeout=2.0)
                except Exception:
                    cur_proc.kill()
        return

    print(f"AURA V4: Working on {workspace_path} [{mode_label}]")

    hub = McpHub(workspace_path, read_only=initial_read_only)
    await hub.start()

    # Initialize model registry and router
    registry = ModelRegistry()
    registry.refresh()
    router = ModelRouter(registry=registry)

    # CLI --model or --low-vram override locks all roles to that model
    cli_model = args.model
    if args.low_vram:
        cli_model = "qwen2.5-coder:7b"
    if cli_model:
        router.set_override(cli_model)

    # Create default provider using the router's lightweight selection
    default_model = router.select(ModelRole.LIGHTWEIGHT) if not cli_model else cli_model
    provider = ProviderFactory.create_for_model(model=default_model, num_ctx=args.ctx)
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
