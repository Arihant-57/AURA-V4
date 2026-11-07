"""
TEST A: Direct MCP Connectivity Verification Script
Follows PERMISSION_APPROVAL_PLAN specification.
"""

import asyncio
import os
import sys
from pathlib import Path

# Workspace configuration
WORKSPACE = Path(__file__).parent.parent.resolve()
os.environ["AURA_WORKSPACE"] = str(WORKSPACE)
sys.path.insert(0, str(WORKSPACE))

from src.aura.core import McpHub
from src.aura.core.tools import mcp_result_to_text


async def run_test_a():
    print("=" * 60)
    print("TEST A: DIRECT MCP CONNECTIVITY VERIFICATION")
    print("=" * 60)

    # [1] Starting MCP Hub
    print("\n[1] Starting MCP Hub...")
    hub = McpHub(WORKSPACE)
    await hub.start()
    tool_names = sorted(list(hub.tools.keys()))
    print(f"    Tools: {tool_names}")
    assert "read_file" in tool_names, "read_file tool missing"
    assert "list_directory" in tool_names, "list_directory tool missing"

    # [2] Listing workspace directory
    print("\n[2] Listing workspace directory...")
    fs_client = hub.tool_clients.get("list_directory")
    assert fs_client is not None, "list_directory client missing"
    list_res = await fs_client.call_tool("list_directory", {"path": "."})
    list_text = mcp_result_to_text(list_res)
    if "src" in list_text:
        print("    PASS: Found 'src' directory")
    else:
        print(f"    FAIL: 'src' not found in listing:\n{list_text}")
        await hub.close()
        sys.exit(1)

    # [3] Reading config/workspace.py
    print("\n[3] Reading src/aura/config/workspace.py...")
    read_client = hub.tool_clients.get("read_file")
    assert read_client is not None, "read_file client missing"
    read_res = await read_client.call_tool("read_file", {"path": "src/aura/config/workspace.py"})
    read_text = mcp_result_to_text(read_res)
    if "WorkspaceConfig" in read_text:
        print("    PASS: workspace.py content verified")
    else:
        print(f"    FAIL: WorkspaceConfig not found in file:\n{read_text[:200]}")
        await hub.close()
        sys.exit(1)

    # [4] Verifying path traversal protection
    print("\n[4] Verifying path traversal protection...")
    traversal_res = await read_client.call_tool("read_file", {"path": "../"})
    traversal_text = mcp_result_to_text(traversal_res)
    is_err = getattr(traversal_res, "is_error", False) or "ERROR" in traversal_text
    if is_err:
        print("    PASS: Traversal blocked")
    else:
        print(f"    FAIL: Traversal was not blocked:\n{traversal_text}")
        await hub.close()
        sys.exit(1)

    # [5] Cleaning up MCP connections
    print("\n[5] Cleaning up MCP connections...")
    await hub.close()
    print("    PASS: Connections closed")

    print("\nTEST A RESULT: PASS")
    return True


if __name__ == "__main__":
    asyncio.run(run_test_a())
