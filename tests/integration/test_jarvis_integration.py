"""
End-to-end integration tests for AURA V4 Windows Jarvis capabilities.

Tests real multi-server MCP stdio orchestration (desktop + filesystem + git + jarvis),
Read-Only / Read-Write enforcement across stdio boundaries, cross-restart SSD
personal memory persistence, FastAPI Web + FastRouter + Voice DSP integration.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
import pytest

from src.aura.core.mcp_hub import McpHub
from src.aura.core.model_registry import ModelRegistry


@pytest.mark.asyncio
async def test_mcp_hub_starts_all_four_servers_and_enforces_ro(tmp_path: Path, monkeypatch):
    storage_dir = tmp_path / "storage"
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AURA_STORAGE_DIR", str(storage_dir))

    hub = McpHub(workspace_dir, read_only=True, enable_desktop=True, enable_jarvis=True)
    await hub.start()
    try:
        names = set(hub.tools.keys())
        for expected in (
            "desktop_list_monitors",
            "desktop_inspect_cursor",
            "desktop_inspect_path",
            "desktop_clipboard",
            "desktop_trash_file",
            "desktop_ui_see",
            "desktop_mouse_click",
            "desktop_type_text",
            "desktop_hotkey",
            "desktop_scroll",
            "jarvis_web_search",
            "jarvis_research",
            "jarvis_fetch_url",
            "jarvis_memory",
            "jarvis_speak",
            "jarvis_calendar",
            "jarvis_gmail",
            "jarvis_briefing",
        ):
            assert expected in names, f"Missing MCP tool: {expected}"

        async def _call(tool_name: str, args: dict) -> str:
            res = await hub.tool_clients[tool_name].call_tool(tool_name, args)
            return res.content[0].text if res.content else ""

        # Read-only safe monitor listing works via real desktop stdio server
        mon_out = await _call("desktop_list_monitors", {})
        assert "DISPLAY" in mon_out or "bounds" in mon_out

        # RO-safe inspection of non-sensitive file + sensitive path protection
        notes = workspace_dir / "notes.txt"
        notes.write_text("Smart helmet prototype telemetry", encoding="utf-8")
        inspect_out = await _call("desktop_inspect_path", {"target": str(notes)})
        assert "Smart helmet prototype telemetry" in inspect_out

        env_secret = workspace_dir / ".env"
        env_secret.write_text("API_KEY=secret", encoding="utf-8")
        sensitive_out = await _call("desktop_inspect_path", {"target": str(env_secret)})
        assert "SECURITY BLOCKED" in sensitive_out or "BLOCKED" in sensitive_out

        # Fail-closed SSRF guard blocks loopback over real jarvis stdio server
        ssrf_out = await _call("jarvis_fetch_url", {"url": "http://127.0.0.1:8000"})
        assert "Blocked" in ssrf_out or "FETCH ERROR" in ssrf_out

        # Mutating tools remain strictly blocked in Read-Only mode
        victim = workspace_dir / "keep.txt"
        victim.write_text("safe", encoding="utf-8")
        trash_out = await _call("desktop_trash_file", {"path": str(victim)})
        assert "READ-ONLY BLOCKED" in trash_out
        assert victim.exists()

        mem_out = await _call("jarvis_memory", {"action": "remember", "entry": "should not write in RO"})
        assert "READ-ONLY BLOCKED" in mem_out

        cal_ro = await _call("jarvis_calendar", {"action": "add", "title": "Blocked in RO"})
        assert "READ-ONLY BLOCKED" in cal_ro

        click_ro = await _call("desktop_mouse_click", {"x": 50, "y": 50})
        assert "READ-ONLY BLOCKED" in click_ro
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_jarvis_mcp_memory_persists_across_stdio_restarts(tmp_path: Path):
    storage_dir = tmp_path / "jarvis_mem"
    script = Path(__file__).resolve().parents[2] / "src" / "aura" / "mcp" / "jarvis_server.py"
    env = os.environ.copy()
    env["AURA_STORAGE_DIR"] = str(storage_dir)
    env["AURA_READ_ONLY"] = "0"

    params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)

    # First stdio session writes durable fact
    async with Client(params) as client1:
        res_add = await client1.call_tool("jarvis_memory", {"action": "remember", "entry": "pref integration test token"})
        assert not res_add.is_error
        assert "Remembered:" in res_add.content[0].text

    # Second stdio session reads persisted SSD fact & cleans it up
    async with Client(params) as client2:
        res_read = await client2.call_tool("jarvis_memory", {"action": "read"})
        assert not res_read.is_error
        assert "pref integration test token" in res_read.content[0].text

        res_forget = await client2.call_tool("jarvis_memory", {"action": "forget", "entry": "integration test token"})
        assert not res_forget.is_error
        assert "Forgot 1" in res_forget.content[0].text


def test_fastapi_jarvis_end_to_end_pipeline(tmp_path: Path, monkeypatch):
    from src.aura.server import create_app

    storage_dir = tmp_path / "web_storage"
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AURA_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("AURA_ENABLE_DESKTOP_MCP", "1")
    monkeypatch.setenv("AURA_ENABLE_JARVIS_MCP", "1")
    monkeypatch.setenv("AURA_CLOUD_FOR_HEAVY", "0")

    with patch.object(ModelRegistry, "refresh", return_value=["qwen3.5:4b", "qwen2.5-coder:7b"]):
        app = create_app(
            workspace=ws_dir,
            read_only=True,
            history_dir=storage_dir / "web_sessions",
        )
        with TestClient(app) as client:
            st = client.get("/api/status").json()
            assert st["servers"] == ["browser", "desktop", "filesystem", "git", "jarvis"]
            assert st["tools_count"] == 54
            assert any(t["name"] == "desktop_open_path" for t in st["available_tools"])
            assert any(t["name"] == "desktop_inspect_cursor" for t in st["available_tools"])
            assert any(t["name"] == "desktop_inspect_path" for t in st["available_tools"])
            assert any(t["name"] == "jarvis_research" for t in st["available_tools"])
            assert st["read_only"] is True
            assert st["cloud_for_heavy"] is False

            # Tier-0 FastRouter handles monitor query without calling LLM
            r_mon = client.post("/api/chat", json={"prompt": "list monitors"})
            assert r_mon.status_code == 200
            assert "Tier-0 FastRouter" in r_mon.json()["metrics"]

            # RO blocks personal memory persistence
            r_ro_mem = client.post("/api/chat", json={"prompt": "remember that integration ro check"})
            assert r_ro_mem.status_code == 200
            assert "READ-ONLY BLOCKED" in r_ro_mem.json()["answer"]
            assert client.get("/api/status").json()["personal_facts"] == 0

            # Toggle RW -> remember -> recall
            assert client.post("/api/mode", json={"mode": "rw"}).status_code == 200
            r_rw_mem = client.post("/api/chat", json={"prompt": "remember that favorite voice is megatron"})
            assert r_rw_mem.status_code == 200
            assert "Remembered" in r_rw_mem.json()["answer"]

            r_recall = client.post("/api/chat", json={"prompt": "what do you remember"})
            assert r_recall.status_code == 200
            assert "megatron" in r_recall.json()["answer"].lower()

            # Voice config + Megatron DSP WAV synthesis (offline SAPI safe path)
            vc = client.post("/api/voice/config", json={"persona": "megatron", "prefer_offline": True})
            assert vc.status_code == 200
            assert vc.json()["persona"] == "megatron"

            if os.name == "nt":
                sp = client.post(
                    "/api/voice/speak",
                    json={"text": "Integration check operational.", "persona": "megatron", "play_local": False},
                )
                assert sp.status_code == 200
                assert sp.headers["content-type"].startswith("audio/wav")
                assert sp.content.startswith(b"RIFF")
                assert len(sp.content) > 1000

            assert client.post("/api/mode", json={"mode": "ro"}).status_code == 200

            # Voice stop + idle steer + shutdown endpoints respond cleanly
            vs = client.post("/api/voice/stop")
            assert vs.status_code == 200
            assert "stopped" in vs.json()

            st_idle = client.post("/api/chat/steer", json={"prompt": "ping"})
            assert st_idle.status_code == 200
            assert st_idle.json()["queued"] is False

            with patch.object(app.state.session, "unload_vram", return_value=["qwen3.5:4b"]) as mock_unload:
                sd = client.post("/api/shutdown")
                assert sd.status_code == 200
                assert sd.json().get("shutting_down") is True
                assert sd.json().get("unloaded") == ["qwen3.5:4b"]
                mock_unload.assert_called_once_with(all_roles=True)
