"""
AURA V4 Controlled Context-Window Benchmark (8K vs 12K)
Measures latency, tool calls, token usage (prompt_eval_count), VRAM, and correctness.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"C:\Users\ariha\local-ai-lab\phase4-agent")
os.environ["AURA_WORKSPACE"] = str(WORKSPACE)
sys.path.insert(0, str(WORKSPACE))

from src.aura.core import McpHub, SessionMemory, ResultCache
from src.aura.core.providers import ProviderFactory
from src.aura.agents.loop import run_agent_loop


def get_vram_usage() -> str:
    """Get current GPU memory usage via nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            text=True
        ).strip()
        used, total = out.split(",")
        return f"{used.strip()} MiB / {total.strip()} MiB"
    except Exception as e:
        return f"VRAM query unavailable ({e})"


async def run_benchmark_trial(num_ctx: int, task: str):
    print("=" * 70)
    print(f"BENCHMARK TRIAL: num_ctx = {num_ctx}")
    print("=" * 70)
    print(f"Initial VRAM: {get_vram_usage()}")

    hub = McpHub(WORKSPACE)
    await hub.start()

    provider = ProviderFactory.create_ollama(model="qwen3.5:4b", num_ctx=num_ctx)
    memory = SessionMemory()
    cache = ResultCache(ttl_seconds=300)

    start_wall = time.strftime("%Y-%m-%d %H:%M:%S")
    start_t = time.perf_counter()

    error = None
    answer = None
    try:
        answer = await run_agent_loop(task, provider, hub, memory, cache)
    except Exception as e:
        error = str(e)

    elapsed_s = time.perf_counter() - start_t
    end_wall = time.strftime("%Y-%m-%d %H:%M:%S")

    vram_after = get_vram_usage()
    await hub.close()

    result_data = {
        "num_ctx": num_ctx,
        "model": provider.model,
        "task": task,
        "start_time": start_wall,
        "end_time": end_wall,
        "elapsed_seconds": round(elapsed_s, 2),
        "last_prompt_eval_tokens": provider.last_prompt_eval_count,
        "total_prompt_tokens": provider.total_prompt_eval_count,
        "eval_tokens_generated": provider.total_eval_count,
        "vram_after": vram_after,
        "success": bool(answer and "Maximum steps reached" not in answer and not error),
        "error": error,
        "answer_preview": answer[:300] if answer else "",
        "full_answer": answer or "",
    }

    print(f"End Time: {end_wall}")
    print(f"Elapsed Time: {elapsed_s:.2f}s")
    print(f"VRAM after: {vram_after}")
    print(f"Prompt Tokens (last turn): {provider.last_prompt_eval_count}")
    print(f"Total Prompt Tokens: {provider.total_prompt_eval_count}")
    print(f"Generated Tokens: {provider.total_eval_count}")
    print(f"Success: {result_data['success']}")
    print(f"Answer snippet: {result_data['answer_preview']}...")
    return result_data


async def main():
    task = "Read src/aura/config/workspace.py and src/aura/config/settings.py and explain how settings and workspace configuration work together."

    # 1. Baseline: 8K (8192)
    res_8k = await run_benchmark_trial(8192, task)

    # Cooldown between trials
    print("\nCooldown 3 seconds before 12K trial...\n")
    await asyncio.sleep(3)

    # 2. Test: 12K (12288)
    res_12k = await run_benchmark_trial(12288, task)

    print("\n" + "=" * 70)
    print("COMPARATIVE SUMMARY TABLE")
    print("=" * 70)
    print(f"{'Metric':<30} | {'8K Context':<20} | {'12K Context':<20}")
    print("-" * 75)
    print(f"{'Context passed to Ollama':<30} | {res_8k['num_ctx']:<20} | {res_12k['num_ctx']:<20}")
    print(f"{'Actual elapsed time':<30} | {res_8k['elapsed_seconds']}s{'':<15} | {res_12k['elapsed_seconds']}s{'':<15}")
    print(f"{'Prompt tokens (last turn)':<30} | {str(res_8k['last_prompt_eval_tokens']):<20} | {str(res_12k['last_prompt_eval_tokens']):<20}")
    print(f"{'Total prompt tokens':<30} | {str(res_8k['total_prompt_tokens']):<20} | {str(res_12k['total_prompt_tokens']):<20}")
    print(f"{'Tokens generated':<30} | {str(res_8k['eval_tokens_generated']):<20} | {str(res_12k['eval_tokens_generated']):<20}")
    print(f"{'VRAM usage':<30} | {res_8k['vram_after']:<20} | {res_12k['vram_after']:<20}")
    print(f"{'Task completed':<30} | {str(res_8k['success']):<20} | {str(res_12k['success']):<20}")
    print(f"{'Errors':<30} | {str(res_8k['error']):<20} | {str(res_12k['error']):<20}")

    # Save raw benchmark output to benchmarks/context_benchmark_results.json
    out_file = WORKSPACE / "benchmarks" / "context_benchmark_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"trial_8k": res_8k, "trial_12k": res_12k}, f, indent=2)
    print(f"\nSaved raw results to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
