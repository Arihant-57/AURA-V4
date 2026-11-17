"""Tests for sequential specialist agents, loop efficiency upgrades, web UI agent selector, and 3-tab Dynamic Island."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aura.core import orchestrator as orch_mod
from aura.core.cache import ResultCache
from aura.core.file_tracker import InspectionTracker
from aura.core.memory import SessionMemory
from aura.core.model_registry import ModelCapability, ModelRegistry
from aura.core.model_router import ModelRouter, TaskState
from aura.core.orchestrator import run_multi_stage
from aura.core.providers import OllamaProvider
from aura.core.scope import (
    SPECIALIST_AGENTS,
    TaskScope,
    normalize_agent_role,
    parse_agent_prefix,
    select_active_tools,
)
from aura.core.tools import check_path_policy, normalize_tool_arguments
from aura.agents.loop import _parse_text_tool_calls, _track_read_file, execute_tool_call, run_agent_loop
from aura.mcp import filesystem_server as fs_srv
from aura.overlay import compute_island_geometry, compute_spring_frames, format_pill_badge


def _make_tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=n, description=n, inputSchema={"type": "object", "properties": {}}) for n in names]


class TestSpecialistAgentsAndScope:
    def test_parse_agent_prefix_and_aliases(self):
        assert parse_agent_prefix("@planner review auth") == ("planner", "review auth")
        assert parse_agent_prefix("@architect inspect") == ("planner", "inspect")
        assert parse_agent_prefix("@debug fix traceback") == ("debugger", "fix traceback")
        assert parse_agent_prefix("hello @user nothing here") == (None, "hello @user nothing here")
        assert normalize_agent_role("verifier") == "tester"
        assert normalize_agent_role("unknown") == "auto"

    def test_specialist_tool_whitelists_and_path_policy(self):
        all_tools = _make_tools(
            "read_file", "list_directory", "search_files", "file_outline",
            "write_file", "edit_file", "get_file_diff", "run_tests",
            "git_status", "git_diff", "jarvis_web_search", "desktop_click",
        )
        planner_tools = {t.name for t in select_active_tools(all_tools, TaskScope("check app", agent_role="planner"))}
        assert "read_file" in planner_tools
        assert "file_outline" in planner_tools
        assert "git_status" in planner_tools
        assert "write_file" not in planner_tools
        assert "edit_file" not in planner_tools
        assert "run_tests" not in planner_tools

        coder_tools = {t.name for t in select_active_tools(all_tools, TaskScope("edit app", agent_role="coder"))}
        assert {"read_file", "edit_file", "write_file", "file_outline", "get_file_diff"} <= coder_tools
        assert "desktop_click" not in coder_tools

        blocked = check_path_policy("file_outline", {"path": ".env"}, TaskScope("outline .env"))
        assert blocked and "security policy" in blocked.lower()

        norm_args = normalize_tool_arguments("search_files", {"pattern": "def main", "glob": "*.py", "regex": True})
        assert norm_args == {"query": "def main", "file_glob": "*.py", "use_regex": True}


class TestLoopAndFilesystemUpgrades:
    def test_file_outline_and_search_regex_glob(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AURA_WORKSPACE", str(tmp_path))
        (tmp_path / "app.py").write_text(
            "class Service:\n    def run(self):\n        return 42\n\nasync def helper():\n    pass\n",
            encoding="utf-8",
        )
        (tmp_path / "notes.md").write_text("# Heading\ndef fake_in_md\n", encoding="utf-8")

        outline = fs_srv.file_outline("app.py")
        assert "class Service" in outline
        assert "def Service.run()" in outline
        assert "async def helper()" in outline

        matches = fs_srv.search_files(r"async\s+def\s+helper", use_regex=True, file_glob="*.py")
        assert "app.py:5:" in matches
        assert "notes.md" not in matches

        bad_re = fs_srv.search_files("[unclosed", use_regex=True)
        assert "ERROR: Invalid regex" in bad_re

    def test_cache_invalidation_after_mutation_and_line_tracking(self):
        cache = ResultCache()
        cache.put(("search_files", '{"query": "foo"}'), "old result")
        cache.put(("list_directory", '{"path": "."}'), "old dir")
        cache.invalidate_mutation()
        assert cache.get(("search_files", '{"query": "foo"}')) is None
        assert cache.get(("list_directory", '{"path": "."}')) is None

        tracker = InspectionTracker()
        _track_read_file("[Lines 5 to 20 of 80 in app.py]:\ncode", "app.py", tracker)
        assert tracker.files["app.py"].total_lines == 80
        assert tracker.files["app.py"].coverage == pytest.approx(16 / 80)

    def test_text_tool_fallback_supports_search_files(self):
        calls = _parse_text_tool_calls('search_files("authenticate")', {"search_files"})
        assert calls == [("search_files", {"query": "authenticate"})]

    @pytest.mark.asyncio
    async def test_parallel_read_only_batch_execution(self):
        hub = MagicMock()
        hub.broken = None
        hub.workspace = "/ws"
        hub.read_only = True
        t_read = SimpleNamespace(name="read_file", description="r", inputSchema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        t_git = SimpleNamespace(name="git_status", description="g", inputSchema={"type": "object", "properties": {}})
        hub.tools = {"read_file": t_read, "git_status": t_git}
        hub.tool_schemas = {"read_file": t_read.inputSchema, "git_status": t_git.inputSchema}
        hub.tool_server = {"read_file": "filesystem", "git_status": "git"}

        active_calls = 0
        max_concurrent = 0

        async def mock_call_tool(name, args):
            nonlocal active_calls, max_concurrent
            active_calls += 1
            max_concurrent = max(max_concurrent, active_calls)
            await asyncio.sleep(0.03)
            active_calls -= 1
            return SimpleNamespace(content=[SimpleNamespace(text=f"ok:{name}:{args}")], is_error=False)

        client = MagicMock()
        client.call_tool = mock_call_tool
        hub.tool_clients = {"read_file": client, "git_status": client}

        tc1 = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments={"path": "a.py"}))
        tc2 = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments={"path": "b.py"}))
        resp1 = SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tc1, tc2]))
        resp2 = SimpleNamespace(message=SimpleNamespace(content="Done reading both files.", tool_calls=None))

        provider = MagicMock()
        provider.model = "qwen3.5:4b"
        provider.chat = AsyncMock(side_effect=[resp1, resp2])

        answer = await run_agent_loop(
            "inspect a.py and b.py",
            provider,
            hub,
            SessionMemory(),
            ResultCache(),
            task_state=TaskState(original_request="inspect a.py and b.py", active_agent="planner"),
        )
        assert "Done reading both files." in answer
        assert max_concurrent == 2

    def test_unload_skips_subprocess_when_daemon_offline(self):
        provider = OllamaProvider(model="qwen3.5:4b")
        provider._client = MagicMock()
        provider._client.generate.side_effect = ConnectionError("connection refused")
        with patch("subprocess.run") as mock_sub:
            res = provider.unload()
        assert res is False
        mock_sub.assert_not_called()


class TestSingleVsHybridOrchestrator:
    @pytest.mark.asyncio
    async def test_single_model_stays_warm_between_stages(self):
        reg = ModelRegistry()
        reg.models["qwen3.5:4b"] = ModelCapability(name="qwen3.5:4b", parameter_count=4.0, context_length=32768, supports_tools=True)
        reg._refreshed = True
        router = ModelRouter(registry=reg)

        hub = MagicMock()
        hub.broken = False
        hub.workspace = "/project"
        hub.read_only = False
        hub.tools = {}
        hub.tool_clients = {}
        hub.tool_schemas = {}

        provider_inst = MagicMock()
        provider_inst.model = "qwen3.5:4b"
        provider_inst.unload = MagicMock(return_value=True)

        async def mock_loop(*args, **kwargs):
            return "VERDICT: PASS\nFINDINGS:\n- ok"

        with patch.object(orch_mod.ProviderFactory, "create_for_model", return_value=provider_inst), \
             patch.object(orch_mod, "run_agent_loop", side_effect=mock_loop):
            await run_multi_stage(
                "fix the multi file authentication handler across routes and verify tests",
                router, hub, MagicMock(), MagicMock(),
            )
        # Reuses single warm instance across stages, unloaded only once in finally.
        assert provider_inst.unload.call_count == 1


class TestWebServerAndDynamicIsland:
    def test_web_ui_and_chat_agent_role(self, tmp_path):
        from aura import server as web_srv

        app = web_srv.create_app(workspace=tmp_path, read_only=True, history_dir=tmp_path / "hist")
        hub = MagicMock()
        hub.workspace = tmp_path
        hub.read_only = True
        hub.tools = {}
        hub.connected_servers = []
        hub.is_running = True
        reg = ModelRegistry()
        reg._refreshed = True
        router = ModelRouter(registry=reg)
        prov = MagicMock()
        prov.model = "qwen3.5:4b"

        session = web_srv.AuraSession(
            hub=hub,
            registry=reg,
            router=router,
            provider=prov,
            history=web_srv.ChatHistoryStore(tmp_path / "hist"),
            workspace_selected=True,
        )
        session.active_chat_id = session.history.create(workspace=str(tmp_path), title="Test")["id"]
        app.state.session = session

        client = TestClient(app)

        html = client.get("/").text
        assert 'id="agentSelect"' in html

        st = client.get("/api/status").json()
        assert "agent_role" in st
        assert "available_agents" in st
        assert any(a["id"] == "planner" for a in st["available_agents"])

        captured: dict[str, str | None] = {}

        async def fake_run_task(prompt, *args, agent_role=None, **_kwargs):
            captured["prompt"] = prompt
            captured["agent_role"] = agent_role
            return "Planner analyzed files", [prov]

        with patch.object(web_srv, "_run_task", side_effect=fake_run_task):
            resp = client.post("/api/chat", json={"prompt": "audit architecture", "agent_role": "planner"})
        assert resp.status_code == 200
        assert captured == {"prompt": "audit architecture", "agent_role": "planner"}

    def test_overlay_helpers_and_3tab_island_lifecycle(self):
        start_geo = compute_island_geometry(1920, 1080, expanded=False)
        end_geo = compute_island_geometry(1920, 1080, expanded=True)
        frames = compute_spring_frames(start_geo, end_geo, steps=6)
        assert len(frames) == 6
        assert frames[-1] == end_geo
        heights = [f.height for f in frames]
        assert heights == sorted(heights)

        badge = format_pill_badge(False, "AURA Jarvis", "off", True, agent_role="planner")
        assert "Planner" in badge

        script = """
import os, sys
os.environ["AURA_NO_AUTO_OLLAMA"] = "1"
from aura.overlay import DynamicIslandPill, _CpuSampler
pill = DynamicIslandPill()
pill.expand()
pill.root.update()
assert hasattr(pill, "wave_canvas")
assert hasattr(pill, "capsule_mic_btn")
assert sorted(pill.tab_btns.keys()) == ["agents", "chat", "deck"]
pill.switch_tab("agents")
pill.root.update()
assert pill.active_tab == "agents"
pill.select_agent("coder", switch_to_chat=True)
pill.root.update()
assert pill.selected_agent == "coder"
assert pill.active_tab == "chat"
assert "Coder" in pill.agent_btn.cget("text")
pill.switch_tab("deck")
pill.root.update()
assert pill.active_tab == "deck"
pill._set_focus_minutes(15)
assert pill._focus_total_sec == 900
assert pill._focus_running is False
pill._toggle_focus_timer()
assert pill._focus_running is True
assert pill.focus_toggle_btn.cget("text") == "Pause"
pill._reset_focus_timer()
assert pill._focus_running is False
assert pill.focus_toggle_btn.cget("text") == "Start"
sample = _CpuSampler().sample()
assert {"cpu_pct", "ram_pct", "battery_pct", "charging"}.issubset(sample.keys())
pill.collapse()
pill.root.update()
pill._destroy_root()
print("OVERLAY_3TAB_OK")
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20, env=env)
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert "OVERLAY_3TAB_OK" in res.stdout
