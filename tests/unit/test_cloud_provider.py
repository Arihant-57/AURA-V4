"""Unit tests for CloudProvider, persistent .env config, heavy-task detection, and offline fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.aura.config.settings import load_aura_env, save_env_vars
from src.aura.core.model_registry import ModelCapability, ModelRegistry
from src.aura.core.model_router import ModelRouter, is_heavy_task
from src.aura.core.providers import CloudProvider, ProviderFactory, _to_openai_messages
from src.aura.core.scope import TaskScope


def test_env_persistence_across_restarts(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("AURA_CLOUD_FOR_HEAVY", raising=False)

    saved = save_env_vars({"GROQ_API_KEY": "gsk_test_123", "AURA_CLOUD_FOR_HEAVY": "1"}, env_path=env_file)
    assert saved == env_file
    assert env_file.exists()

    # Clear process env to simulate reboot, then reload from SSD
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("AURA_CLOUD_FOR_HEAVY", raising=False)
    load_aura_env([env_file])

    assert "cloud:groq/llama-3.3-70b-versatile" in ProviderFactory.available_cloud_models()


def test_heavy_task_classification():
    simple = "Write a 3-line Python function to add two numbers."
    assert is_heavy_task(simple, TaskScope(simple)) is False

    medium = "Fix the typo in README.md"
    assert is_heavy_task(medium, TaskScope(medium)) is False

    heavy = (
        "Refactor the entire authentication architecture across src/auth.py, "
        "src/server.py, and tests/test_auth.py to enforce RBAC and update tests"
    )
    assert is_heavy_task(heavy, TaskScope(heavy)) is True


def test_openai_message_and_tool_call_translation():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read app.py"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "app.py"}}}]},
        {"role": "tool", "name": "read_file", "content": "print('hi')"},
    ]
    out = _to_openai_messages(msgs)
    assert out[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "app.py"}
    assert out[3]["role"] == "tool"
    assert out[3]["tool_call_id"] == out[2]["tool_calls"][0]["id"]


@pytest.mark.asyncio
async def test_cloud_provider_chat_and_missing_key(monkeypatch):
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AURA_CLOUD_API_KEY", "FREELLMAPI_KEY"):
        monkeypatch.delenv(k, raising=False)

    unconfigured = CloudProvider("cloud:groq", api_key="")
    assert unconfigured.is_model_available() is False
    with pytest.raises(ValueError, match="No free cloud LLM API key configured"):
        await unconfigured.chat([{"role": "user", "content": "hi"}])

    provider = CloudProvider("cloud:groq/llama-3.3-70b-versatile", api_key="gsk_fake_key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                }],
            }
        }],
        "usage": {"prompt_tokens": 120, "completion_tokens": 25},
    }

    with patch("src.aura.core.providers.httpx.AsyncClient") as mock_client_cls:
        client_inst = AsyncMock()
        client_inst.post.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = client_inst

        res = await provider.chat([{"role": "user", "content": "inspect main.py"}], tools=[{"type": "function", "function": {"name": "read_file"}}])

    assert res.message.tool_calls[0].function.name == "read_file"
    assert res.message.tool_calls[0].function.arguments == {"path": "main.py"}
    assert provider.total_prompt_eval_count == 120
    assert provider.total_eval_count == 25


@pytest.mark.asyncio
async def test_heavy_task_falls_back_to_local_when_cloud_offline(monkeypatch):
    from src.aura.agents.aura_v4 import _run_task
    from src.aura.core.memory import SessionMemory

    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_key")
    monkeypatch.setenv("AURA_CLOUD_FOR_HEAVY", "1")

    reg = ModelRegistry()
    reg.models["qwen3.5:4b"] = ModelCapability(name="qwen3.5:4b", parameter_count=4.0, context_length=32768, supports_tools=True)
    reg._refreshed = True
    router = ModelRouter(registry=reg)

    hub = MagicMock(workspace="/workspace", read_only=True)
    memory = SessionMemory()
    cache = MagicMock()
    provider = MagicMock(model="qwen3.5:4b")

    heavy_task = "Refactor the entire backend architecture across server.py, loop.py, and orchestrator.py with tests"

    with patch("src.aura.agents.aura_v4.run_agent_loop", AsyncMock(side_effect=httpx.ConnectError("offline"))):
        with patch("src.aura.agents.aura_v4.run_multi_stage", AsyncMock(return_value="LOCAL FALLBACK REPORT")) as mock_local:
            ans, _ = await _run_task(heavy_task, router, provider, hub, memory, cache, None)

    assert ans == "LOCAL FALLBACK REPORT"
    assert mock_local.called


def test_freellm_gateway_empty_model_defaults_to_auto(monkeypatch, tmp_path: Path):
    from src.aura.core.providers import resolve_cloud_target

    monkeypatch.setenv("FREELLMAPI_ENDPOINT", "http://127.0.0.1:31415/v1")
    monkeypatch.setenv("FREELLMAPI_KEY", "flm_test_key")
    monkeypatch.delenv("FREELLMAPI_MODEL", raising=False)
    monkeypatch.delenv("AURA_CLOUD_MODEL", raising=False)

    preset, url, key, model = resolve_cloud_target("cloud:custom/auto")
    assert preset == "custom"
    assert url == "http://127.0.0.1:31415/v1"
    assert key == "flm_test_key"
    assert model == "auto"
    assert "cloud:custom/auto" in ProviderFactory.available_cloud_models()


@pytest.mark.asyncio
async def test_cloud_auto_fails_over_from_gemini_429_to_freellm(monkeypatch):
    from src.aura.core.providers import resolve_cloud_candidates

    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AURA_CLOUD_API_KEY", "AURA_CLOUD_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gm_primary_key")
    monkeypatch.setenv("FREELLMAPI_ENDPOINT", "https://backup-freellm.example/v1")
    monkeypatch.setenv("FREELLMAPI_KEY", "flm_backup_key")
    monkeypatch.setenv("FREELLMAPI_MODEL", "auto")

    cands = resolve_cloud_candidates("cloud:auto")
    assert [c[0] for c in cands] == ["gemini", "custom"]

    provider = CloudProvider("cloud:auto")
    assert provider.preset == "gemini"

    resp_429 = MagicMock(status_code=429, text="rate limit exceeded")
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"choices": [{"message": {"content": "backup ok"}}]}

    with patch("src.aura.core.providers.httpx.AsyncClient") as mock_client_cls:
        client_inst = AsyncMock()
        client_inst.post.side_effect = [resp_429, resp_ok]
        mock_client_cls.return_value.__aenter__.return_value = client_inst

        out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.message.content == "backup ok"
    assert provider.preset == "custom"
    assert "gemini->429" in provider.failover_events
    assert client_inst.post.call_count == 2


@pytest.mark.asyncio
async def test_gemini_default_38_flash_and_503_failover_to_36_flash(monkeypatch):
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AURA_CLOUD_API_KEY", "AURA_CLOUD_BASE_URL", "FREELLMAPI_ENDPOINT", "FREELLMAPI_KEY", "GEMINI_MODEL", "AURA_GEMINI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gm_key")

    with patch("src.aura.config.settings.load_aura_env", return_value=None):
        avail = ProviderFactory.available_cloud_models()
        assert avail == ["cloud:gemini/gemini-3.8-flash"]

        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash-custom")
        assert ProviderFactory.available_cloud_models() == ["cloud:gemini/gemini-3.8-flash-custom"]
        monkeypatch.delenv("GEMINI_MODEL", raising=False)

        provider = CloudProvider("cloud:gemini")
    assert provider.api_model == "gemini-3.8-flash"

    resp_503 = MagicMock(status_code=503, text="high demand")
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"choices": [{"message": {"content": "gemini 3.6 ok"}}]}

    with patch("src.aura.core.providers.httpx.AsyncClient") as mock_client_cls:
        client_inst = AsyncMock()
        client_inst.post.side_effect = [resp_503, resp_503, resp_ok]
        mock_client_cls.return_value.__aenter__.return_value = client_inst

        out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.message.content == "gemini 3.6 ok"
    assert provider.api_model == "gemini-3.6-flash"
    assert "gemini->503" in provider.failover_events
    assert client_inst.post.call_args_list[1].kwargs["json"]["model"] == "gemini-3-flash-preview"
    assert client_inst.post.call_args_list[2].kwargs["json"]["model"] == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_gemini_thought_signature_preserved_across_agent_loop_turns(tmp_path: Path):
    from mcp.types import TextContent, Tool
    from src.aura.agents.loop import run_agent_loop
    from src.aura.core import McpHub, ResultCache, SessionMemory
    from src.aura.core.providers import CloudToolCall
    from ollama import ChatResponse, Message

    provider = CloudProvider("cloud:gemini/gemini-3.8-flash", api_key="gm_key", auto_failover=False)

    resp1 = MagicMock(status_code=200)
    resp1.json.return_value = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_gem_1",
                    "type": "function",
                    "extra_content": {"google": {"thought_signature": "sig_token_xyz"}},
                    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                }],
            }
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp2 = MagicMock(status_code=200)
    resp2.json.return_value = {
        "choices": [{"message": {"content": "Directory listed cleanly."}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 6},
    }

    hub = MagicMock(spec=McpHub)
    hub.read_only = True
    hub.workspace = tmp_path
    hub.broken = set()
    hub.tools = {
        "list_dir": Tool(
            name="list_dir",
            description="List dir",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    }
    hub.tool_schemas = {"list_dir": {"type": "object", "properties": {"path": {"type": "string"}}}}
    hub.tool_clients = {"list_dir": object()}
    hub.call_tool = AsyncMock(return_value=MagicMock(content=[TextContent(type="text", text="README.md")], is_error=False))

    with patch("src.aura.core.providers.httpx.AsyncClient") as mock_client_cls:
        client_inst = AsyncMock()
        client_inst.post.side_effect = [resp1, resp2]
        mock_client_cls.return_value.__aenter__.return_value = client_inst

        ans = await run_agent_loop("list dir", provider, hub, SessionMemory(), ResultCache(ttl_seconds=60))

    assert ans == "Directory listed cleanly."
    second_msgs = client_inst.post.call_args_list[1].kwargs["json"]["messages"]
    asst_msgs = [m for m in second_msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert asst_msgs[-1]["tool_calls"][0]["id"] == "call_gem_1"
    assert asst_msgs[-1]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig_token_xyz"}}
    assert tool_msgs[-1]["tool_call_id"] == "call_gem_1"


@pytest.mark.asyncio
async def test_gemini_503_failover_prefers_fast_3_flash_preview(monkeypatch):
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AURA_CLOUD_API_KEY", "AURA_CLOUD_BASE_URL", "FREELLMAPI_ENDPOINT", "FREELLMAPI_KEY", "GEMINI_MODEL", "AURA_GEMINI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gm_key")

    with patch("src.aura.config.settings.load_aura_env", return_value=None):
        provider = CloudProvider("cloud:gemini")

    resp_503 = MagicMock(status_code=503, text="high demand")
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"choices": [{"message": {"content": "fast preview ok"}}]}

    with patch("src.aura.core.providers.httpx.AsyncClient") as mock_client_cls:
        client_inst = AsyncMock()
        client_inst.post.side_effect = [resp_503, resp_ok]
        mock_client_cls.return_value.__aenter__.return_value = client_inst

        out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.message.content == "fast preview ok"
    assert provider.api_model == "gemini-3-flash-preview"



