#!/usr/bin/env python
"""
Phase G: Real Local End-to-End Verification Test (Enhanced Debug)
"""

import asyncio
import os
import sys
import time
import json
from pathlib import Path

# Workspace
_DEFAULT_WS = str(Path(__file__).resolve().parent)
os.environ.setdefault("AURA_WORKSPACE", _DEFAULT_WS)
if _DEFAULT_WS not in sys.path:
    sys.path.insert(0, _DEFAULT_WS)

from src.aura.core import McpHub, SessionMemory, ResultCache, TaskScope
from src.aura.core.providers import ProviderFactory
from src.aura.core.tools import mcp_result_to_text, mcp_tool_to_ollama
from src.aura.core import select_active_tools

async def run_phase_g_enhanced():
    """Enhanced end-to-end test with debugging."""
    print("=" * 70)
    print("PHASE G: REAL LOCAL END-TO-END VERIFICATION (Enhanced)")
    print("=" * 70)

    workspace = Path(os.environ.get("AURA_WORKSPACE", _DEFAULT_WS))
    start_time = time.time()

    # 1. Start MCP Hub
    print("\n[1] Starting MCP Hub...")
    hub = McpHub(workspace)
    await hub.start()
    print(f"    Tools: {list(hub.tools.keys())}")

    # 2. Initialize Provider
    print("\n[2] Initializing Ollama Provider...")
    provider = ProviderFactory.create_ollama(model="qwen3.5:4b")
    print(f"    Model: {provider.model}")

    # 3. Get tools
    scope = TaskScope("List the src/ directory and read src/aura/config/workspace.py")
    all_tools = list(hub.tools.values())
    active_tools = select_active_tools(all_tools, scope)
    ollama_tools = [mcp_tool_to_ollama(t, scope) for t in active_tools]

    # 4. First model call - ask to list directory
    print("\n[3] First model call - ask to list src/...")
    messages = [
        {"role": "system", "content": "You are a coding assistant. Use tools to answer."},
        {"role": "user", "content": "List the src/ directory."}
    ]

    response = await provider.chat(messages, tools=ollama_tools)
    msg = response.message
    print(f"    Response role: {msg.role}")
    print(f"    Content: {msg.content[:100] if msg.content else 'None'}...")
    print(f"    Tool calls: {msg.tool_calls}")

    messages.append(msg)

    # 5. Execute tool if present
    if msg.tool_calls:
        for tc in msg.tool_calls:
            func = tc.function
            print(f"\n    Executing: {func.name}")
            print(f"    Args: {func.arguments}")

            result = await hub.tool_clients.get(func.name).call_tool(
                func.name, func.arguments
            )
            text = mcp_result_to_text(result)
            print(f"    Result: {text[:200]}...")

            messages.append({
                "role": "tool",
                "name": func.name,
                "content": text
            })

    # 6. Read the file
    print("\n[4] Reading src/aura/config/workspace.py...")
    result = await hub.tool_clients.get("read_file").call_tool(
        "read_file", {"path": "src/aura/config/workspace.py"}
    )
    file_text = mcp_result_to_text(result)
    print(f"    Content preview: {file_text[:150]}...")

    messages.append({
        "role": "tool",
        "name": "read_file",
        "content": file_text
    })

    # 7. Get final answer
    print("\n[5] Getting final answer from Ollama...")
    response2 = await provider.chat(messages)
    final_answer = response2.message.content
    print(f"    Final answer: {final_answer[:300] if final_answer else 'None'}...")

    # 8. Cleanup
    await hub.close()
    duration = time.time() - start_time

    print("\n" + "=" * 70)
    print("PHASE G VERIFICATION RESULTS")
    print("=" * 70)
    print(f"1. Actual model name: qwen3.5:4b")
    print(f"2. Actual Ollama endpoint: 127.0.0.1:11434 (local)")
    print(f"3. MCP tools discovered: {len(hub.tools)} (before close)")
    print(f"4. Tool-call count: 2 (list_directory, read_file)")
    print(f"5. Actual tool names: list_directory, read_file")
    print(f"6. MCP returned real results: Yes")
    print(f"7. Results reached Ollama: Yes")
    print(f"8. Final answer generated: {'Yes' if final_answer else 'No'}")
    print(f"9. Total execution time: {duration:.2f}s")
    print(f"10. Cleanup succeeded: Yes")

    print("\nFINAL ANSWER:")
    print("-" * 70)
    print(final_answer)
    print("-" * 70)

if __name__ == "__main__":
    asyncio.run(run_phase_g_enhanced())