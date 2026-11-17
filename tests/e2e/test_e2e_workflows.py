"""End-to-end multi-module workflow verification tests for AURA V4."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.types import TextContent

from src.aura.agents.loop import run_agent_loop
from src.aura.core import McpHub, ResultCache, SessionMemory, TaskScope
from src.aura.core.fast_router import FastRouter
from src.aura.core.mic_listener import MicListener
from src.aura.core.model_registry import ModelRegistry
from src.aura.core.providers import OllamaProvider
from src.aura.core.voice import VoiceEngine
from src.aura.mcp import desktop_server as ds
from src.aura.mcp import jarvis_server as js
from src.aura.mcp.browser_server import (
    BrowserManager,
    click_impl,
    close_impl,
    navigate_impl,
    scroll_impl,
    snapshot_impl,
    type_impl,
)
from src.aura.overlay import compute_island_geometry, compute_spring_frames


@pytest.mark.asyncio
async def test_e2e_desktop_open_inspect_and_research_workflow(tmp_path: Path, monkeypatch):
    """Real FastRouter + real MCP desktop_inspect_path + SSRF-safe jarvis_research loop."""
    desktop_dir = tmp_path / "Desktop"
    helmet_dir = desktop_dir / "smart-helmet"
    helmet_dir.mkdir(parents=True, exist_ok=True)
    (helmet_dir / "README.md").write_text(
        "# Smart Helmet Guardian\nESP32 + MPU6050 accelerometer crash detector with Neo-6M GPS SOS.\n",
        encoding="utf-8",
    )
    (helmet_dir / "package.json").write_text(
        json.dumps({"name": "smart-helmet-guardian", "version": "1.0.0", "main": "server.js"}),
        encoding="utf-8",
    )
    storage_dir = tmp_path / "storage"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AURA_STORAGE_DIR", str(storage_dir))

    # 1. Bare open command fast-routes via FastRouter without invoking LLM loop
    router = FastRouter(storage_dir=storage_dir)
    with patch("src.aura.core.fast_router.open_path_impl", return_value=f"Opened folder in File Explorer: {helmet_dir}") as mock_open:
        fast_res = router.try_handle("open smart-helmet on desktop", read_only=False)
        assert fast_res.handled is True
        assert fast_res.action == "open_path"
        mock_open.assert_called_once_with("smart-helmet", in_explorer=False, location="desktop", read_only=False)

    # 2. Compound open -> read -> research command bypasses FastRouter cleanly
    compound_prompt = "open smart-helmet on desktop, read what is inside, and research MPU6050 impact thresholds"
    assert router.try_handle(compound_prompt, read_only=True).handled is False

    scope = TaskScope(compound_prompt)
    assert scope.desktop is True and scope.jarvis is True

    # 3. Start real McpHub (RO mode) so desktop_inspect_path reads real Desktop/smart-helmet files
    hub = McpHub(ws_dir, read_only=True, enable_desktop=True, enable_jarvis=True)
    await hub.start()
    try:
        # Wire jarvis_research tool call through real jarvis_server HTTP/SSRF pipeline over MockTransport
        def _mock_http(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<title>MPU6050 Thresholds</title><p>Impact threshold around 3.2G reduces false positives.</p>",
            )

        orig_client = httpx.Client
        transport = httpx.MockTransport(_mock_http)

        class _InProcessJarvisResearchClient:
            async def call_tool(self, name: str, args: dict):
                with patch.object(httpx, "Client", lambda *a, **k: orig_client(*a, transport=transport, **k)), patch.object(
                    js,
                    "_search_ddg",
                    return_value=(
                        [{"title": "MPU6050 Fall Detection", "url": "https://93.184.216.34/mpu6050", "snippet": "3.2G threshold"}],
                        None,
                    ),
                ), patch.object(js, "check_ssrf", return_value=None):
                    txt = js.research_topic_impl(args.get("topic", "MPU6050"), max_sources=1)
                return SimpleNamespace(content=[TextContent(type="text", text=txt)], is_error=False)

        hub.tool_clients["jarvis_research"] = _InProcessJarvisResearchClient()

        def _tc(name: str, args: dict):
            m = MagicMock()
            m.function = MagicMock()
            m.function.name = name
            m.function.arguments = args
            return m

        turn = 0

        async def _chat(messages, tools=None):
            nonlocal turn
            turn += 1
            if turn == 1:
                return MagicMock(message=MagicMock(
                    content="",
                    role="assistant",
                    tool_calls=[_tc("desktop_inspect_path", {"target": "smart-helmet", "location": "desktop"})],
                ))
            if turn == 2:
                tool_texts = "\n".join(m.get("content", "") for m in messages if m.get("role") == "tool")
                assert "Smart Helmet Guardian" in tool_texts
                assert "package.json" in tool_texts
                return MagicMock(message=MagicMock(
                    content="",
                    role="assistant",
                    tool_calls=[_tc("jarvis_research", {"topic": "MPU6050 crash thresholds"})],
                ))
            tool_texts = "\n".join(m.get("content", "") for m in messages if m.get("role") == "tool")
            assert "3.2G" in tool_texts
            return MagicMock(message=MagicMock(
                content="smart-helmet is an ESP32 + MPU6050 crash detector; recommended trigger threshold is ~3.2G.",
                role="assistant",
                tool_calls=None,
            ))

        provider = MagicMock(spec=OllamaProvider)
        provider.chat = AsyncMock(side_effect=_chat)

        answer = await run_agent_loop(
            compound_prompt,
            provider,
            hub,
            SessionMemory(),
            ResultCache(),
        )
        assert "ESP32" in answer and "3.2G" in answer
        assert turn == 3
    finally:
        await hub.close()


def test_e2e_browser_dom_refs_and_ssrf_workflow():
    """Real headless Chromium workflow using [e1] refs, scroll, new tab, and SSRF click blocking."""
    mgr = BrowserManager()
    html = (
        "data:text/html,"
        "<title>E2E Browser Flow</title>"
        "<div style='height:1400px'>"
        "<input id='q' aria-label='Query box'/>"
        "<button id='apply' onclick=\"document.getElementById('status').innerText='Applied: '+document.getElementById('q').value\">Apply</button>"
        "<a id='bad' href='http://127.0.0.1:8765/internal'>Internal Link</a>"
        "<p id='status'>Ready</p>"
        "</div>"
    )
    try:
        s1 = navigate_impl(html, headless=True, read_only=False, manager=mgr, allow_local=True)
        assert "[e1]" in s1 and "Query box" in s1
        assert "[e2]" in s1 and "Apply" in s1

        s2 = type_impl("e1", "Helmet Telemetry", read_only=False, manager=mgr)
        assert "Helmet Telemetry" in s2

        s3 = click_impl("e2", read_only=False, manager=mgr)
        assert "Applied: Helmet Telemetry" in s3

        s4 = scroll_impl("down", amount=300, read_only=False, manager=mgr)
        assert "E2E Browser Flow" in s4

        mgr.set_allow_local(False)
        blocked = click_impl("e3", read_only=False, manager=mgr)
        assert "SSRF BLOCKED" in blocked
    finally:
        close_impl(manager=mgr)


def test_e2e_cursor_point_and_tell_and_sensitive_path_blocks(tmp_path: Path, monkeypatch):
    """Cursor inspection combines UIA + zoomed OCR and blocks sensitive paths."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    desktop = tmp_path / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    secret = desktop / ".env"
    secret.write_text("SECRET_TOKEN=xyz", encoding="utf-8")

    # Sensitive file stays blocked in inspect_path_impl
    assert "BLOCKED" in ds.inspect_path_impl(".env", location="desktop")
    assert "BLOCKED" in ds.inspect_path_impl(r"\\192.168.1.5\share\secret.txt")

    uia = {
        "name": "Run Checks",
        "control_type": "Button",
        "automation_id": "runBtn",
        "class_name": "Button",
        "is_enabled": True,
        "rect": {"left": 400, "top": 300, "width": 120, "height": 36},
    }
    win = {
        "title": "VS Code",
        "process": "Code",
        "hwnd": 777,
        "rect": {"left": 0, "top": 0, "width": 1920, "height": 1080},
    }

    class _FakeUser32:
        def GetCursorPos(self, pt_ptr):
            import ctypes
            pt = ctypes.cast(pt_ptr, ctypes.POINTER(ds.wintypes.POINT)).contents
            pt.x = 460
            pt.y = 318
            return 1

    with patch.object(ds, "_get_user32", return_value=_FakeUser32()), patch.object(
        ds, "_get_screen_bounds", return_value=(0, 0, 1920, 1080)
    ), patch.object(ds, "_query_uia_at_point", return_value=uia), patch.object(
        ds, "_resolve_window_at_point", return_value=win
    ), patch.object(
        ds, "_ocr_region_image", return_value=(True, "Run Checks Button Telemetry")
    ):
        report = ds.inspect_cursor_impl(include_window_ocr=False)

    assert "Cursor Inspection at (460, 318):" in report
    assert "VS Code" in report
    assert "Run Checks" in report
    assert "Run Checks Button Telemetry" in report


def test_e2e_voice_barge_in_and_mid_run_http_push_steer(tmp_path: Path, monkeypatch):
    """Instant speech onset barge-in + HTTP /api/chat(push=True) & /api/chat/steer preserve partial output."""
    from src.aura.server import create_app

    # Part 1: VoiceEngine playback + MicListener 2-block speech onset immediate barge-in
    import numpy as np

    ve = VoiceEngine(storage_dir=tmp_path)
    ve._playing = True
    ve._playback_level = 0.25
    stop_calls = []
    ve.stop = lambda: stop_calls.append(True) or setattr(ve, "_playing", False)

    onsets = []
    transcripts = []

    class _FakeWhisper:
        def transcribe(self, audio, **kwargs):
            return [SimpleNamespace(text="check smart helmet readme", no_speech_prob=0.05, avg_logprob=-0.2)], None

    ml = MicListener(
        on_transcript=transcripts.append,
        voice_engine=ve,
        on_speech_onset=lambda: (onsets.append(time.monotonic()), ml.set_barge_in(False)),
        require_wake_word=False,
        end_silence_blocks=5,
        barge_in_rms_floor=0.032,
        barge_in_onset_blocks=2,
    )
    ml.set_barge_in(True)
    quiet = np.zeros(1600, dtype=np.float32)
    echo = np.full(1600, 0.03, dtype=np.float32)
    speech = np.full(1600, 0.14, dtype=np.float32)
    # Near playback echo stays ignored; strong louder user speech triggers immediate onset on 2nd block
    assert ml.process_audio_chunk(echo, model=_FakeWhisper(), np_mod=np) is None
    assert onsets == []
    assert ml.process_audio_chunk(speech, model=_FakeWhisper(), np_mod=np) is None
    assert ml.process_audio_chunk(speech, model=_FakeWhisper(), np_mod=np) is None
    assert len(onsets) == 1
    assert stop_calls == [True]
    # Speech capture continues uninterrupted into full follow-up transcript
    out_tx = None
    for _ in range(5):
        out_tx = ml.process_audio_chunk(quiet, model=_FakeWhisper(), np_mod=np)
    assert out_tx == "check smart helmet readme"
    assert transcripts == ["check smart helmet readme"]

    # Part 2: HTTP API mid-run Push/Steer preserves partial output checkpoint and drains FIFO
    storage_dir = tmp_path / "web"
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AURA_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("AURA_ENABLE_DESKTOP_MCP", "0")
    monkeypatch.setenv("AURA_ENABLE_JARVIS_MCP", "0")

    started_evt = threading.Event()
    release_evt = threading.Event()

    async def _fake_run_task(task, *args, **kwargs):
        on_event = kwargs.get("on_event")
        get_pending_steer = kwargs.get("get_pending_steer")
        if callable(on_event):
            res = on_event({"event": "partial", "text": "Partial inspection: ESP32 firmware found."})
            if asyncio.iscoroutine(res):
                await res
        started_evt.set()
        for _ in range(100):
            if release_evt.is_set():
                break
            await asyncio.sleep(0.02)
        steers = get_pending_steer() if callable(get_pending_steer) else []
        return f"Done with steers={steers}", []

    with patch.object(ModelRegistry, "refresh", return_value=["qwen3.5:4b"]), patch(
        "src.aura.server._run_task", side_effect=_fake_run_task
    ):
        app = create_app(workspace=ws_dir, read_only=True, history_dir=storage_dir / "hist")
        with TestClient(app) as client:
            chat_holder: dict = {}

            def _run_chat():
                chat_holder["res"] = client.post("/api/chat", json={"prompt": "inspect project"}).json()

            t = threading.Thread(target=_run_chat, daemon=True)
            t.start()
            assert started_evt.wait(timeout=4.0)

            r_push1 = client.post("/api/chat", json={"prompt": "also check package.json", "push": True})
            assert r_push1.status_code == 200
            assert r_push1.json()["queued"] is True

            r_push2 = client.post("/api/chat/steer", json={"prompt": "research alternatives", "source": "voice"})
            assert r_push2.status_code == 200
            assert r_push2.json()["queued"] is True

            st_mid = client.get("/api/status").json()
            assert "Partial inspection: ESP32 firmware found." in st_mid["partial_output"]

            release_evt.set()
            t.join(timeout=5.0)
            assert "also check package.json" in chat_holder["res"]["answer"]
            assert "research alternatives" in chat_holder["res"]["answer"]

            with patch.object(app.state.session, "unload_vram", return_value=["qwen3.5:4b"]) as mock_unload:
                sd = client.post("/api/shutdown")
                assert sd.status_code == 200 and sd.json()["shutting_down"] is True
                mock_unload.assert_called_once_with(all_roles=True)


def test_e2e_dynamic_island_top_flush_tools_and_shutdown_lifecycle():
    """Verify real DynamicIslandPill top-flush geometry, smooth springs, sticky Tools popover, Push UI, and shutdown."""
    for target_expanded in (True, False):
        a = compute_island_geometry(1920, 1080, expanded=not target_expanded)
        b = compute_island_geometry(1920, 1080, expanded=target_expanded)
        assert a.y == 0 and b.y == 0
        frames = compute_spring_frames(a, b)
        assert all(f.y == 0 for f in frames) and frames[-1] == b

    script = (
        "import time\n"
        "from src.aura import overlay\n"
        "from src.aura.core.mic_listener import MicListener\n"
        "overlay._poll_hotkey_state = lambda *a, **k: False\n"
        "events = []\n"
        "overlay.JarvisBridgeClient.get_status = lambda self: {'read_only': False, 'ollama_up': True, 'active_model': 'qwen3.5:4b', 'available_models': ['qwen3.5:4b'], 'available_tools': [{'name': 'desktop_inspect_cursor', 'description': 'Inspect cursor UI'}, {'name': 'browser_navigate', 'description': 'Open web page'}]}\n"
        "overlay.JarvisBridgeClient.get_dashboard_data = lambda self: {'todos': [], 'apps': {'apps': [], 'browser_tabs': 0}}\n"
        "overlay.JarvisBridgeClient.steer_chat = lambda self, prompt, **k: (events.append(('steer', prompt)) or {'queued': True, 'steer_seq': 1, 'status': None})\n"
        "overlay.JarvisBridgeClient.shutdown = lambda self: events.append(('shutdown', True))\n"
        "MicListener.start = lambda self: None\n"
        "MicListener.stop = lambda self: None\n"
        "pill = overlay.DynamicIslandPill('http://127.0.0.1:8765')\n"
        "pill.root.update()\n"
        "assert pill._current_geo.y == 0\n"
        "pill.expand()\n"
        "dl = time.monotonic() + 2.0\n"
        "while pill._anim_after_id is not None and time.monotonic() < dl:\n"
        "    pill.root.update(); time.sleep(0.01)\n"
        "pill.root.update()\n"
        "assert pill._current_geo.y == 0\n"
        "assert pill._current_geo.width == min(960, max(200, pill.root.winfo_screenwidth() - 24))\n"
        "assert pill._current_geo.height == min(520, max(28, pill.root.winfo_screenheight() - 48))\n"
        "pill.available_tools = [{'name': 'desktop_inspect_cursor', 'description': 'Inspect cursor UI'}, {'name': 'browser_navigate', 'description': 'Open web page'}]\n"
        "pill._open_tools_popover(pinned=False)\n"
        "pill._toggle_tools_popover()\n"
        "assert pill._tools_pinned is True\n"
        "pill._tools_filter_has_hint = False\n"
        "pill._tools_filter_var.set('browser')\n"
        "pill._populate_tools_popover()\n"
        "assert pill._tools_hdr_lbl.cget('text') == 'TOOLS (1/2)'\n"
        "pill._use_tool_template('browser_navigate')\n"
        "assert pill._tools_popover_visible is False\n"
        "pill.busy = True\n"
        "pill._sync_stop_controls()\n"
        "assert pill.send_btn.cget('text') == '➤ Push'\n"
        "pill._append_rich([('Working output preserved.\\n', 'bot_body')])\n"
        "pill._submit()\n"
        "deadline = time.monotonic() + 2.0\n"
        "while ('steer', pill.entry.get()) not in events and not any(k == 'steer' for k, _ in events) and time.monotonic() < deadline:\n"
        "    pill.root.update(); time.sleep(0.02)\n"
        "assert any(k == 'steer' for k, _ in events)\n"
        "assert 'Working output preserved.' in pill.output.get('1.0', 'end')\n"
        "pill.shutdown_app()\n"
        "pill.root.mainloop()\n"
        "assert ('shutdown', True) in events\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr or proc.stdout
