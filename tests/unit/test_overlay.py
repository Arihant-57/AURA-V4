"""Unit tests for the Windows Dynamic Island Floating Pill overlay."""

from __future__ import annotations

from src.aura.overlay import (
    IslandGeometry,
    _CpuSampler,
    clean_reply_and_metrics,
    compute_island_geometry,
    compute_spring_frames,
    enable_high_dpi_awareness,
    format_friendly_error,
    format_pill_badge,
    next_persona,
)


def test_geometry_and_badge_helpers():
    c = compute_island_geometry(1920, 1080, expanded=False)
    s = compute_island_geometry(1920, 1080, expanded=False, shuttered=True)
    e = compute_island_geometry(1920, 1080, expanded=True)
    assert s.width == 220 and s.height == 32 and s.y == 0
    assert c.width == 310 and c.height == 40 and c.y == 0
    assert e.width == 960 and e.height == 520 and e.y == 0
    assert c.width < e.width and c.height < e.height
    assert c.x == (1920 - c.width) // 2
    assert e.x == (1920 - e.width) // 2

    small = compute_island_geometry(880, 500, expanded=True)
    assert small.y == 0
    assert small.width == 856 and small.height == 452
    assert isinstance(enable_high_dpi_awareness(), bool)

    assert next_persona("off") == "jarvis"
    assert next_persona("jarvis") == "ultron"
    assert next_persona("ultron") == "megatron"
    assert next_persona("megatron") == "off"

    assert "RO" in format_pill_badge(False, "AURA Jarvis", "jarvis", True)
    assert "U · RW" in format_pill_badge(True, "Synthesizing...", "ultron", False)
    assert "M · RW" in format_pill_badge(True, "Thinking...", "megatron", False)

    body, meta = clean_reply_and_metrics(
        "Hello Arihant!\n[[Metrics (qwen3.5:4b): prompt_eval_tokens=unavailable, num_ctx=12288]]",
        "[[Metrics (qwen3.5:4b): prompt_eval_tokens=unavailable]]",
    )
    assert body == "Hello Arihant!"
    assert "[[Metrics" not in body
    assert meta == "Model: qwen3.5:4b"

    err = format_friendly_error("Error: Cannot connect to Ollama: Failed to connect to Ollama.")
    assert "Start Ollama" in err


def test_dynamic_island_widget_lifecycle():
    """Run real Tk DynamicIslandPill layout, tools menu, mic toggle, typing focus & shutdown in an isolated subprocess."""
    import subprocess
    import sys

    script = (
        "import tkinter as tk\n"
        "from unittest.mock import patch\n"
        "from src.aura.overlay import DynamicIslandPill, JarvisBridgeClient\n"
        "from src.aura.core.mic_listener import MicListener\n"
        "called = []\n"
        "stopped = []\n"
        "sent_prompts = []\n"
        "mic_actions = []\n"
        "with patch.object(JarvisBridgeClient, 'get_status', return_value={'read_only': False, 'ollama_up': True, 'voice': {'persona': 'jarvis', 'speaking': False}}), "
        "patch.object(JarvisBridgeClient, 'set_voice', return_value={'persona': 'jarvis', 'status': {'read_only': False, 'ollama_up': True, 'voice': {'persona': 'jarvis'}}}), "
        "patch.object(JarvisBridgeClient, 'send_chat', side_effect=lambda p: (sent_prompts.append(p) or {'answer': 'All systems online.', 'metrics': '', 'status': None})), "
        "patch.object(JarvisBridgeClient, 'speak_local', lambda *a, **k: None), "
        "patch.object(JarvisBridgeClient, 'stop', side_effect=lambda: (stopped.append(True) or {'stopped': True})), "
        "patch.object(JarvisBridgeClient, 'get_dashboard_data', return_value={'todos': [], 'agenda': [], 'gmail': [], 'briefing': ['No to-dos added yet'], 'apps': {'gmail': {'connected': False}, 'calendar': {'connected': False}}}), "
        "patch.object(JarvisBridgeClient, 'todo_action', side_effect=lambda *a, **k: {'todos': [], 'agenda': [], 'gmail': [], 'briefing': []}), "
        "patch.object(JarvisBridgeClient, 'get_apps_status', return_value={'apps': {'gmail': {'connected': False}, 'calendar': {'connected': False}}}), "
        "patch.object(JarvisBridgeClient, 'shutdown', side_effect=lambda: called.append(True)), "
        "patch.object(MicListener, 'start', side_effect=lambda: (mic_actions.append('start') or True)), "
        "patch.object(MicListener, 'pause', side_effect=lambda: mic_actions.append('pause')), "
        "patch.object(MicListener, 'stop', side_effect=lambda: mic_actions.append('stop')), "
        "patch.object(DynamicIslandPill, '_start_hotkey_listener', lambda self: None):\n"
        "    try:\n"
        "        pill = DynamicIslandPill('http://127.0.0.1:8000')\n"
        "    except tk.TclError:\n"
        "        raise SystemExit(0)\n"
        "    pill.root.attributes('-alpha', 0.0)\n"
        "    pill.root.update()\n"
        "    assert pill.expanded is False\n"
        "    assert pill.mic_enabled is False\n"
        "    assert mic_actions == []\n"
        "    assert not pill.stop_canvas.winfo_manager()\n"
        "    assert pill.compact_voice_btn.winfo_manager()\n"
        "    assert not pill.apps_btn.winfo_manager()\n"
        "    assert pill.root.winfo_width() == 310 and pill.root.winfo_height() == 40\n"
        "    pill.expand()\n"
        "    pill.root.update()\n"
        "    assert pill.expanded is True\n"
        "    assert not pill.compact_voice_btn.winfo_manager()\n"
        "    assert pill.apps_btn.winfo_manager()\n"
        "    pill._open_apps_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._apps_popover_visible is True\n"
        "    pill._close_apps_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._apps_popover_visible is False\n"
        "    # Composer entry + tools picker + mic toggle + send must fit cleanly inside expanded card height\n"
        "    card_h = pill.root.winfo_height()\n"
        "    entry_bottom = pill.input_row.winfo_y() + pill.input_row.winfo_height()\n"
        "    assert entry_bottom <= card_h, f'{entry_bottom} > {card_h}'\n"
        "    assert pill.tools_btn.cget('text') == '+ Tools ▴'\n"
        "    assert pill.mic_btn.cget('text') == '🎙 Mic'\n"
        "    assert pill.composer_stop_btn.cget('text') == '■ Stop Aura'\n"
        "    assert pill.hdr_stop_btn.cget('text') == '⏸ Pause'\n"
        "    assert pill.hdr_rw_btn.winfo_manager()\n"
        "    assert pill.persona_btn.winfo_manager()\n"
        "    assert pill.tab_btns['deck'].cget('text') == 'Hub & Focus'\n"
        "    assert pill.web_ui_btn.winfo_manager()\n"
        "    assert pill.jarvis_tab_btn.winfo_manager()\n"
        "    assert pill.entry.cget('wrap') == 'word'\n"
        "    assert int(pill.entry.cget('height')) >= 3\n"
        "    assert pill.entry.winfo_width() >= 550\n"
        "    assert pill.composer_stop_btn.winfo_rootx() + pill.composer_stop_btn.winfo_width() <= pill.input_row.winfo_rootx() + pill.input_row.winfo_width()\n"
        "    assert pill.shutdown_btn.winfo_parent() == str(pill.hdr_right)\n"
        "    assert str(pill.shutdown_btn) not in [str(c) for c in pill.tab_bar.winfo_children()]\n"
        "    assert pill.shutdown_btn.winfo_width() >= 48\n"
        "    pill.root.tk.call('tk', 'scaling', 1.75)\n"
        "    pill.root.update()\n"
        "    assert pill.shutdown_btn.winfo_width() >= 48\n"
        "    pill.collapse()\n"
        "    pill.root.update()\n"
        "    assert pill.shutdown_btn.winfo_width() >= 28\n"
        "    pill.expand()\n"
        "    pill.root.update()\n"
        "    assert pill.shutdown_btn.winfo_width() >= 48\n"
        "    assert pill.agent_status_lbl.cget('text') == '@planner -> @coder'\n"
        "    assert pill.agent_prog_pct_lbl.cget('text') == '0%'\n"
        "    pill._open_model_menu()\n"
        "    pill.root.update()\n"
        "    assert pill._model_popover_visible is True\n"
        "    pill._close_model_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._model_popover_visible is False\n"
        "    pill._apply_status({'read_only': False, 'available_tools': [{'name': 'desktop_open_path', 'description': 'Open file or folder directly'}, {'name': 'browser_navigate', 'description': 'Navigate web'}]})\n"
        "    pill._on_tools_enter()\n"
        "    pill.root.update()\n"
        "    assert pill._tools_popover_visible is True\n"
        "    assert pill._tools_pinned is False\n"
        "    assert '2' in pill._tools_hdr_lbl.cget('text')\n"
        "    orig_over = pill._is_cursor_over_tools\n"
        "    pill._is_cursor_over_tools = lambda: True\n"
        "    pill._check_tools_hover_leave()\n"
        "    assert pill._tools_popover_visible is True\n"
        "    pill._toggle_tools_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._tools_popover_visible is True\n"
        "    assert pill._tools_pinned is True\n"
        "    assert pill.tools_popover.winfo_manager() == 'place'\n"
        "    pill._is_cursor_over_tools = lambda: False\n"
        "    pill._check_tools_hover_leave()\n"
        "    assert pill._tools_popover_visible is True\n"
        "    pill._toggle_tools_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._tools_popover_visible is False\n"
        "    assert pill._tools_pinned is False\n"
        "    pill._on_tools_enter()\n"
        "    assert pill._tools_popover_visible is True\n"
        "    pill._check_tools_hover_leave()\n"
        "    assert pill._tools_popover_visible is False\n"
        "    pill._is_cursor_over_tools = orig_over\n"
        "    pill._focus_entry()\n"
        "    assert pill._has_placeholder is False\n"
        "    pill.toggle_mic()\n"
        "    assert pill.mic_enabled is True\n"
        "    assert 'start' in mic_actions\n"
        "    # During voice output, speaker echo is ignored unless user says 'stop'\n"
        "    pill._set_voice_speaking(True)\n"
        "    pill.root.update()\n"
        "    assert pill.stop_canvas.winfo_manager()\n"
        "    assert pill.send_btn.cget('text') == '■ Stop'\n"
        "    before_echo = len(sent_prompts)\n"
        "    pill._on_voice_transcript('all systems online')\n"
        "    assert len(sent_prompts) == before_echo\n"
        "    pill._on_voice_transcript('Hey Jarvis, stop!')\n"
        "    pill.root.update()\n"
        "    assert pill.voice_speaking is False\n"
        "    assert pill.busy is False\n"
        "    assert not pill.stop_canvas.winfo_manager()\n"
        "    assert pill.send_btn.cget('text') == 'Send'\n"
        "    pill._on_voice_transcript('hey jarvis, what are you doing?')\n"
        "    pill.root.update()\n"
        "    pill.collapse()\n"
        "    pill.root.update()\n"
        "    assert pill.expanded is False\n"
        "    assert pill.island_state == 'compact'\n"
        "    # Ultron 3-state transition: Compact -> Shuttered -> Compact -> Expanded\n"
        "    pill.enter_shuttered()\n"
        "    pill.root.update()\n"
        "    assert pill.shuttered is True\n"
        "    assert pill.island_state == 'shuttered'\n"
        "    pill.wake_from_shutter()\n"
        "    pill.root.update()\n"
        "    assert pill.shuttered is False\n"
        "    assert pill.island_state == 'compact'\n"
        "    # Interactive Todo progress check (zero hardcoded fake todos on boot)\n"
        "    assert pill._todo_items == []\n"
        "    assert pill.todo_count_lbl.cget('text') == '(0/0)'\n"
        "    pill._apply_dashboard({'todos': [{'text': 'Finish CC lab report', 'done': False}, {'text': 'Deploy autoscaler', 'done': True}], 'agenda': [], 'gmail': [], 'briefing': ['1 open to-do']})\n"
        "    assert pill.todo_count_lbl.cget('text') == '(1/2)'\n"
        "    pill._todo_vars[0].set(True)\n"
        "    pill._on_todo_toggle(0)\n"
        "    assert pill.todo_count_lbl.cget('text') == '(2/2)'\n"
        "    pill.expand()\n"
        "    pill.root.update()\n"
        "    assert pill.expanded is True\n"
        "    pill._on_reply('Done', '', None)\n"
        "    pill.root.update()\n"
        "    assert pill.agent_prog_pct_lbl.cget('text') == 'Done · 100%'\n"
        "    assert pill.check_outside_click(pill.root.winfo_rootx() - 40, pill.root.winfo_rooty() + 20) is True\n"
        "    pill.root.update()\n"
        "    assert pill.expanded is False\n"
        "    assert pill.island_state == 'compact'\n"
        "    pill.shutdown_app()\n"
        "    pill.root.mainloop()\n"
        "    assert called == [True]\n"
        "    assert 'stop' in mic_actions\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_overlay_hotkey_press_expands_and_arms_mic(monkeypatch):
    from types import SimpleNamespace
    from src.aura.overlay import DynamicIslandPill

    calls = []
    pill = SimpleNamespace(
        expanded=False,
        mic_enabled=False,
        expand=lambda: (setattr(pill, "expanded", True), calls.append("expand")),
        collapse=lambda: (setattr(pill, "expanded", False), calls.append("collapse")),
        toggle_mic=lambda: (setattr(pill, "mic_enabled", not pill.mic_enabled), calls.append(f"mic:{pill.mic_enabled}")),
        toggle_expand=lambda: calls.append("toggle_expand"),
    )

    monkeypatch.delenv("AURA_HOTKEY_NO_AUTO_MIC", raising=False)
    DynamicIslandPill.on_hotkey_press(pill)
    assert pill.expanded is True
    assert pill.mic_enabled is True
    assert calls == ["expand", "mic:True"]

    calls.clear()
    DynamicIslandPill.on_hotkey_press(pill)
    assert pill.expanded is False
    assert pill.mic_enabled is False
    assert calls == ["mic:False", "collapse"]

    calls.clear()
    monkeypatch.setenv("AURA_HOTKEY_NO_AUTO_MIC", "1")
    DynamicIslandPill.on_hotkey_press(pill)
    assert calls == ["toggle_expand"]


def test_overlay_top_hwnd_and_single_instance_wake():
    from types import SimpleNamespace
    from src.aura.overlay import DynamicIslandPill, _get_top_hwnd, signal_overlay_wake_event

    assert _get_top_hwnd(0) == 0
    assert isinstance(signal_overlay_wake_event(), bool)

    woken = []
    pill = SimpleNamespace(
        read_only=False,
        ollama_up=True,
        available_models=[],
        available_cloud_models=[],
        active_model_name="qwen3.5:4b",
        selected_agent="auto",
        persona="jarvis",
        busy=False,
        voice_speaking=False,
        shuttered=True,
        expanded=False,
        _last_wake_seq=0,
        _event_woken_at=None,
        _sync_stop_controls=lambda: None,
        _update_header=lambda: None,
        wake_from_shutter=lambda: (setattr(pill, "shuttered", False), woken.append("unshutter")),
        expand=lambda: (setattr(pill, "expanded", True), woken.append("expand")),
        _ensure_pinned_visible=lambda: woken.append("pin"),
    )
    pill.wake_and_expand = lambda **k: DynamicIslandPill.wake_and_expand(pill, **k)

    DynamicIslandPill._apply_status(pill, {"read_only": False, "overlay_wake_seq": 2, "overlay_expand": True})
    assert pill._last_wake_seq == 2
    assert pill.shuttered is False
    assert pill.expanded is True
    assert woken == ["unshutter", "expand", "pin"]


def test_spring_motion_and_smooth_shutter():
    from types import SimpleNamespace
    from src.aura.overlay import DynamicIslandPill

    c = compute_island_geometry(1920, 1080, expanded=False, shuttered=False)
    e = compute_island_geometry(1920, 1080, expanded=True, shuttered=False)
    s = compute_island_geometry(1920, 1080, expanded=False, shuttered=True)

    expand_frames = compute_spring_frames(c, e)
    assert len(expand_frames) == 16
    assert expand_frames[-1] == e
    assert expand_frames[0].width > c.width and expand_frames[0].height > c.height
    assert all(f.y == 0 for f in expand_frames)
    assert all(
        expand_frames[i].width <= expand_frames[i + 1].width
        and expand_frames[i].height <= expand_frames[i + 1].height
        for i in range(len(expand_frames) - 1)
    )

    collapse_frames = compute_spring_frames(e, c, steps=14)
    assert len(collapse_frames) == 14
    assert collapse_frames[-1] == c
    assert all(f.y == 0 for f in collapse_frames)
    assert all(
        collapse_frames[i].width >= collapse_frames[i + 1].width
        and collapse_frames[i].height >= collapse_frames[i + 1].height
        for i in range(len(collapse_frames) - 1)
    )

    shutter_calls = []
    pill = SimpleNamespace(
        root=SimpleNamespace(winfo_screenwidth=lambda: 1920, winfo_screenheight=lambda: 1080),
        _current_geo=c,
        expanded=False,
        shuttered=False,
        mic_enabled=False,
        busy=False,
        voice_speaking=False,
        _close_model_popover=lambda: None,
        _close_apps_popover=lambda: None,
        _apply_state_layout=lambda: None,
        _apply_geometry=lambda animate_from=None: shutter_calls.append(animate_from),
        _ensure_pinned_visible=lambda: None,
    )
    DynamicIslandPill.enter_shuttered(pill)
    assert pill.shuttered is True
    assert shutter_calls[-1] == c

    pill._current_geo = s
    DynamicIslandPill.wake_from_shutter(pill)
    assert pill.shuttered is False
    assert shutter_calls[-1] == s


def test_push_steer_preserved_output_speech_barge_in_and_tools_popover_search():
    import subprocess
    import sys
    from src.aura.overlay import apply_window_region

    assert apply_window_region(0, 960, 520, 48, top_flush=True) is False
    script = (
        "import time\n"
        "from src.aura import overlay\n"
        "from src.aura.core.mic_listener import MicListener\n"
        "overlay._poll_hotkey_state = lambda *a, **k: False\n"
        "steer_calls, stop_voice_calls, send_calls, shutdown_calls = [], [], [], []\n"
        "overlay.JarvisBridgeClient.get_status = lambda self: {'read_only': False, 'ollama_up': True, 'active_model': 'qwen3.5:4b', 'available_models': ['qwen3.5:4b'], 'available_tools': [{'name': 'desktop_inspect_cursor', 'description': 'Inspect UI under cursor'}, {'name': 'desktop_inspect_path', 'description': 'Read folder metadata'}, {'name': 'jarvis_research', 'description': 'Research web topic'}]}\n"
        "overlay.JarvisBridgeClient.get_dashboard_data = lambda self: {'todos': [], 'apps': {'apps': [], 'browser_tabs': 0}}\n"
        "overlay.JarvisBridgeClient.steer_chat = lambda self, prompt, agent_role=None, source='text', keep_output=True: (steer_calls.append((prompt, source, keep_output)) or {'queued': True, 'steer_seq': len(steer_calls), 'status': None})\n"
        "overlay.JarvisBridgeClient.stop_voice = lambda self: (stop_voice_calls.append(True) or {'stopped': True})\n"
        "overlay.JarvisBridgeClient.send_chat = lambda self, prompt, agent_role=None, push=False: (send_calls.append((prompt, push)) or {'answer': 'Final follow-up', 'metrics': '', 'status': None})\n"
        "overlay.JarvisBridgeClient.shutdown = lambda self: shutdown_calls.append(True)\n"
        "MicListener.start = lambda self: None\n"
        "MicListener.stop = lambda self: None\n"
        "pill = overlay.DynamicIslandPill('http://127.0.0.1:8765')\n"
        "pill.root.update()\n"
        "assert pill._current_geo.y == 0\n"
        "pill.expand()\n"
        "pill.root.update()\n"
        "assert pill._current_geo.y == 0\n"
        "assert pill._current_geo.width <= 960 and pill._current_geo.height <= 520\n"
        "pill._append_rich([('Partial finding: smart-helmet uses MPU6050\\n', 'bot_body')])\n"
        "pill.busy = True\n"
        "pill._sync_stop_controls()\n"
        "pill.root.update()\n"
        "assert pill.send_btn.cget('text') == '➤ Push'\n"
        "pill._clear_placeholder()\n"
        "pill.entry.delete(0, 'end')\n"
        "pill.entry.insert(0, 'research alternatives')\n"
        "pill._submit()\n"
        "deadline = time.monotonic() + 2.0\n"
        "while not steer_calls and time.monotonic() < deadline:\n"
        "    pill.root.update(); time.sleep(0.02)\n"
        "assert steer_calls == [('research alternatives', 'text', True)]\n"
        "assert send_calls == []\n"
        "assert 'Partial finding: smart-helmet uses MPU6050' in pill.output.get('1.0', 'end')\n"
        "pill.busy = False\n"
        "pill.voice_speaking = True\n"
        "pill.mic_enabled = True\n"
        "pill._sync_stop_controls()\n"
        "pill._on_speech_onset()\n"
        "deadline = time.monotonic() + 2.0\n"
        "while not stop_voice_calls and time.monotonic() < deadline:\n"
        "    pill.root.update(); time.sleep(0.02)\n"
        "assert stop_voice_calls == [True]\n"
        "assert pill.voice_speaking is False\n"
        "assert pill.state_label == 'ULTRON VOICE · LISTENING'\n"
        "pill._on_voice_transcript('check calendar')\n"
        "deadline = time.monotonic() + 2.0\n"
        "while not send_calls and time.monotonic() < deadline:\n"
        "    pill.root.update(); time.sleep(0.02)\n"
        "assert send_calls == [('check calendar', False)]\n"
        "pill.available_tools = [{'name': 'desktop_inspect_cursor', 'description': 'Inspect cursor UIA'}, {'name': 'desktop_inspect_path', 'description': 'Read folder contents'}, {'name': 'jarvis_research', 'description': 'Web research'}]\n"
        "pill._open_tools_popover(pinned=False)\n"
        "assert pill._tools_popover_visible is True and pill._tools_pinned is False\n"
        "pill._toggle_tools_popover()\n"
        "assert pill._tools_pinned is True\n"
        "assert pill._tools_hdr_lbl.cget('text') == 'AVAILABLE TOOLS (3)'\n"
        "pill._tools_filter_has_hint = False\n"
        "pill._tools_filter_var.set('inspect')\n"
        "pill._populate_tools_popover()\n"
        "assert pill._tools_hdr_lbl.cget('text') == 'TOOLS (2/3)'\n"
        "pill._toggle_tools_popover()\n"
        "assert pill._tools_popover_visible is False\n"
        "pill.shutdown_app()\n"
        "pill.root.mainloop()\n"
        "assert shutdown_calls == [True]\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_overlay_model_choices_clean_without_unconfigured_cloud():
    from types import SimpleNamespace
    from src.aura.overlay import DynamicIslandPill, _format_cloud_model_label

    assert _format_cloud_model_label("cloud:custom/auto") == "FreeLLMAPI · Auto"
    assert _format_cloud_model_label("cloud:gemini/gemini-3.8-flash") == "Gemini 3.8 · API"
    assert _format_cloud_model_label("cloud:auto") == "Cloud · Auto"

    fake = SimpleNamespace(
        available_models=["qwen3.5:4b", "qwen2.5-coder:7b"],
        available_cloud_models=["cloud:custom/auto", "cloud:gemini/gemini-3.8-flash", "cloud:auto"],
    )
    choices = DynamicIslandPill._collect_model_choices(fake)
    labels = [lbl for _, lbl in choices]
    assert "FreeLLMAPI · Auto" in labels
    assert "Gemini 3.8 · API" in labels
    assert "Cloud · Auto" in labels
    assert "Claude · API" not in labels
    assert "Gemini 2.5 · API" not in labels


def test_overlay_header_no_clipping_and_apps_catalog_panel():
    import subprocess
    import sys

    sample_stats = _CpuSampler().sample()
    assert "gpu_pct" in sample_stats and "vram_pct" in sample_stats
    script = (
        "import tkinter as tk\n"
        "from unittest.mock import patch\n"
        "from src.aura.overlay import DynamicIslandPill\n"
        "with patch.object(DynamicIslandPill, '_refresh_status_async', lambda self: None), \\\n"
        "     patch.object(DynamicIslandPill, '_start_hotkey_listener', lambda self: None):\n"
        "    try:\n"
        "        pill = DynamicIslandPill('http://127.0.0.1:8000')\n"
        "    except tk.TclError:\n"
        "        raise SystemExit(0)\n"
        "    pill.root.attributes('-alpha', 0.0)\n"
        "    # Dragging the compact capsule moves without triggering click-expand\n"
        "    sx = pill.root.winfo_x()\n"
        "    pill._on_pill_press(type('E', (), {'x': 10, 'y': 10, 'x_root': sx + 40, 'y_root': 20, 'widget': pill.orb_canvas})())\n"
        "    pill._on_pill_drag(type('E', (), {'x': 30, 'y': 10, 'x_root': sx + 60, 'y_root': 20, 'widget': pill.orb_canvas})())\n"
        "    pill._on_pill_release(type('E', (), {'x': 30, 'y': 10, 'x_root': sx + 60, 'y_root': 20, 'widget': pill.orb_canvas})())\n"
        "    assert pill.expanded is False\n"
        "    pill.expand()\n"
        "    pill.root.update()\n"
        "    hdr_right_edge = pill.header.winfo_rootx() + pill.header.winfo_width()\n"
        "    for btn in (pill.hdr_stop_btn, pill.shutter_btn, pill.apps_btn, pill.shutdown_btn):\n"
        "        assert btn.winfo_rootx() + btn.winfo_width() <= hdr_right_edge + 1\n"
        "        assert btn.winfo_width() >= 20\n"
        "    assert pill.nav_pill_bar.winfo_width() >= pill.nav_pill_bar.winfo_reqwidth()\n"
        "    assert pill.jarvis_tab_btn.winfo_width() >= pill.jarvis_tab_btn.winfo_reqwidth()\n"
        "    # Word-wrapping multi-line composer keeps long prior text visible across lines\n"
        "    pill._clear_placeholder()\n"
        "    long_text = 'Please open Chrome and summarize all unread emails about tomorrow morning deployment plan'\n"
        "    pill.entry.insert(0, long_text)\n"
        "    pill._sync_composer_height()\n"
        "    pill.root.update()\n"
        "    assert pill.entry.get().strip() == long_text\n"
        "    # Model switcher in bottom-left opens upward cleanly above model_btn\n"
        "    pill._open_model_menu()\n"
        "    pill.root.update()\n"
        "    assert pill._model_popover_visible is True\n"
        "    assert pill.model_popover.winfo_rooty() < pill.model_btn.winfo_rooty()\n"
        "    pill._close_model_popover()\n"
        "    pill._open_apps_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._apps_popover_visible is True\n"
        "    assert 270 <= pill.apps_popover.winfo_width() <= 350\n"
        "    assert pill.apps_popover.winfo_rootx() > pill.frame.winfo_rootx() + pill.frame.winfo_width() // 2\n"
        "    assert pill._apps_gmail_sw.winfo_rootx() > pill.apps_gmail_state_lbl.winfo_rootx()\n"
        "    assert pill._apps_cal_sw.winfo_rootx() > pill.apps_cal_state_lbl.winfo_rootx()\n"
        "    pill._toggle_apps_cfg('gmail')\n"
        "    pill.root.update()\n"
        "    assert pill._apps_cfg_gmail_open is True\n"
        "    pill._apps_filter_var.set('chrome')\n"
        "    pill._filter_apps_cards()\n"
        "    assert len(pill._apps_cards) == 4\n"
        "    pill._close_apps_popover()\n"
        "    # Upward @agent popover + 3-theme persona switcher\n"
        "    pill._open_agent_menu()\n"
        "    pill.root.update()\n"
        "    assert pill._agent_popover_visible is True\n"
        "    assert pill.agent_popover.winfo_rooty() < pill.agent_btn.winfo_rooty()\n"
        "    pill._pick_agent_from_popover('@coder', 'coder')\n"
        "    assert pill.selected_agent == 'coder'\n"
        "    pill._open_persona_popover()\n"
        "    pill.root.update()\n"
        "    assert pill._persona_popover_visible is True\n"
        "    pill.persona = 'ultron'\n"
        "    pill._apply_persona_theme()\n"
        "    assert 'Ultron' in pill.persona_btn.cget('text')\n"
        "    pill.persona = 'megatron'\n"
        "    pill._apply_persona_theme()\n"
        "    assert 'Megatron' in pill.persona_btn.cget('text')\n"
        "    pill.shutdown_app()\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr or proc.stdout


