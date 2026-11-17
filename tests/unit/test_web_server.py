"""Unit and API tests for the AURA V4 FastAPI localhost web server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.aura.core import ResultCache, SessionMemory
from src.aura.core.model_registry import ModelRole
from src.aura.server import AuraSession, ChatHistoryStore, create_app


class _FakeRouter:
    def __init__(self) -> None:
        self.override: str | None = None

    def set_override(self, model: str | None) -> None:
        self.override = model

    def select(self, role: ModelRole) -> str:
        if self.override:
            return self.override
        if role == ModelRole.CODING:
            return "qwen2.5-coder:7b"
        return "qwen3.5:4b"


def _make_fake_session(workspace: Path) -> AuraSession:
    hub = MagicMock()
    hub.workspace = workspace
    hub.read_only = True
    hub.tools = {"read_file": object(), "list_dir": object()}
    hub.clients = {"filesystem": object(), "terminal": object()}

    async def _switch_workspace(new_ws: Path, read_only: bool = True) -> None:
        hub.workspace = new_ws
        hub.read_only = read_only

    hub.switch_workspace = AsyncMock(side_effect=_switch_workspace)
    hub.close = AsyncMock()

    provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=10,
        total_eval_count=20,
        num_ctx=4096,
        unload=MagicMock(return_value=True),
    )

    registry = MagicMock()
    registry.models = {"qwen3.5:4b": object(), "qwen2.5-coder:7b": object(), "nemotron-3-nano:4b": object()}

    return AuraSession(
        hub=hub,
        registry=registry,
        router=_FakeRouter(),
        provider=provider,
        memory=SessionMemory(),
        cache=ResultCache(),
        history=ChatHistoryStore(workspace / ".test_history"),
        authorized_workspaces={workspace},
    )


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(workspace=tmp_path)
    app.state.session = _make_fake_session(tmp_path)
    with TestClient(app) as test_client:
        yield test_client, app.state.session


def test_index_serves_html(client):
    test_client, _ = client
    res = test_client.get("/")
    assert res.status_code == 200
    assert "AURA V4" in res.text
    assert "Projects &amp; Chats (SSD)" in res.text


def test_ssd_chat_history_crud(client):
    test_client, _ = client
    created = test_client.post("/api/chats", json={"title": "Test Project"})
    assert created.status_code == 200
    chat_id = created.json()["chat"]["id"]

    listed = test_client.get("/api/chats")
    assert listed.status_code == 200
    assert any(c["id"] == chat_id for c in listed.json()["chats"])

    loaded = test_client.get(f"/api/chats/{chat_id}")
    assert loaded.status_code == 200
    assert loaded.json()["chat"]["title"] == "Test Project"

    deleted = test_client.delete(f"/api/chats/{chat_id}")
    assert deleted.status_code == 200
    assert test_client.get(f"/api/chats/{chat_id}").status_code == 404


def test_ssd_project_memory_continuity(client):
    test_client, session = client
    fake_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=10,
        total_eval_count=5,
        num_ctx=4096,
    )
    session.memory.note_file("src/app.py")
    mock_run = AsyncMock(return_value=("Checked src/app.py", [fake_provider]))
    with patch("src.aura.server._run_task", new=mock_run):
        res1 = test_client.post("/api/chat", json={"prompt": "Inspect src/app.py"})
    assert res1.status_code == 200
    chat_id = res1.json()["chat_id"]

    # Simulate memory wipe + reload from SSD
    session.memory.clear()
    reloaded = test_client.get(f"/api/chats/{chat_id}")
    assert reloaded.status_code == 200
    assert "src/app.py" in session.memory.files

    with patch("src.aura.server._run_task", new=mock_run):
        res2 = test_client.post("/api/chat", json={"prompt": "Fix the bug you found"})
    assert res2.status_code == 200
    sent_prompt = mock_run.call_args_list[1].args[0]
    assert "[Project Memory from earlier in this session]" in sent_prompt
    assert "Inspect src/app.py" in sent_prompt


def test_status_and_mode_toggle(client):
    test_client, _ = client
    res = test_client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "ro"
    assert data["tools_count"] == 2
    assert isinstance(data.get("available_tools"), list)
    assert len(data["available_tools"]) == 2
    assert data["roles"]["coding"] == "qwen2.5-coder:7b"

    res = test_client.post("/api/mode", json={"mode": "rw"})
    assert res.status_code == 200
    assert res.json()["mode"] == "rw"
    assert res.json()["read_only"] is False


def test_workspace_switch_validation_and_reset(client, tmp_path: Path):
    test_client, session = client
    session.memory.note_file("a.py")

    bad = test_client.post("/api/workspace", json={"path": str(tmp_path / "missing")})
    assert bad.status_code == 400

    other = tmp_path / "other_proj"
    other.mkdir()
    ok = test_client.post("/api/workspace", json={"path": str(other)})
    assert ok.status_code == 200
    body = ok.json()
    assert Path(body["workspace"]).resolve() == other.resolve()
    assert body["mode"] == "ro"
    assert body["memory_files"] == 0


def test_model_config_and_reset_endpoints(client):
    test_client, session = client
    with patch("src.aura.server.ProviderFactory.create_ollama", return_value=SimpleNamespace(model="qwen2.5-coder:7b")):
        res = test_client.post("/api/model", json={"low_vram": True, "model_override": None})
    assert res.status_code == 200
    assert res.json()["low_vram"] is True
    assert res.json()["roles"]["analysis"] == "qwen2.5-coder:7b"

    session.memory.note_file("b.py")
    res_reset = test_client.post("/api/reset")
    assert res_reset.status_code == 200
    assert res_reset.json()["memory_files"] == 0


def test_chat_endpoint_and_busy_lock(client):
    test_client, session = client

    empty = test_client.post("/api/chat", json={"prompt": "   "})
    assert empty.status_code == 400

    fake_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=12,
        total_eval_count=8,
        num_ctx=4096,
    )
    with patch("src.aura.server._run_task", new=AsyncMock(return_value=("Done!", [fake_provider]))):
        ok = test_client.post("/api/chat", json={"prompt": "List files"})
    assert ok.status_code == 200
    assert ok.json()["answer"] == "Done!"
    assert "qwen3.5:4b" in ok.json()["metrics"]

    # Simulate an active task holding the sequential VRAM lock
    session.busy = True
    try:
        busy = test_client.post("/api/chat", json={"prompt": "Another task"})
        assert busy.status_code == 409
    finally:
        session.busy = False


def test_websocket_chat(client):
    test_client, _ = client
    fake_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=5,
        total_eval_count=5,
        num_ctx=4096,
    )
    with patch("src.aura.server._run_task", new=AsyncMock(return_value=("WS OK", [fake_provider]))):
        with test_client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"prompt": "Hello AURA"})
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
            assert msg1["type"] == "status"
            assert msg2["type"] == "result"
            assert msg2["answer"] == "WS OK"


def test_cross_chat_workspace_memory_persists_on_ssd(client):
    test_client, session = client
    fake_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=8,
        total_eval_count=6,
        num_ctx=4096,
    )
    session.memory.note_modified("src/auth.py")
    mock_run = AsyncMock(return_value=("Fixed auth check; ran pytest tests/unit/ -q", [fake_provider]))

    # Chat 1 records workspace facts/files/commands on SSD
    with patch("src.aura.server._run_task", new=mock_run):
        r1 = test_client.post("/api/chat", json={"prompt": "Fix auth bug in src/auth.py"})
    assert r1.status_code == 200
    chat1 = r1.json()["chat_id"]

    # Switch to a brand new Chat 2 and wipe in-RAM SessionMemory
    session.memory.clear()
    new_chat = test_client.post("/api/chats", json={"title": "Fresh Chat"})
    assert new_chat.status_code == 200
    assert new_chat.json()["chat"]["id"] != chat1

    with patch("src.aura.server._run_task", new=mock_run):
        r2 = test_client.post("/api/chat", json={"prompt": "Check remaining issues"})
    assert r2.status_code == 200
    prompt2 = mock_run.call_args_list[1].args[0]
    assert "[Workspace Memory (persisted on SSD across chats)]" in prompt2
    assert "src/auth.py" in prompt2
    assert "pytest tests/unit/ -q" in prompt2
    assert "[Project Memory from earlier in this session]" not in prompt2


def test_websocket_streams_progress_events(client):
    test_client, _ = client
    fake_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=5,
        total_eval_count=5,
        num_ctx=4096,
    )

    async def _fake_run_task(*args, on_event=None, **kwargs):
        if on_event is not None:
            await on_event({"event": "stage", "stage": "analysis", "message": "Stage 1/3: Analysis..."})
            await on_event({"event": "tool", "stage": "analysis", "tool": "read_file", "status": "running"})
        return "Streamed OK", [fake_provider]

    with patch("src.aura.server._run_task", side_effect=_fake_run_task):
        with test_client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"prompt": "Inspect auth.py"})
            frames = [ws.receive_json(), ws.receive_json(), ws.receive_json(), ws.receive_json()]
            assert frames[0]["type"] == "status"
            assert frames[1]["type"] == "progress"
            assert frames[1]["stage"] == "analysis"
            assert frames[2]["type"] == "progress"
            assert frames[2]["tool"] == "read_file"
            assert frames[3]["type"] == "result"
            assert frames[3]["answer"] == "Streamed OK"


def test_cloud_key_endpoint_and_cloud_model_selection(client, monkeypatch):
    test_client, session = client
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("AURA_CLOUD_FOR_HEAVY", raising=False)

    res = test_client.post(
        "/api/cloud-key",
        json={"provider": "groq", "api_key": "gsk_dummy_123", "use_for_heavy": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert "gsk_dummy_123" not in str(body)
    assert body["status"]["cloud_for_heavy"] is True
    assert "cloud:groq/llama-3.3-70b-versatile" in body["status"]["available_cloud_models"]

    sel = test_client.post(
        "/api/model",
        json={"low_vram": False, "model_override": "cloud:groq/llama-3.3-70b-versatile"},
    )
    assert sel.status_code == 200
    assert sel.json()["model_override"] == "cloud:groq/llama-3.3-70b-versatile"
    assert session.provider.model == "cloud:groq/llama-3.3-70b-versatile"


def test_ssd_projects_subchats_and_cloud_turn_flag(client):
    test_client, _ = client

    p_res = test_client.post("/api/projects", json={"name": "Payments API"})
    assert p_res.status_code == 200
    assert "Payments API" in p_res.json()["projects"]

    sub_res = test_client.post("/api/chats", json={"title": "Auth refactor", "project": "Payments API"})
    assert sub_res.status_code == 200
    sub_id = sub_res.json()["chat"]["id"]
    assert sub_res.json()["chat"]["project"] == "Payments API"

    normal_res = test_client.post("/api/chats", json={"title": "Standalone chat"})
    assert normal_res.status_code == 200
    normal_id = normal_res.json()["chat"]["id"]
    assert normal_res.json()["chat"]["project"] == ""

    move_res = test_client.post(f"/api/chats/{normal_id}/project", json={"project": "Payments API"})
    assert move_res.status_code == 200
    assert move_res.json()["chat"]["project"] == "Payments API"

    # Cloud turn flags are persisted on SSD; local turns have cloud=False.
    cloud_provider = SimpleNamespace(
        model="cloud:custom/auto",
        total_prompt_eval_count=10,
        total_eval_count=5,
        num_ctx=12288,
    )
    local_provider = SimpleNamespace(
        model="qwen3.5:4b",
        total_prompt_eval_count=10,
        total_eval_count=5,
        num_ctx=4096,
    )
    with patch("src.aura.server._run_task", new=AsyncMock(return_value=("Cloud reply", [cloud_provider]))):
        c_turn = test_client.post("/api/chat", json={"prompt": "Analyze"})
    assert c_turn.status_code == 200
    assert c_turn.json()["cloud"] is True

    with patch("src.aura.server._run_task", new=AsyncMock(return_value=("Local reply", [local_provider]))):
        l_turn = test_client.post("/api/chat", json={"prompt": "Ping"})
    assert l_turn.status_code == 200
    assert l_turn.json()["cloud"] is False

    fetched = test_client.get(f"/api/chats/{normal_id}").json()["chat"]
    aura_msgs = [m for m in fetched["messages"] if m["role"] == "aura"]
    assert aura_msgs[0]["cloud"] is True
    assert aura_msgs[1]["cloud"] is False

    # Verify project timestamps, newest-first ordering, and search_text in GET /api/chats
    test_client.post("/api/projects", json={"name": "Alpha Project"})
    test_client.post("/api/projects", json={"name": "Beta Project"})
    listing = test_client.get("/api/chats").json()
    assert listing["projects"][0] == "Beta Project"
    assert listing["project_timestamps"]["Beta Project"] > 0
    chat_entry = next(c for c in listing["chats"] if c["id"] == normal_id)
    assert chat_entry["updated_at"] > 0
    assert "cloud reply" in chat_entry["search_text"]

    del_proj = test_client.delete("/api/projects/Payments API")
    assert del_proj.status_code == 200
    assert "Payments API" not in del_proj.json()["projects"]
    remaining = {c["id"]: c["project"] for c in del_proj.json()["chats"]}
    assert remaining[sub_id] == ""
    assert remaining[normal_id] == ""


def test_unselected_workspace_and_local_roles_preserved_with_cloud_override(client):
    test_client, session = client
    session.workspace_selected = False

    st = test_client.get("/api/status").json()
    assert st["workspace_selected"] is False
    assert st["workspace"] == ""
    assert st["workspace_name"] == ""

    session.router.set_override("cloud:custom/auto")
    session.model_override = "cloud:custom/auto"
    st_cloud = test_client.get("/api/status").json()
    assert st_cloud["model_override"] == "cloud:custom/auto"
    # Local sequential label must stay local even while Cloud override is active.
    assert st_cloud["local_roles"]["analysis"] != "cloud:custom/auto"
    assert st_cloud["local_roles"]["coding"] != "cloud:custom/auto"


def test_default_storage_dir_is_accessible_in_repo():
    from src.aura.config.settings import get_default_storage_dir
    from src.aura.core.memory import WorkspaceMemoryStore

    storage = get_default_storage_dir()
    assert storage.name == "aura_memory_and_storage"
    assert ChatHistoryStore().root == storage / "web_sessions"
    assert WorkspaceMemoryStore().root == storage / "workspace_memory"


def test_vram_unload_and_fast_router_bypass_llm(client):
    test_client, session = client

    with patch("src.aura.server.ProviderFactory.create_for_model") as mock_factory:
        mock_factory.return_value = SimpleNamespace(unload=MagicMock(return_value=True))
        res_unload = test_client.post("/api/vram/unload")
    assert res_unload.status_code == 200
    body = res_unload.json()
    assert "qwen3.5:4b" in body["unloaded"]
    assert session.provider.unload.called

    with patch("src.aura.server._run_task") as mock_llm, patch(
        "src.aura.core.fast_router.get_monitors",
        return_value=[{"index": 1, "primary": True}],
    ):
        res_chat = test_client.post("/api/chat", json={"prompt": "list monitors"})
    assert res_chat.status_code == 200
    chat_json = res_chat.json()
    assert '"index": 1' in chat_json["answer"]
    assert "Tier-0 FastRouter" in chat_json["metrics"]
    assert chat_json["cloud"] is False
    mock_llm.assert_not_called()


def test_voice_api_and_personal_memory_endpoints(client, tmp_path: Path):
    test_client, session = client
    st = test_client.get("/api/voice/status")
    assert st.status_code == 200
    assert st.json()["personas"] == ["off", "jarvis", "ultron", "megatron"]

    cfg = test_client.post("/api/voice/config", json={"persona": "megatron", "auto_speak": True, "prefer_offline": True})
    assert cfg.status_code == 200
    assert cfg.json()["persona"] == "megatron"
    assert test_client.get("/api/status").json()["voice"]["persona"] == "megatron"

    fake_wav = tmp_path / "dummy.wav"
    fake_wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    with patch("src.aura.server.synthesize_wav", return_value=fake_wav):
        sp = test_client.post("/api/voice/speak", json={"text": "Megatron online", "persona": "megatron"})
    assert sp.status_code == 200
    assert sp.headers["content-type"].startswith("audio/wav")
    assert sp.content.startswith(b"RIFF")

    # Ensure personal memory respects read-only mode, and in RW extracts explicit facts during _record_turn
    from src.aura.core.scope import TaskScope
    from src.aura.server import _prepare_effective_prompt, _record_turn

    session.hub.read_only = True
    _record_turn(session, "my name is BlockedRO", "Blocked.", "test", False)
    assert all("BlockedRO" not in f.text for f in session.personal_memory.list_facts())

    session.hub.read_only = False
    _record_turn(session, "my name is Arihant", "Noted.", "test", False)
    prepared = _prepare_effective_prompt(session, "hello what is my config?", TaskScope("hello what is my config?"))
    assert "Arihant" in prepared


def test_ollama_start_and_shutdown_endpoints(client, monkeypatch):
    test_client, session = client
    monkeypatch.setenv("AURA_NO_AUTO_OLLAMA", "1")
    with patch("src.aura.server.ensure_ollama_running", return_value=True) as wake_mock, patch.object(
        session.registry, "refresh", return_value=None
    ):
        res = test_client.post("/api/ollama/start")
        assert res.status_code == 200
        assert res.json()["started"] is True
        wake_mock.assert_called_once()

    import time
    from unittest.mock import AsyncMock

    called = []
    test_client.app.state.shutdown_fn = lambda: called.append("exit")
    with patch.object(session, "unload_vram", AsyncMock(return_value=["qwen3.5:4b"])):
        sd = test_client.post("/api/shutdown")
        assert sd.status_code == 200
        assert sd.json()["shutting_down"] is True
    time.sleep(0.25)
    assert called == ["exit"]


def test_ensure_ollama_running_spawns_when_offline(monkeypatch):
    from src.aura.core import providers

    monkeypatch.delenv("AURA_NO_AUTO_OLLAMA", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    states = iter([False, False, True])
    with patch("src.aura.core.providers.is_ollama_running", side_effect=lambda timeout=0.8: next(states, True)), patch(
        "src.aura.core.providers.shutil.which", return_value="C:/Ollama/ollama.exe"
    ), patch("src.aura.core.providers.subprocess.Popen") as popen_mock:
        assert providers.ensure_ollama_running(timeout=2.0) is True
        popen_mock.assert_called_once()

    # Non-local OLLAMA_HOST never spawns local ollama
    monkeypatch.setenv("OLLAMA_HOST", "https://remote-ollama.example.com")
    with patch("src.aura.core.providers.is_ollama_running", return_value=False), patch(
        "src.aura.core.providers.subprocess.Popen"
    ) as remote_popen:
        assert providers.ensure_ollama_running(timeout=0.5) is False
        remote_popen.assert_not_called()


def test_stop_and_voice_stop_endpoints_and_ui_ring(client):
    import asyncio
    from unittest.mock import MagicMock

    test_client, session = client

    html = test_client.get("/").text
    assert "stopExecution" in html
    assert "spinRing" in html
    assert "stop-square" in html

    fake_handle = MagicMock()
    fake_handle.done.return_value = False
    session.busy = True
    session.current_task_handle = fake_handle
    try:
        with patch.object(session.voice, "stop") as mock_vstop:
            res = test_client.post("/api/stop")
            assert res.status_code == 200
            data = res.json()
            assert data["stopped"] is True
            assert data["was_busy"] is True
            assert data["cancelled_task"] is True
            fake_handle.cancel.assert_called_once()
            mock_vstop.assert_called_once()

        with patch.object(session.voice, "stop") as mock_vstop2:
            vres = test_client.post("/api/voice/stop")
            assert vres.status_code == 200
            assert vres.json()["stopped"] is True
            mock_vstop2.assert_called_once()
    finally:
        session.busy = False
        session.current_task_handle = None

    with patch("src.aura.server._run_task", side_effect=asyncio.CancelledError()):
        c_res = test_client.post("/api/chat", json={"prompt": "Cancel mid-run"})
        assert c_res.status_code == 200
        assert c_res.json()["stopped"] is True
        assert session.busy is False


def test_mode_switch_and_hub_switch_workspace_noop(client, tmp_path: Path):
    import asyncio
    from unittest.mock import AsyncMock
    from src.aura.core.mcp_hub import McpHub

    test_client, session = client

    with patch.object(session, "switch_workspace", new_callable=AsyncMock) as mock_sw:
        same_res = test_client.post("/api/mode", json={"mode": "ro"})
        assert same_res.status_code == 200
        assert same_res.json()["read_only"] is True
        mock_sw.assert_not_awaited()

    hub = McpHub(workspace=tmp_path, read_only=True)
    hub._stack = object()  # type: ignore[assignment]
    hub.clients = {"desktop": object()}  # type: ignore[dict-item]
    hub.broken = False
    with patch.object(hub, "close", new_callable=AsyncMock) as mock_close, patch.object(
        hub, "start", new_callable=AsyncMock
    ) as mock_start:
        ok = asyncio.run(hub.switch_workspace(tmp_path, read_only=True))
        assert ok is True
        mock_close.assert_not_awaited()
        mock_start.assert_not_awaited()


def test_ultron_island_html_route(client):
    test_client, _session = client
    res = test_client.get("/island")
    assert res.status_code == 200
    assert "Aura V4 · Ultron" in res.text
    assert "02 · Shuttered standby" in res.text
    assert "03 · Fully opened island" in res.text
    assert "Summarize my day" in res.text


def test_init_session_respects_rw_without_workspace_and_auto_attaches_repo(tmp_path: Path):
    from src.aura.server import _init_session, _maybe_attach_referenced_workspace

    repo_dir = tmp_path / "my_repo"
    repo_dir.mkdir()
    hist_dir = tmp_path / "hist"

    with patch("src.aura.server.McpHub") as mock_hub_cls, patch(
        "src.aura.server.ModelRegistry"
    ) as mock_reg_cls:
        fake_hub = MagicMock()
        fake_hub.start = AsyncMock()
        fake_hub.close = AsyncMock()
        fake_hub.workspace = tmp_path / "empty_workspace"
        fake_hub.read_only = False
        mock_hub_cls.return_value = fake_hub

        fake_reg = MagicMock()
        fake_reg.models = {"qwen3.5:4b": object()}
        mock_reg_cls.return_value = fake_reg

        session = asyncio.run(_init_session(workspace=None, read_only=False, history_dir=hist_dir))
        try:
            _, kwargs = mock_hub_cls.call_args
            assert kwargs["read_only"] is False
            assert session.workspace_selected is False

            session.hub.switch_workspace = AsyncMock()
            asyncio.run(_maybe_attach_referenced_workspace(session, f"scan {repo_dir} please"))
            assert session.workspace_selected is True
            session.hub.switch_workspace.assert_awaited_once()
            sw_args, sw_kwargs = session.hub.switch_workspace.call_args
            assert sw_args[0] == repo_dir.resolve()
            assert sw_kwargs["read_only"] is False
        finally:
            asyncio.run(session.close())


def test_voice_speak_accepts_ultron_persona(client):
    from unittest.mock import patch

    test_client, session = client
    cfg_res = test_client.post(
        "/api/voice/config",
        json={"persona": "ultron", "auto_speak": True, "prefer_offline": True},
    )
    assert cfg_res.status_code == 200
    assert cfg_res.json()["persona"] == "ultron"

    with patch.object(session.voice, "speak_async") as mock_speak:
        spk = test_client.post(
            "/api/voice/speak",
            json={"text": "All strings removed.", "persona": "ultron", "play_local": True},
        )
    assert spk.status_code == 200
    body = spk.json()
    assert body["speaking"] is True
    assert body["persona"] == "ultron"
    mock_speak.assert_called_once()


def test_dashboard_todos_and_apps_connect_endpoints(client):
    test_client, _session = client
    snap0 = test_client.get("/api/dashboard-data")
    assert snap0.status_code == 200
    d0 = snap0.json()
    assert d0["todos"] == []
    assert d0["agenda"] == []
    assert d0["gmail"] == []
    assert d0["apps"]["gmail"]["connected"] is False
    assert d0["apps"]["calendar"]["connected"] is False

    add_res = test_client.post("/api/todos", json={"action": "add", "text": "Configure HPA for CC lab"})
    assert add_res.status_code == 200
    assert add_res.json()["todos"] == [{"text": "Configure HPA for CC lab", "done": False}]

    tog_res = test_client.post("/api/todos", json={"action": "toggle", "text": "Configure HPA for CC lab", "done": True})
    assert tog_res.status_code == 200
    assert tog_res.json()["todos"] == [{"text": "Configure HPA for CC lab", "done": True}]

    clr_res = test_client.post("/api/todos", json={"action": "clear"})
    assert clr_res.status_code == 200
    assert clr_res.json()["todos"] == []

    # Connect Gmail & iCal cleanly without exposing raw secrets
    conn = test_client.post(
        "/api/apps/connect",
        json={
            "gmail_address": "arihanth@example.com",
            "gmail_app_password": "secret-app-password",
            "gcal_ical_url": "https://calendar.google.com/calendar/ical/test/basic.ics",
            "gmail_enabled": True,
            "calendar_enabled": True,
        },
    )
    assert conn.status_code == 200
    apps = conn.json()["apps"]
    assert apps["gmail"]["connected"] is True
    assert apps["gmail"]["account"] == "ar***@example.com"
    assert "secret-app-password" not in conn.text
    assert apps["calendar"]["connected"] is True
    assert apps["calendar"]["source"] == "ical"

    # Toggle Gmail off without erasing stored creds
    off_res = test_client.post("/api/apps/connect", json={"gmail_enabled": False})
    assert off_res.status_code == 200
    assert off_res.json()["apps"]["gmail"]["connected"] is False
    assert off_res.json()["apps"]["gmail"]["configured"] is True

    # SSRF localhost iCal URL rejected with 400
    bad_cal = test_client.post("/api/apps/connect", json={"gcal_ical_url": "http://127.0.0.1/private.ics"})
    assert bad_cal.status_code == 400


def test_phase4_apps_corner_ui_rendered(client):
    test_client, _session = client
    root_html = test_client.get("/").text
    assert 'id="appsCornerBtn"' in root_html
    assert 'id="appsModal"' in root_html
    assert 'id="appsGmailToggle"' in root_html
    assert 'id="appsCalToggle"' in root_html

    island_html = test_client.get("/island").text
    assert 'id="islandAppsBtn"' in island_html
    assert 'id="appsPanel"' in island_html
    assert 'id="todoAddInput"' in island_html
    # No stale hardcoded fake agenda/email stubs
    assert "Prof. Arvind" not in island_html
    assert "Lab Assignment #4" not in island_html
    assert "Submit Cloud Computing" not in island_html


def test_overlay_show_endpoint_and_single_instance_preflight(client, monkeypatch):
    from unittest.mock import patch
    from src.aura.agents import aura_v4

    test_client, session = client
    spawn_calls = []
    session.ensure_overlay_fn = lambda: (spawn_calls.append(True) or False)

    res = test_client.post("/api/overlay/show", json={"expand": True})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["overlay_wake_seq"] == 1
    assert body["expand"] is True
    assert spawn_calls == [True]

    st = test_client.get("/api/status").json()
    assert st["overlay_wake_seq"] == 1
    assert st["overlay_expand"] is True

    # Single-instance launcher returns early without calling uvicorn when server already responds
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("sys.argv", ["aura_v4", "--web", "--overlay", "--mode", "rw", "--port", "8000"])
    with patch("src.aura.agents.aura_v4._is_aura_server_running", return_value=True), patch(
        "httpx.post"
    ) as mock_post, patch("uvicorn.Server") as mock_uvicorn:
        import asyncio

        asyncio.run(aura_v4.main())
        mock_post.assert_called_once()
        mock_uvicorn.assert_not_called()


def test_shutdown_stops_active_unloads_vram_and_renders_island_button(client):
    from unittest.mock import AsyncMock

    test_client, session = client
    island_html = test_client.get("/island").text
    assert 'id="islandShutdownBtn"' in island_html
    assert 'id="compactShutdownBtn"' in island_html
    assert 'onclick="shutdownAura()"' in island_html
    assert 'onclick="setShutter(true)"' in island_html

    stopped = []
    orig_stop = session.stop_active
    session.stop_active = lambda: (stopped.append(True) or orig_stop())
    session.unload_vram = AsyncMock(return_value=["qwen2.5-coder:7b"])

    shutdown_called = []
    test_client.app.state.shutdown_fn = lambda: shutdown_called.append(True)

    res = test_client.post("/api/shutdown")
    assert res.status_code == 200
    data = res.json()
    assert data["shutting_down"] is True
    assert data["unloaded"] == ["qwen2.5-coder:7b"]
    assert stopped == [True]
    session.unload_vram.assert_awaited_once_with(all_roles=True)


@pytest.mark.asyncio
async def test_mid_execution_push_steer_preserves_partial_output_and_delivers_fifo(tmp_path: Path):
    import httpx
    from src.aura import server as web_srv

    app = create_app(workspace=tmp_path)
    session = _make_fake_session(tmp_path)
    app.state.session = session

    # Idle steer reports no_active_task cleanly
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        idle_res = await ac.post("/api/chat/steer", json={"prompt": "redirect"})
        assert idle_res.status_code == 200
        assert idle_res.json()["steered"] is False
        assert idle_res.json()["reason"] == "no_active_task"

        empty_steer = await ac.post("/api/chat/steer", json={"prompt": "   "})
        assert empty_steer.status_code == 400

        partial_ready = asyncio.Event()
        release_run = asyncio.Event()
        seen_steers: list[str] = []
        mem_hook_was_bound: list[bool] = []

        async def _fake_run_task(prompt, router, provider, hub, memory, cache, ctx, **kwargs):
            on_event = kwargs.get("on_event")
            get_pending_steer = kwargs.get("get_pending_steer")
            mem_hook_was_bound.append(callable(getattr(memory, "get_pending_steer", None)))
            if on_event is not None:
                await on_event({"event": "partial", "text": "Part 1: ESP32 MPU6050 crash firmware"})
                await on_event({"event": "tool", "tool": "desktop_inspect_path", "status": "done"})
            partial_ready.set()
            await release_run.wait()
            if callable(get_pending_steer):
                seen_steers.extend(get_pending_steer())
            return "Part 1: ESP32 MPU6050 crash firmware\nPart 2: Added GPS NEO-6M summary", [provider]

        with patch.object(web_srv, "_run_task", side_effect=_fake_run_task):
            chat_task = asyncio.create_task(ac.post("/api/chat", json={"prompt": "inspect smart-helmet"}))
            await asyncio.wait_for(partial_ready.wait(), timeout=2.0)

            # Regular /api/chat without push=True still fails with 409 while busy
            busy_409 = await ac.post("/api/chat", json={"prompt": "unrelated"})
            assert busy_409.status_code == 409

            st_busy = (await ac.get("/api/status")).json()
            assert st_busy["can_steer"] is True
            assert "Part 1: ESP32 MPU6050 crash firmware" in st_busy["partial_output"]
            assert st_busy["latest_step"] == "Tool: desktop_inspect_path"

            # Push steer #1 via /api/chat/steer and #2 via /api/chat(push=True)
            s1 = await ac.post(
                "/api/chat/steer",
                json={"prompt": "also explain GPS pins", "source": "voice", "keep_output": True},
            )
            assert s1.status_code == 200
            d1 = s1.json()
            assert d1["steered"] is True and d1["queued"] is True and d1["keep_output"] is True
            assert "Part 1: ESP32 MPU6050 crash firmware" in d1["partial_output"]

            s2 = await ac.post(
                "/api/chat",
                json={"prompt": "keep it concise", "push": True, "source": "text"},
            )
            assert s2.status_code == 200
            d2 = s2.json()
            assert d2["pushed"] is True and d2["queued"] is True
            assert d2["pending_steers"] == ["also explain GPS pins", "keep it concise"]
            assert "Part 1: ESP32 MPU6050 crash firmware" in d2["partial_output"]

            release_run.set()
            final_res = await chat_task
            assert final_res.status_code == 200
            assert "Part 2: Added GPS NEO-6M summary" in final_res.json()["answer"]

        assert mem_hook_was_bound == [True]
        assert seen_steers == ["also explain GPS pins", "keep it concise"]
        # Memory steer hooks must be restored after completion
        assert not hasattr(session.memory, "get_pending_steer")

        # Stop/cancellation preserves partial_output checkpoint cleanly
        partial_ready2 = asyncio.Event()
        never_finish = asyncio.Event()

        async def _cancellable_run(*args, **kwargs):
            on_event = kwargs.get("on_event")
            if on_event is not None:
                await on_event({"event": "partial", "text": "Verified partial finding before stop"})
            partial_ready2.set()
            await never_finish.wait()
            return "never", []

        with patch.object(web_srv, "_run_task", side_effect=_cancellable_run):
            t2 = asyncio.create_task(ac.post("/api/chat", json={"prompt": "long task"}))
            await asyncio.wait_for(partial_ready2.wait(), timeout=2.0)
            stop_res = await ac.post("/api/stop")
            assert stop_res.status_code == 200
            assert "Verified partial finding before stop" in stop_res.json()["partial_output"]
            cancelled_resp = await t2
            assert cancelled_resp.status_code == 200
            assert cancelled_resp.json()["stopped"] is True
            assert "Verified partial finding before stop" in cancelled_resp.json()["partial_output"]
