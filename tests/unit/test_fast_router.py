"""Unit tests for the Tier-0 Zero-AI FastRouter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.aura.core.fast_router import FastRouter


def test_fast_router_blocks_mutating_commands_in_ro(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    res_open = router.try_handle("open notepad", read_only=True)
    assert res_open.handled is True
    assert "READ-ONLY BLOCKED" in res_open.answer

    res_snap = router.try_handle("snap chrome left", read_only=True)
    assert res_snap.handled is True
    assert "READ-ONLY BLOCKED" in res_snap.answer


def test_fast_router_does_not_hijack_code_prompts(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    for prompt in (
        "open settings.py",
        "open package.json",
        "open server.py and inspect FastRouter",
        "open the workspace readme",
        "fix the bug in src/aura/server.py",
        "what is on my screen?",
    ):
        res = router.try_handle(prompt, read_only=False)
        assert res.handled is False, f"Prompt should go to LLM, got handled: {prompt}"


def test_fast_router_routes_snap_monitors_and_apps(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    with patch("src.aura.core.fast_router.place_window_impl", return_value="Placed 'Chrome'") as mock_place:
        res = router.try_handle("snap chrome to right on monitor 2", read_only=False)
        assert res.handled is True
        assert res.action == "place_window"
        assert "Chrome" in res.answer
        mock_place.assert_called_once_with("chrome", position="right", monitor=2, read_only=False)

    with patch("src.aura.core.fast_router.get_monitors", return_value=[{"index": 1, "primary": True}]):
        res_mon = router.try_handle("list monitors", read_only=True)
        assert res_mon.handled is True
        assert res_mon.action == "list_monitors"
        assert '"index": 1' in res_mon.answer

    with patch("src.aura.core.fast_router.launch_app_impl", return_value="Launched calc.exe.") as mock_launch:
        res_app = router.try_handle("open calc", read_only=False)
        assert res_app.handled is True
        assert res_app.action == "launch_app"
        mock_launch.assert_called_once_with("calc", read_only=False)


def test_fast_router_custom_shortcuts_round_trip(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    ro_bind = router.try_handle("shortcut add notes -> open notepad", read_only=True)
    assert ro_bind.handled is True
    assert "READ-ONLY BLOCKED" in ro_bind.answer

    rw_bind = router.try_handle("shortcut add notes -> open notepad", read_only=False)
    assert rw_bind.handled is True
    assert "Saved shortcut" in rw_bind.answer

    with patch("src.aura.core.fast_router.launch_app_impl", return_value="Launched notepad.exe.") as mock_launch:
        triggered = router.try_handle("notes", read_only=False)
        assert triggered.handled is True
        assert triggered.action == "launch_app"
        mock_launch.assert_called_once_with("notepad", read_only=False)


def test_fast_router_websites_time_and_compound_passthrough():
    router = FastRouter()

    t_res = router.try_handle("what time is it", read_only=True)
    assert t_res.handled is True
    assert t_res.action == "local_time"

    ro_gmail = router.try_handle("open my gmail", read_only=True)
    assert ro_gmail.handled is True
    assert "READ-ONLY BLOCKED" in ro_gmail.answer

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened https://mail.google.com") as mock_open:
        rw_gmail = router.try_handle("open gmail", read_only=False)
        assert rw_gmail.handled is True
        assert rw_gmail.action == "open_website"
        mock_open.assert_called_once_with("gmail", read_only=False)

    # Compound prompt must pass through to full LLM+tools so both open + summary run
    compound = router.try_handle("open my gmail and tell me the inbox summary", read_only=False)
    assert compound.handled is False


def test_fast_router_routes_calendar_check(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)
    res = router.try_handle("check my calendar", read_only=True)
    assert res.handled is True
    assert res.action == "jarvis_calendar"
    assert "Calendar:" in res.answer


def test_fast_router_handles_briefing_and_wake_word_prefixes(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    res_brief = router.try_handle("morning briefing", read_only=True)
    assert res_brief.handled is True
    assert res_brief.action == "jarvis_briefing"
    assert "Jarvis Daily Briefing" in res_brief.answer

    res_wake_cal = router.try_handle("Hey Jarvis, check my calendar", read_only=True)
    assert res_wake_cal.handled is True
    assert res_wake_cal.action == "jarvis_calendar"

    res_wake_comma = router.try_handle("Hey, Jarvis, check my calendar", read_only=True)
    assert res_wake_comma.handled is True
    assert res_wake_comma.action == "jarvis_calendar"

    res_wake_time = router.try_handle("Jarvis, what time is it?", read_only=True)
    assert res_wake_time.handled is True
    assert res_wake_time.action == "local_time"


def test_fast_router_handles_indian_english_browser_and_youtube_phrases(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened https://www.youtube.com") as mock_open:
        r1 = router.try_handle("open the youtube", read_only=False)
        assert r1.handled is True
        assert r1.action == "open_website"
        mock_open.assert_called_once_with("youtube", read_only=False)

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened https://www.youtube.com") as mock_open2:
        r2 = router.try_handle("open browser and open like YouTube", read_only=False)
        assert r2.handled is True
        assert r2.action == "open_website"
        mock_open2.assert_called_once_with("YouTube", read_only=False)

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened https://www.google.com") as mock_open3:
        r3 = router.try_handle("open browser", read_only=False)
        assert r3.handled is True
        assert r3.action == "open_website"
        mock_open3.assert_called_once_with("browser", read_only=False)

    # Generic "open the workspace readme" must still fall through to LLM/tools
    assert router.try_handle("open the workspace readme", read_only=False).handled is False


def test_fast_router_learned_shortcuts_gist_chains_and_forget(tmp_path, monkeypatch):
    from src.aura.core import fast_router as fr

    router = fr.FastRouter(storage_dir=tmp_path)
    opened_urls = []
    launched = []
    monkeypatch.setattr(fr, "open_website_impl", lambda site, read_only=False: opened_urls.append(site) or f"OPENED {site}")
    monkeypatch.setattr(fr, "launch_app_impl", lambda app, args="", read_only=False: launched.append(app) or f"LAUNCHED {app}")
    monkeypatch.setattr(fr.webbrowser, "open", lambda url: opened_urls.append(url) or True)

    # RO blocks teaching
    assert router.try_handle("when I say study, open https://github.com", read_only=True).action == "shortcut_blocked_ro"

    # Teach multi-step shortcut with URL (must not be rejected by '/' guard)
    res_save = router.try_handle("when I say study mode, open https://github.com ; open notepad", read_only=False)
    assert res_save.handled is True
    assert res_save.action == "shortcut_save"

    # Match with spoken filler words via Atticus gist()
    res_run = router.try_handle("can you please study mode", read_only=False)
    assert res_run.handled is True
    assert res_run.action == "shortcut_chain"
    assert "https://github.com" in opened_urls
    assert "notepad" in launched

    res_list = router.try_handle("list shortcuts", read_only=True)
    assert "study mode" in res_list.answer

    res_del = router.try_handle("forget shortcut study mode", read_only=False)
    assert res_del.action == "shortcut_delete"
    assert router.try_handle("study mode", read_only=False).handled is False


def test_fast_router_focus_mode_media_close_todo_search_and_multi_snap(tmp_path, monkeypatch):
    from src.aura.core import fast_router as fr
    from src.aura.core.scope import TaskScope

    monkeypatch.setenv("AURA_HEADLESS_TEST", "1")
    router = fr.FastRouter(storage_dir=tmp_path)

    # Focus mode RO blocked then RW active
    assert router.try_handle("focus mode", read_only=True).action == "focus_mode_blocked_ro"
    monkeypatch.setattr(
        fr,
        "get_windows",
        lambda max_items=60: [
            {"title": "Spotify", "process": "spotify"},
            {"title": "Visual Studio Code", "process": "code"},
        ],
    )
    closed = []
    monkeypatch.setattr(fr, "close_window_impl", lambda target, read_only=False: closed.append(target) or f"Closed {target}")
    res_focus = router.try_handle("start focus mode", read_only=False)
    assert res_focus.handled is True
    assert res_focus.action == "focus_mode"
    assert closed == ["spotify"]
    assert "Jarvis_Notes.md" in res_focus.answer

    # Media controls
    media_calls = []
    monkeypatch.setattr(fr, "media_control_impl", lambda action, read_only=False: media_calls.append((action, read_only)) or f"media:{action}")
    res_med = router.try_handle("mute volume", read_only=False)
    assert res_med.action == "media_control"
    assert media_calls == [("mute", False)]

    # Close app (but never hijack code/issue/pr)
    res_close = router.try_handle("close chrome", read_only=False)
    assert res_close.action == "close_window"
    assert router.try_handle("close server.py", read_only=False).handled is False
    assert router.try_handle("close the database connection", read_only=False).handled is False

    # Multi-snap chaining
    snapped = []
    monkeypatch.setattr(fr, "place_multiple_windows_impl", lambda specs, read_only=False: snapped.append(specs) or "SNAPPED")
    res_msnap = router.try_handle("put chrome left and notepad right", read_only=False)
    assert res_msnap.action == "place_window"
    assert snapped == [[("chrome", "left", 1), ("notepad", "right", 1)]]

    # Quick YouTube / Google search (but not code/deictic queries)
    searched = []
    monkeypatch.setattr(fr.webbrowser, "open", lambda url: searched.append(url) or True)
    res_yt = router.try_handle("search youtube for lo-fi coding beats", read_only=False)
    assert res_yt.action == "quick_search"
    assert any("youtube.com/results?search_query=" in u for u in searched)
    assert router.try_handle("search the workspace for fast_router.py", read_only=False).handled is False

    # To-Do CRUD via FastRouter
    assert "Added" in router.try_handle("add buy coffee to my todo list", read_only=False).answer
    assert "buy coffee" in router.try_handle("show my todo list", read_only=True).answer
    assert "Marked completed" in router.try_handle("mark buy coffee done", read_only=False).answer

    # Scope classification sanity
    assert TaskScope("close chrome").desktop is True
    assert TaskScope("pause music").desktop is True
    assert TaskScope("inspect server.py and check routes").desktop is False


def test_fast_router_chrome_multisite_urls_and_system_info(tmp_path: Path, monkeypatch):
    from src.aura.core import fast_router as fr

    router = fr.FastRouter(storage_dir=tmp_path)

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened in Chrome") as mock_open:
        r1 = router.try_handle("open my chrome and open youtube", read_only=False)
        assert r1.handled is True
        assert r1.action == "open_website"
        mock_open.assert_called_once_with("youtube", read_only=False, browser="chrome")

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened in Chrome") as mock_open2:
        r2 = router.try_handle("open youtube and github in chrome", read_only=False)
        assert r2.handled is True
        assert r2.action == "open_website"
        mock_open2.assert_called_once_with("youtube and github", read_only=False, browser="chrome")

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened https://github.com") as mock_url:
        r3 = router.try_handle("open https://github.com", read_only=False)
        assert r3.handled is True
        assert r3.action == "open_website"
        mock_url.assert_called_once_with("https://github.com", read_only=False)

    monkeypatch.setattr(fr, "system_info_impl", lambda: "System Status:\n- CPU Cores: 8")
    r_sys = router.try_handle("system info", read_only=True)
    assert r_sys.handled is True
    assert r_sys.action == "system_info"
    assert "CPU Cores: 8" in r_sys.answer

    # Hybrid chrome + repo scanning prompt must fall through to the full agent loop
    assert router.try_handle("open my chrome and open youtube and scan the repo and implement it", read_only=False).handled is False


def test_fast_router_handles_calendar_today_variants(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)
    for phrase in (
        "what is on my calendar today?",
        "what's on my calendar today",
        "check calendar today",
        "agenda today",
    ):
        res = router.try_handle(phrase, read_only=True)
        assert res.handled is True, phrase
        assert res.action == "jarvis_calendar"


def test_fast_router_youtube_play_compound_chrome_and_no_false_app_launch(tmp_path: Path):
    router = FastRouter(storage_dir=tmp_path)

    # Natural YouTube element open requests must NOT fail with NOT FOUND app launch
    with patch("src.aura.core.fast_router.launch_app_impl") as mock_launch:
        r_elem = router.try_handle("Can you open one shots on the YouTube page?", read_only=False)
        assert r_elem.handled is False
        mock_launch.assert_not_called()

    # Compound Chrome + YouTube search
    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened in Google Chrome") as mock_open:
        r_s1 = router.try_handle("open Chrome and open YouTube and search for Avengers Endgame", read_only=False)
        assert r_s1.handled is True
        assert r_s1.action == "quick_search"
        assert "results?search_query=Avengers%20Endgame" in mock_open.call_args.args[0]
        assert mock_open.call_args.kwargs["browser"] == "chrome"

    with patch("src.aura.core.fast_router.open_website_impl", return_value="Opened in Google Chrome") as mock_open2:
        r_s2 = router.try_handle(
            "Open the Chrome which is running on my tab and search YouTube in YouTube, search Avenger Endgame",
            read_only=False,
        )
        assert r_s2.handled is True
        assert r_s2.action == "quick_search"
        assert "results?search_query=Avenger%20Endgame" in mock_open2.call_args.args[0]
        assert mock_open2.call_args.kwargs["browser"] == "chrome"

    # Direct play on YouTube routes cleanly to play_youtube_impl
    with patch("src.aura.core.fast_router.play_youtube_impl", return_value="Playing trailer") as mock_play:
        r_p1 = router.try_handle("play Avengers Endgame trailer on YouTube", read_only=False)
        assert r_p1.handled is True
        assert r_p1.action == "play_youtube"
        mock_play.assert_called_once_with("Avengers Endgame trailer", read_only=False, browser="chrome")

    with patch("src.aura.core.fast_router.play_youtube_impl", return_value="Playing trailer") as mock_play2:
        r_p2 = router.try_handle(
            "On the YouTube page, you can see an endgame and core official trailer, can you play it?",
            read_only=False,
        )
        assert r_p2.handled is True
        assert r_p2.action == "play_youtube"
        mock_play2.assert_called_once_with("endgame and core official trailer", read_only=False, browser="chrome")


def test_fast_router_opens_local_files_and_folders_directly(tmp_path: Path):
    from src.aura.core.scope import TaskScope

    router = FastRouter(storage_dir=tmp_path)

    with patch("src.aura.core.fast_router.open_path_impl", return_value="Opened folder in File Explorer: C:\\Users\\demo\\Desktop\\smart-helmet") as mock_open:
        r1 = router.try_handle("Can you open the smart helmet file on my desktop?", read_only=False)
        assert r1.handled is True
        assert r1.action == "open_path"
        mock_open.assert_called_once_with("smart helmet", in_explorer=False, location="desktop", read_only=False)

    with patch("src.aura.core.fast_router.open_path_impl", return_value="Opened File Explorer highlighting: smart-helmet") as mock_exp:
        r2 = router.try_handle("Open the file explorer and open smart-helmet file", read_only=False)
        assert r2.handled is True
        assert r2.action == "open_path"
        mock_exp.assert_called_once_with("smart-helmet", in_explorer=True, location="", read_only=False)

    with patch("src.aura.core.fast_router.open_path_impl", return_value="Opened C:\\Users\\demo\\Desktop\\smart-helmet") as mock_drive:
        r3 = router.try_handle(r"open C:\Users\demo\Desktop\smart-helmet", read_only=False)
        assert r3.handled is True
        assert r3.action == "open_path"
        mock_drive.assert_called_once_with(r"C:\Users\demo\Desktop\smart-helmet", in_explorer=False, location="", read_only=False)

    # Repo paths & code prompts must NEVER be hijacked by local file opener
    for code_prompt in (
        "open settings.py",
        "open the workspace readme",
        "fix the bug in src/aura/server.py",
        "open C:\\repo\\app.py and inspect bug",
    ):
        assert router.try_handle(code_prompt, read_only=False).handled is False

    assert TaskScope("open smart helmet on my desktop").desktop is True
    assert TaskScope("open resume in downloads").desktop is True
    assert TaskScope("inspect server.py and check routes").desktop is False


def test_fast_router_bypasses_compound_desktop_read_and_research_prompts(tmp_path: Path):
    from src.aura.core.scope import TaskScope

    router = FastRouter(storage_dir=tmp_path)
    compound_prompts = (
        "open smart-helmet on desktop, read its files, and research how it works",
        "open smart-helmet on desktop and explain what is inside it",
        "find smart-helmet on desktop and summarize its readme",
        "open smart helmet on desktop, read package.json and explain it",
    )
    for prompt in compound_prompts:
        res = router.try_handle(prompt, read_only=False)
        assert res.handled is False, f"Compound prompt unexpectedly fast-routed: {prompt}"

    compound_scope = TaskScope("open smart-helmet on desktop, read its files, and research similar ESP32 crash detection")
    assert compound_scope.desktop is True
    assert compound_scope.jarvis is True
