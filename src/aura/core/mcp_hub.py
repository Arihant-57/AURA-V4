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
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import Client, StdioServerParameters

from ..config import get_settings


class McpHub:
    """
    Owns MCP server connections for the whole interactive session.
    """
    workspace: Path = Path(".")
    read_only: bool = False

    def __init__(self, workspace: Path, read_only: bool = False) -> None:
        self.workspace = workspace
        self.read_only = read_only
        self._stack: Optional[AsyncExitStack] = None
        self.clients: dict[str, Client] = {}
        self.tools: dict[str, Any] = {}
        self.tool_clients: dict[str, Client] = {}
        self.tool_schemas: dict[str, Any] = {}
        self.tool_server: dict[str, str] = {}
        self.broken: set[str] = set()
        self.server_names: tuple[str, ...] = ("filesystem", "git")

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
            client = await self._stack.enter_async_context(
                Client(self._server_parameters(server_name))
            )
            tools = await self._discover_all_tools(client)
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
        await self.close()
        self.workspace = new_workspace.resolve()
        self.read_only = read_only
        await self.start()
        return len(self.clients) > 0
