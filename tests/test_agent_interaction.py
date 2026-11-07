"""
TEST B: Complete Agent Interaction Verification Script
Follows PERMISSION_APPROVAL_PLAN specification.
Executes real local Ollama + live MCP servers via run_agent_loop.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent.resolve()
os.environ["AURA_WORKSPACE"] = str(WORKSPACE)
sys.path.insert(0, str(WORKSPACE))

from src.aura.core import McpHub, SessionMemory, ResultCache
from src.aura.core.providers import ProviderFactory
from src.aura.agents.loop import run_agent_loop


async def run_test_b():
    print("=" * 60)
    print("TEST B: COMPLETE AGENT INTERACTION VERIFICATION")
    print("=" * 60)

    # [1] Starting MCP Hub
    print("\n[1] Starting MCP Hub...")
    hub = McpHub(WORKSPACE)
    await hub.start()
    print(f"    Tools discovered: {len(hub.tools)}")

    # [2] Initializing Ollama provider
    print("\n[2] Initializing Ollama provider...")
    provider = ProviderFactory.create_ollama(model="qwen3.5:4b")
    available = provider.is_model_available()
    print(f"    Model available: {available}")
    if not available:
        print(f"    SKIP: Model {provider.model} not available in Ollama.")
        await hub.close()
        return

    # [3] Creating conversation memory and cache
    print("\n[3] Creating conversation session...")
    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    # [4] Running real agent task
    task = "Read src/aura/config/workspace.py and summarize in two sentences how workspace resolution works."
    print(f"\n[4] Running task through agent loop: '{task}'...")
    start_time = time.time()
    answer = await run_agent_loop(task, provider, hub, memory, cache)
    elapsed = time.time() - start_time
    print(f"    Response time: {elapsed:.2f}s")

    # [5] Validating result
    print("\n[5] Validating final answer...")
    print(f"    Final answer:\n    {answer[:300]}...")
    assert answer, "Final answer is empty"
    assert "workspace" in answer.lower() or "resolution" in answer.lower(), "Answer not grounded"
    assert "Maximum steps reached" not in answer, "Loop failed to synthesize answer"

    # [6] Cleaning up
    print("\n[6] Cleaning up MCP connections...")
    await hub.close()
    print("    PASS: Connections closed")

    print("\nTEST B RESULT: PASS")
    return True


if __name__ == "__main__":
    asyncio.run(run_test_b())
