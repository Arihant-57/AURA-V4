"""
MCP Hub: Persistent MCP server connection manager.

Servers are spawned ONCE at startup and reused across tasks. This avoids
the Windows process-spawn overhead that dominated MCP latency in earlier
versions. Tools are discovered once; per-task gating happens purely in
select_active_tools. Servers that fail mid-session are reconnected
lazily at the start of the next task.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

_WIN32_NO_WINDOW_PATCHED = False


def win32_subprocess_kwargs(detached: bool = False, hide_window: bool = True) -> dict[str, Any]:
    """Return Windows subprocess kwargs that suppress popup console windows cleanly."""
    if sys.platform != "win32":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if detached:
        # CREATE_NEW_PROCESS_GROUP works cleanly with CREATE_NO_WINDOW without console allocation
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    kwargs: dict[str, Any] = {"creationflags": flags}
    if hide_window and hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = si
    return kwargs


def _suppress_win32_consoles() -> None:
    """Ensure Windows MCP stdio subprocesses never flash popup console windows."""
    global _WIN32_NO_WINDOW_PATCHED
    if sys.platform != "win32" or _WIN32_NO_WINDOW_PATCHED:
        return
    try:
        import anyio

        orig_open = anyio.open_process
        win_kw = win32_subprocess_kwargs(hide_window=True)
        create_no_window = int(win_kw.get("creationflags", 0x08000000))

        async def _silent_open_process(*args: Any, **kwargs: Any) -> Any:
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0) or 0) | create_no_window
            if kwargs.get("startupinfo") is None and "startupinfo" in win_kw:
                kwargs["startupinfo"] = win_kw["startupinfo"]
            return await orig_open(*args, **kwargs)

        anyio.open_process = _silent_open_process

        try:
            import mcp.os.win32.utilities as _mcp_win32

            orig_fallback = getattr(_mcp_win32, "_create_windows_fallback_process", None)
            if callable(orig_fallback):
                orig_popen = subprocess.Popen

                async def _silent_fallback(*args: Any, **kwargs: Any) -> Any:
                    def _wrapped_popen(*p_args: Any, **p_kwargs: Any) -> Any:
                        p_kwargs["creationflags"] = int(p_kwargs.get("creationflags", 0) or 0) | create_no_window
                        if p_kwargs.get("startupinfo") is None and "startupinfo" in win_kw:
                            p_kwargs["startupinfo"] = win_kw["startupinfo"]
                        return orig_popen(*p_args, **p_kwargs)

                    _mcp_win32.subprocess.Popen = _wrapped_popen  # type: ignore[attr-defined]
                    try:
                        return await orig_fallback(*args, **kwargs)
                    finally:
                        _mcp_win32.subprocess.Popen = orig_popen  # type: ignore[attr-defined]

                _mcp_win32._create_windows_fallback_process = _silent_fallback  # type: ignore[attr-defined]
        except Exception:
            pass

        _WIN32_NO_WINDOW_PATCHED = True
    except Exception:
        pass


_suppress_win32_consoles()

from mcp import Client, StdioServerParameters

from ..config import get_settings


class McpHub:
    """
    Owns MCP server connections for the whole interactive session.
    """
    workspace: Path = Path(".")
    read_only: bool = False

    def __init__(
        self,
        workspace: Path,
        read_only: bool = False,
        enable_desktop: bool | None = None,
        enable_jarvis: bool | None = None,
        enable_browser: bool | None = None,
    ) -> None:
        _suppress_win32_consoles()
        self.workspace = workspace
        self.read_only = read_only
        self._stack: Optional[AsyncExitStack] = None
        self.clients: dict[str, Client] = {}
        self.tools: dict[str, Any] = {}
        self.tool_clients: dict[str, Client] = {}
        self.tool_schemas: dict[str, Any] = {}
        self.tool_server: dict[str, str] = {}
        self.broken: set[str] = set()
        use_desktop = (os.environ.get("AURA_ENABLE_DESKTOP_MCP", "0") == "1") if enable_desktop is None else enable_desktop
        use_jarvis = (os.environ.get("AURA_ENABLE_JARVIS_MCP", "0") == "1") if enable_jarvis is None else enable_jarvis
        use_browser = (os.environ.get("AURA_ENABLE_BROWSER_MCP", "0") == "1") if enable_browser is None else enable_browser
        names: list[str] = ["filesystem", "git"]
        if use_desktop:
            names.append("desktop")
        if use_jarvis:
            names.append("jarvis")
        if use_browser:
            names.append("browser")
        self.server_names: tuple[str, ...] = tuple(names)

    def _server_parameters(self, server_name: str) -> StdioServerParameters:
        """
        Build StdioServerParameters for the given server, injecting
        the workspace via AURA_WORKSPACE environment variable and
        read-only state via AURA_READ_ONLY.
        """
        settings = get_settings()
        script = Path(__file__).parent.parent / "mcp" / getattr(settings.mcp, f"{server_name}_server_filename")
        env = dict(os.environ)
        env["AURA_WORKSPACE"] = str(self.workspace)
        if self.read_only:
            env["AURA_READ_ONLY"] = "1"
        else:
            env.pop("AURA_READ_ONLY", None)
        return StdioServerParameters(
            command=sys.executable,
            args=[str(script)],
            env=env,
        )

    async def start(self) -> None:
        """Connect to all MCP servers."""
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for server_name in self.server_names:
            await self._connect(server_name)

    async def _connect(self, server_name: str) -> bool:
        assert self._stack is not None
        try:
            client = await asyncio.wait_for(
                self._stack.enter_async_context(Client(self._server_parameters(server_name))),
                timeout=15.0,
            )
            tools = await asyncio.wait_for(self._discover_all_tools(client), timeout=15.0)
        except Exception as exc:
            print(f"MCP SERVER FAILED: {server_name}: {exc}")
            self.broken.add(server_name)
            return False
        self.clients[server_name] = client
        self.broken.discard(server_name)
        print(f"MCP SERVER CONNECTED: {server_name} ({len(tools)} tools)")
        for tool in tools:
            owner = self.tool_server.get(tool.name)
            if owner is not None and owner != server_name:
                continue  # another server already provides this tool name
            self.tools[tool.name] = tool
            self.tool_clients[tool.name] = client
            self.tool_schemas[tool.name] = (
                getattr(tool, "input_schema", None)
                or getattr(tool, "inputSchema", None)
            )
            self.tool_server[tool.name] = server_name
        return True

    async def _discover_all_tools(self, client: Client) -> list[Any]:
        all_tools = []
        cursor = None
        while True:
            result = await client.list_tools() if cursor is None else await client.list_tools(cursor=cursor)
            all_tools.extend(result.tools)
            cursor = result.next_cursor
            if not cursor:
                break
        return all_tools

    def mark_broken(self, server_name: str) -> None:
        if server_name:
            self.broken.add(server_name)

    async def heal(self) -> None:
        """Lazily reconnect any broken servers at the start of a task."""
        for server_name in self.server_names:
            if server_name in self.broken:
                await self._connect(server_name)

    async def close(self) -> None:
        if self._stack:
            await self._stack.__aexit__(None, None, None)
            self._stack = None
        self.clients.clear()
        self.tools.clear()
        self.tool_clients.clear()
        self.tool_schemas.clear()
        self.tool_server.clear()
        self.broken.clear()

    async def switch_workspace(self, new_workspace: Path, read_only: bool = True) -> bool:
        """
        Safely switch active workspace and re-initialize MCP servers.

        Closes existing server processes cleanly, points workspace to the new
        directory, sets read_only mode, and restarts all servers.
        """
        resolved = new_workspace.resolve()
        if (
            self._stack is not None
            and not self.broken
            and resolved == self.workspace
            and self.read_only == read_only
        ):
            return len(self.clients) > 0
        await self.close()
        self.workspace = resolved
        self.read_only = read_only
        await self.start()
        return len(self.clients) > 0
