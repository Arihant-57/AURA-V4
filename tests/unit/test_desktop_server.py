"""Unit tests for the Windows Desktop MCP server and scope gating."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.aura.core.scope import TaskScope, select_active_tools
from src.aura.mcp import desktop_server as ds


def test_mutating_desktop_tools_blocked_in_read_only(monkeypatch):
    monkeypatch.setenv("AURA_READ_ONLY", "1")
    res_place = ds.desktop_place_window("notepad", "left")
    res_launch = ds.desktop_launch_app("notepad")
    res_clip = ds.desktop_clipboard("set", "hi")
    res_trash = ds.desktop_trash_file("C:\\Users\\demo\\file.txt")
    res_click = ds.desktop_ui_click("notepad", "File")
    assert "READ-ONLY BLOCKED" in res_place
    assert "READ-ONLY BLOCKED" in res_launch
    assert "READ-ONLY BLOCKED" in res_clip
    assert "READ-ONLY BLOCKED" in res_trash
    assert "READ-ONLY BLOCKED" in res_click


def test_trash_file_blocks_system_paths():
    assert "BLOCKED:" in ds.trash_file_impl("C:\\Windows\\System32\\notepad.exe", read_only=False)
    assert "BLOCKED:" in ds.trash_file_impl("C:\\", read_only=False)


@pytest.mark.parametrize(
    "bad_app,bad_args",
    [
        ("cmd.exe", "/c dir"),
        ("powershell", "-Command calc"),
        ("regedit", ""),
        ("shutdown", "/s"),
        ("taskkill", "/F /IM python.exe"),
        ("notepad", "a & calc"),
        ("notepad | calc", ""),
    ],
)
def test_dangerous_apps_and_shell_chars_blocked_even_in_rw(bad_app, bad_args):
    res = ds.launch_app_impl(bad_app, args=bad_args, read_only=False)
    assert res.startswith(("BLOCKED:", "ERROR:"))


def test_find_files_rejects_short_query_and_handles_fallback(tmp_path, monkeypatch):
    assert ds.find_files_impl("!") .startswith("ERROR:")

    docs = tmp_path / "Documents"
    docs.mkdir()
    target = docs / "aura_budget_notes.txt"
    target.write_text("test", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ds, "_run_powershell", lambda *a, **kw: (1, "", "no index"))
    out = ds.find_files_impl("aura_budget")
    assert str(target) in out


def test_read_screen_ocr_handles_imagegrab_oserror(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("screen grab failed")

    monkeypatch.setattr("PIL.ImageGrab.grab", _boom)
    res = ds.read_screen_ocr_impl()
    assert "ERROR: Screen capture unavailable" in res


def test_winrt_ocr_reads_rendered_png(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only WinRT OCR test")
    from PIL import Image, ImageDraw, ImageFont

    img_path = tmp_path / "ocr_sample.png"
    img = Image.new("RGB", (520, 120), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=32)
    draw.text((20, 35), "AURA DESKTOP 42", fill="black", font=font)
    img.save(img_path)

    ok, text = ds._ocr_image_file(str(img_path))
    assert ok is True
    assert "AURA" in text.upper()
    assert "42" in text


def test_desktop_tools_scope_gated():
    fake_tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="git_status"),
        SimpleNamespace(name="desktop_list_windows"),
        SimpleNamespace(name="desktop_read_screen_ocr"),
    ]

    code_scope = TaskScope("inspect server.py and check routes")
    assert code_scope.desktop is False
    code_names = {t.name for t in select_active_tools(fake_tools, code_scope)}
    assert code_names == {"read_file"}

    screen_scope = TaskScope("what is on my screen right now?")
    assert screen_scope.desktop is True
    screen_names = {t.name for t in select_active_tools(fake_tools, screen_scope)}
    assert "desktop_read_screen_ocr" in screen_names
    assert "desktop_list_windows" in screen_names

    chat_scope = TaskScope("hello")
    assert select_active_tools(fake_tools, chat_scope) == []


def test_phase3_os_input_tools_ro_bounds_and_safety(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    monkeypatch.setenv("AURA_READ_ONLY", "1")
    assert "READ-ONLY BLOCKED" in ds.desktop_mouse_click(100, 100)
    assert "READ-ONLY BLOCKED" in ds.desktop_type_text("hello")
    assert "READ-ONLY BLOCKED" in ds.desktop_hotkey("ctrl+c")
    assert "READ-ONLY BLOCKED" in ds.desktop_scroll("down", 3)

    mock_pg = SimpleNamespace(
        FAILSAFE=False,
        size=MagicMock(return_value=(1920, 1080)),
        click=MagicMock(),
        write=MagicMock(),
        press=MagicMock(),
        hotkey=MagicMock(),
        scroll=MagicMock(),
    )
    monkeypatch.setitem(__import__("sys").modules, "pyautogui", mock_pg)
    monkeypatch.setattr(ds, "get_monitors", lambda: [{"bounds": {"left": 0, "top": 0, "width": 1920, "height": 1080}}])

    # Out-of-bounds clicks are rejected even in RW mode
    assert "outside screen bounds" in ds.mouse_click_impl(-10, 50, read_only=False)
    assert "outside screen bounds" in ds.mouse_click_impl(5000, 50, read_only=False)

    click_res = ds.mouse_click_impl(400, 300, button="right", double=True, read_only=False)
    assert "Clicked right mouse button at (400, 300)" in click_res
    mock_pg.click.assert_called_once_with(x=400, y=300, clicks=2, button="right")

    assert "Error: Cannot type empty text" in ds.type_text_impl("", read_only=False)
    type_res = ds.type_text_impl("hello jarvis", press_enter=True, read_only=False)
    assert "Typed 12 characters" in type_res
    mock_pg.write.assert_called_once_with("hello jarvis", interval=0.01)
    mock_pg.press.assert_called_once_with("enter")

    for dangerous in ("alt+f4", "F4 + Alt", "ctrl+alt+del", "Control+Alt+Delete", "win+r", "windows+r", "ctrl+shift+esc"):
        assert "SECURITY BLOCKED" in ds.hotkey_impl(dangerous, read_only=False)

    hk_res = ds.hotkey_impl("Control+c", read_only=False)
    assert "Executed keyboard shortcut: ctrl+c" in hk_res
    mock_pg.hotkey.assert_called_once_with("ctrl", "c")

    assert "Scrolled down by 5 steps" in ds.desktop_scroll_impl("down", 5, read_only=False)
    mock_pg.scroll.assert_called_with(-600)
    assert "Scrolled up by 3 steps" in ds.desktop_scroll_impl("up", 3, read_only=False)
    mock_pg.scroll.assert_called_with(360)
    assert "clicks must be an integer" in ds.desktop_scroll_impl("down", "bad", read_only=False)  # type: ignore[arg-type]

    input_scope = TaskScope("use mouse double click and scroll down")
    assert input_scope.desktop is True


def test_desktop_close_window_and_media_and_ui_set(monkeypatch):
    import sys
    from types import SimpleNamespace
    from src.aura.mcp import desktop_server as ds

    # RO blocks
    assert ds.close_window_impl("chrome", read_only=True).startswith("READ-ONLY BLOCKED")
    assert ds.media_control_impl("playpause", read_only=True).startswith("READ-ONLY BLOCKED")
    assert ds.ui_set_impl("notepad", "Search", "hello", read_only=True).startswith("READ-ONLY BLOCKED")

    if sys.platform != "win32":
        return

    posted = []
    keys = []
    fake_user32 = SimpleNamespace(
        PostMessageW=lambda hwnd, msg, wp, lp: posted.append((int(hwnd.value), msg)),
        keybd_event=lambda vk, scan, flags, extra: keys.append((vk, flags)),
    )
    monkeypatch.setattr(ds, "_get_user32", lambda: fake_user32)
    monkeypatch.setattr(
        ds,
        "_find_windows",
        lambda t: [{"hwnd": 101, "title": f"{t} Window", "process": t}] if t.lower() == "chrome" else [],
    )
    res_close = ds.close_window_impl("chrome, missingapp", read_only=False)
    assert "Closed 1 window(s)" in res_close
    assert posted and posted[0][1] == 0x0010
    assert "missingapp" in res_close

    res_media = ds.media_control_impl("mute", read_only=False)
    assert "Triggered media key: mute" in res_media
    assert len(keys) == 2

    assert ds.media_control_impl("badaction", read_only=False).startswith("ERROR:")

    monkeypatch.setattr(ds, "_run_powershell", lambda script, env=None, timeout=10: (0, "SET: Search Box", ""))
    assert "SET: Search Box" in ds.ui_set_impl("chrome", "Search Box", "hello world", read_only=False)


def test_desktop_todo_note_and_settings_and_watch(monkeypatch, tmp_path):
    import sys
    from src.aura.mcp import desktop_server as ds

    monkeypatch.setenv("AURA_HEADLESS_TEST", "1")
    assert ds.todo_note_impl("read", storage_dir=tmp_path) == "(To-Do list is empty)"
    assert ds.todo_note_impl("add", "Call Alex", storage_dir=tmp_path, read_only=True).startswith("READ-ONLY BLOCKED")

    assert "Added to Jarvis_Notes.md" in ds.todo_note_impl("add", "Call Alex", storage_dir=tmp_path, read_only=False)
    assert "Call Alex" in ds.todo_note_impl("read", storage_dir=tmp_path)
    assert "Opened Jarvis_Notes.md" in ds.todo_note_impl("open", storage_dir=tmp_path, read_only=False)
    assert "Marked completed" in ds.todo_note_impl("done", "Alex", storage_dir=tmp_path, read_only=False)
    assert "- [x] Call Alex" in ds.todo_note_impl("read", storage_dir=tmp_path)
    assert "Cleared completed" in ds.todo_note_impl("clear", storage_dir=tmp_path, read_only=False)
    assert "Call Alex" not in ds.todo_note_impl("read", storage_dir=tmp_path)

    # Watch window helper
    monkeypatch.setattr(
        ds,
        "_find_windows",
        lambda t: [{"title": "Notes - Notepad", "process": "notepad"}] if "notepad" in t.lower() else [],
    )
    assert ds.watch_window_impl("notepad", "appears", timeout_seconds=0.1).startswith("MATCHED:")
    assert ds.watch_window_impl("otherapp", "disappears", timeout_seconds=0.1).startswith("MATCHED:")
    assert ds.watch_window_impl("otherapp", "appears", timeout_seconds=0.05).startswith("TIMEOUT")

    if sys.platform == "win32":
        started = []
        monkeypatch.setattr(ds.os, "startfile", lambda uri: started.append(uri), raising=False)
        out_uri = ds.launch_app_impl("bluetooth settings", read_only=False)
        assert "ms-settings:bluetooth" in out_uri
        assert started == ["ms-settings:bluetooth"]

    # + chaining inside place_window_impl
    calls = []
    monkeypatch.setattr(ds, "place_multiple_windows_impl", lambda placements, read_only=None: calls.append(placements) or "PLACED MULTI")
    assert ds.place_window_impl("chrome left + notepad right", read_only=False) == "PLACED MULTI"
    assert calls == [[("chrome", "left", 1), ("notepad", "right", 1)]]


def test_get_windows_filters_cloaked_and_noisy_apps(monkeypatch):
    import sys
    from types import SimpleNamespace
    from src.aura.mcp import desktop_server as ds

    assert ds._is_window_cloaked(0) is False
    if sys.platform != "win32":
        return

    windows = {
        10: ("Settings", "applicationframehost", True),
        20: ("AURA Dynamic Island", "pythonw", False),
        21: ("NVIDIA GeForce Overlay", "nvcontainer", False),
        22: ("Nahimic", "nahimic", False),
        30: ("Windows Input Experience", "textinputhost", False),
        40: ("Notes - Notepad", "notepad", False),
    }

    def _set_text(_hwnd, buf, _n):
        t = windows[int(_hwnd)][0]
        buf.value = t
        return len(t)

    def _set_rect(_hwnd, rect_ref):
        rect_ref._obj.left = 0
        rect_ref._obj.top = 0
        rect_ref._obj.right = 800
        rect_ref._obj.bottom = 600
        return 1

    def _set_pid(_hwnd, pid_ref):
        pid_ref._obj.value = int(_hwnd)
        return 1

    def _enum(cb, lparam):
        for hwnd in windows:
            cb(hwnd, lparam)
        return 1

    fake_user32 = SimpleNamespace(
        EnumWindows=_enum,
        IsWindowVisible=lambda _h: 1,
        GetWindowTextLengthW=lambda h: len(windows[int(h)][0]),
        GetWindowTextW=_set_text,
        GetWindowRect=_set_rect,
        GetWindowThreadProcessId=_set_pid,
        IsIconic=lambda _h: 0,
        IsZoomed=lambda _h: 0,
    )
    monkeypatch.setattr(ds, "_get_user32", lambda: fake_user32)
    monkeypatch.setattr(ds, "_is_window_cloaked", lambda h: windows[int(h)][2])
    monkeypatch.setattr(ds, "_process_name_for_pid", lambda pid: windows[pid][1])

    visible = ds.get_windows()
    assert [w["title"] for w in visible] == ["Notes - Notepad"]
    assert visible[0]["process"] == "notepad"


def test_desktop_chrome_site_args_and_system_info(monkeypatch):
    monkeypatch.setattr(ds, "_lookup_app_path", lambda exe: f"C:/Apps/{exe}" if "chrome" in exe else None)
    monkeypatch.setattr(ds, "get_monitors", lambda: [{"index": 1, "primary": True}])
    monkeypatch.setattr(ds, "get_windows", lambda max_items=8: [{"process": "code", "title": "VS Code"}])

    launched: list[list[str]] = []
    monkeypatch.setattr(ds.subprocess, "Popen", lambda cmd, **kw: launched.append(list(cmd)))
    monkeypatch.setattr("src.aura.mcp.jarvis_server.check_ssrf", lambda _u: None)

    out = ds.launch_app_impl("chrome", args="youtube github.com", read_only=False)
    assert "Launched chrome.exe" in out
    assert launched == [["C:/Apps/chrome.exe", "https://www.youtube.com", "https://github.com"]]

    info = ds.desktop_system_info()
    assert "System Status:" in info
    assert "CPU Cores:" in info
    assert "Disk (" in info
    assert "VS Code" in info


def test_find_and_open_local_path_handles_folders_hyphens_and_safety(tmp_path: Path, monkeypatch):
    import sys

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    target_dir = desktop / "smart-helmet"
    target_dir.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    doc_file = downloads / "smart_helmet_specs.pdf"
    doc_file.write_text("pdf", encoding="utf-8")
    secret_env = desktop / ".env"
    secret_env.write_text("SECRET=1", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ds, "_run_powershell", lambda *a, **kw: (1, "", "no index"))

    # Space-separated query finds hyphenated Desktop folder
    found = ds.find_files_impl("smart helmet", location="desktop")
    assert str(target_dir) in found
    assert str(doc_file) not in found

    # Read-only blocks direct file/folder open
    assert "READ-ONLY BLOCKED" in ds.open_path_impl("smart helmet", location="desktop", read_only=True)

    # Sensitive files and system dirs remain strictly blocked in RW mode
    assert ds.open_path_impl(str(secret_env), read_only=False).startswith("BLOCKED:")
    assert ds.open_path_impl("C:\\Windows\\System32\\cmd.exe", read_only=False).startswith("BLOCKED:")

    if sys.platform == "win32":
        started: list[str] = []
        popen_cmds: list[list[str]] = []
        monkeypatch.setattr(ds.os, "startfile", lambda p: started.append(str(p)), raising=False)
        monkeypatch.setattr(ds.subprocess, "Popen", lambda cmd, **kw: popen_cmds.append(list(cmd)))

        res_folder = ds.desktop_open_path("smart helmet file", location="desktop")
        assert "Opened folder in File Explorer" in res_folder
        assert Path(started[0]).resolve() == target_dir.resolve()

        res_file = ds.open_path_impl("smart_helmet_specs.pdf", location="downloads", read_only=False)
        assert "Opened file directly" in res_file
        assert Path(started[-1]).resolve() == doc_file.resolve()

        res_sel = ds.open_path_impl("smart_helmet_specs.pdf", in_explorer=True, location="downloads", read_only=False)
        assert "Opened File Explorer highlighting" in res_sel
        assert popen_cmds == [["explorer.exe", f"/select,{doc_file.resolve()}"]]


def test_inspect_cursor_validation_uia_ocr_and_overlay_filtering(monkeypatch, tmp_path: Path):
    import json
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only cursor inspection test")

    # Reject mismatched / boolean / out-of-bounds coordinates
    assert ds.inspect_cursor_impl(x=100, y=None).startswith("ERROR:")
    assert ds.inspect_cursor_impl(x=True, y=50).startswith("ERROR:")  # type: ignore[arg-type]
    monkeypatch.setattr(ds, "_get_screen_bounds", lambda: (0, 0, 1920, 1080))
    assert "outside screen bounds" in ds.inspect_cursor_impl(x=5000, y=50)

    class _FakePoint:
        def __init__(self, x: int = 0, y: int = 0) -> None:
            self.x = x
            self.y = y

    def _fake_get_cursor(pt_ref):
        pt_ref._obj.x = 420
        pt_ref._obj.y = 310
        return 1

    fake_user32 = SimpleNamespace(
        GetCursorPos=_fake_get_cursor,
        GetWindowLongW=lambda _h, _idx: 0,
    )
    monkeypatch.setattr(ds, "_get_user32", lambda: fake_user32)
    monkeypatch.setattr(
        ds,
        "_query_uia_at_point",
        lambda x, y: {
            "control_type": "Button",
            "name": "Deploy Build",
            "automation_id": "btnDeploy",
            "class_name": "Button",
            "help_text": "Triggers release pipeline",
            "value": "",
            "is_enabled": True,
            "rect": {"left": 400, "top": 295, "width": 90, "height": 32},
            "parent_type": "ToolBar",
            "parent_name": "Actions",
            "ancestor_type": "Pane",
            "ancestor_name": "Main",
            "top_title": "VS Code",
            "top_pid": 999,
            "top_hwnd": 555,
        },
    )
    monkeypatch.setattr(
        ds,
        "get_windows",
        lambda max_items=40: [
            {
                "hwnd": 555,
                "title": "phase4-agent - VS Code",
                "process": "code",
                "rect": {"left": 0, "top": 0, "right": 1400, "bottom": 900, "width": 1400, "height": 900},
                "minimized": False,
                "maximized": False,
            }
        ],
    )
    monkeypatch.setattr(ds, "_process_name_for_pid", lambda _pid: "code")
    monkeypatch.setattr(ds, "_is_window_cloaked", lambda _hwnd: False)

    ocr_calls: list[tuple[tuple[int, int, int, int], int]] = []

    def _fake_ocr_region(bbox, upscale=1):
        ocr_calls.append((bbox, upscale))
        if upscale == 2:
            return True, "Deploy Build\nCancel"
        return True, "Explorer Sidebar\nDeploy Build"

    monkeypatch.setattr(ds, "_ocr_region_image", _fake_ocr_region)

    # Works safely even in Read-Only mode
    monkeypatch.setenv("AURA_READ_ONLY", "1")
    summary = ds.desktop_inspect_cursor()
    assert "Cursor Inspection at (420, 310):" in summary
    assert 'Window: "phase4-agent - VS Code" (process: code' in summary
    assert 'Element: [Button] "Deploy Build"' in summary
    assert "HelpText: Triggers release pipeline" in summary
    assert "Deploy Build" in summary
    assert "Window OCR Context:" in summary
    assert ocr_calls[0][1] == 2 and ocr_calls[1][1] == 1

    raw_json = ds.desktop_inspect_cursor(x=420, y=310, include_window_ocr=False, as_json=True)
    parsed = json.loads(raw_json)
    assert parsed["cursor"] == {"x": 420, "y": 310}
    assert parsed["element"]["name"] == "Deploy Build"
    assert parsed["window"]["process"] == "code"
    assert parsed["window_ocr"] == ""

    # Noisy AURA Dynamic Island UIA match falls back to underlying Z-order window
    monkeypatch.setattr(
        ds,
        "get_windows",
        lambda max_items=40: [
            {
                "hwnd": 777,
                "title": "Smart Helmet Docs - Chrome",
                "process": "chrome",
                "rect": {"left": 100, "top": 100, "right": 1200, "bottom": 900, "width": 1100, "height": 800},
                "minimized": False,
            }
        ],
    )
    resolved = ds._resolve_window_at_point(
        300,
        300,
        {"top_hwnd": 111, "top_pid": 222, "top_title": "AURA Dynamic Island"},
    )
    assert resolved["title"] == "Smart Helmet Docs - Chrome"


def test_ocr_region_image_cleans_temp_file_and_clickthrough_filter(monkeypatch):
    from PIL import Image

    created_tmp_paths: list[str] = []
    real_ntf = ds.tempfile.NamedTemporaryFile

    def _tracking_ntf(*args, **kwargs):
        f = real_ntf(*args, **kwargs)
        created_tmp_paths.append(f.name)
        return f

    monkeypatch.setattr(ds.tempfile, "NamedTemporaryFile", _tracking_ntf)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda bbox=None, all_screens=True: Image.new("RGB", (40, 40), "white"))
    monkeypatch.setattr(ds, "_ocr_image_file", lambda _p: (False, "simulated ocr fail"))
    ok, msg = ds._ocr_region_image((0, 0, 40, 40), upscale=2)
    assert ok is False and "simulated" in msg
    assert created_tmp_paths and not Path(created_tmp_paths[-1]).exists()

    # Click-through layered+transparent overlay detection
    fake_u32_overlay = SimpleNamespace(GetWindowLongW=lambda _h, _idx: 0x00080000 | 0x00000020)
    fake_u32_normal = SimpleNamespace(GetWindowLongW=lambda _h, _idx: 0x00080000)
    assert ds._is_clickthrough_overlay(fake_u32_overlay, 123) is True
    assert ds._is_clickthrough_overlay(fake_u32_normal, 123) is False


def test_desktop_inspect_path_reads_folder_files_and_blocks_sensitive_paths(tmp_path: Path, monkeypatch):
    import sys

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ds, "_run_powershell", lambda *a, **kw: (1, "", "no index"))
    monkeypatch.setenv("AURA_READ_ONLY", "1")  # Must work in RO mode

    desktop = tmp_path / "Desktop"
    helmet = desktop / "smart-helmet"
    (helmet / "esp32").mkdir(parents=True)
    (helmet / "node_modules" / "express").mkdir(parents=True)
    (helmet / ".git").mkdir(parents=True)
    (helmet / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (helmet / "node_modules" / "express" / "index.js").write_text("module.exports = {};", encoding="utf-8")
    (helmet / "README.md").write_text("# Smart Helmet Guardian\nCrash detection with ESP32 and MPU6050.\n", encoding="utf-8")
    (helmet / "server.js").write_text("const app = require('express')();\nconsole.log('ready');\n", encoding="utf-8")
    (helmet / "firmware.bin").write_bytes(b"\x00\x01\x02\x03")
    (helmet / ".env").write_text("API_KEY=secret123\n", encoding="utf-8")
    (helmet / "secrets.json").write_text('{"token":"abc"}', encoding="utf-8")

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

    # Folder inspection lists non-hidden/non-dependency items & auto-previews README.md
    folder_out = ds.desktop_inspect_path("smart helmet", location="desktop")
    assert "Folder:" in folder_out
    assert "[DIR] esp32/" in folder_out
    assert "[FILE] server.js" in folder_out
    assert "node_modules" not in folder_out
    assert ".git" not in folder_out
    assert ".env" not in folder_out
    assert "Preview: README.md" in folder_out
    assert "Smart Helmet Guardian" in folder_out

    # Subpath file inspection with line slicing
    file_out = ds.desktop_inspect_path("smart-helmet/server.js", location="desktop", start_line=2)
    assert "(from line 2)" in file_out
    assert "console.log('ready')" in file_out

    # Binary rejection
    bin_out = ds.desktop_inspect_path("smart-helmet/firmware.bin", location="desktop")
    assert "BINARY FILE:" in bin_out

    # Strict security blocking: UNC, traversal, sensitive files, .git, outside roots, symlinks
    assert ds.desktop_inspect_path(r"\\fileserver\share\doc.txt").startswith("BLOCKED:")
    assert ds.desktop_inspect_path("smart-helmet/../../.env", location="desktop").startswith("BLOCKED:")
    assert ds.desktop_inspect_path(str(helmet / ".env")).startswith("BLOCKED:")
    assert ds.desktop_inspect_path(str(helmet / "secrets.json")).startswith("BLOCKED:")
    assert ds.desktop_inspect_path(str(helmet / ".git" / "config")).startswith("BLOCKED:")
    assert ds.desktop_inspect_path(str(ssh_dir / "id_rsa")).startswith("BLOCKED:")
    assert ds.open_path_impl(r"\\fileserver\share\doc.txt", read_only=False).startswith("BLOCKED:")

    # Symlink pointing to sensitive file must be blocked
    link_path = helmet / "innocent_readme.txt"
    try:
        link_path.symlink_to(helmet / ".env")
    except (OSError, NotImplementedError):
        pass
    else:
        assert ds.desktop_inspect_path(str(link_path)).startswith("BLOCKED:")
        assert ds._is_sensitive_or_symlink_target(link_path) is True

    # Non-minimized window tie-breaker and minimized OCR guard
    monkeypatch.setattr(
        ds,
        "get_windows",
        lambda max_items=80: [
            {"hwnd": 1, "title": "Notepad", "process": "notepad", "minimized": True, "rect": {"left": 0, "top": 0, "right": 500, "bottom": 400}},
            {"hwnd": 2, "title": "Notepad", "process": "notepad", "minimized": False, "rect": {"left": 10, "top": 10, "right": 500, "bottom": 400}},
        ],
    )
    assert ds._find_window("notepad")["hwnd"] == 2
    if sys.platform == "win32":
        monkeypatch.setattr(
            ds,
            "get_windows",
            lambda max_items=80: [
                {"hwnd": 1, "title": "Notepad", "process": "notepad", "minimized": True, "rect": {"left": 0, "top": 0, "right": 500, "bottom": 400}},
            ],
        )
        assert ds.read_screen_ocr_impl("notepad").startswith("WINDOW MINIMIZED:")


def test_bfs_shallow_local_search_stops_before_deep_subtree(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    desktop = tmp_path / "Desktop"
    target = desktop / "smart-helmet"
    target.mkdir(parents=True, exist_ok=True)
    # Deep sibling tree in Documents that would slow a naive full walk
    deep_dir = tmp_path / "Documents" / "giant" / "lvl1" / "lvl2" / "lvl3"
    deep_dir.mkdir(parents=True, exist_ok=True)
    (deep_dir / "noise.txt").write_text("x", encoding="utf-8")

    hits = ds._search_local_paths("smart helmet", max_results=5)
    assert hits and hits[0] == target


