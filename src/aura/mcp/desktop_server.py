"""
AURA Desktop MCP Server (Windows-native)

Provides monitor/window inspection, window snapping, safe app launching,
Windows Search file lookup, safe local file/folder inspection, Point-and-Tell
cursor UI Automation + zoomed WinRT OCR inspection, and screen reading.

Security & safety:
- Mutating tools (`desktop_place_window`, `desktop_launch_app`, `desktop_open_path`,
  `desktop_trash_file`, `desktop_ui_click`, `desktop_ui_set`, `desktop_mouse_click`,
  `desktop_type_text`, `desktop_hotkey`, `desktop_scroll`, `desktop_close_window`,
  `desktop_media_control`, and mutating actions of `desktop_clipboard`/`desktop_todo_note`)
  strictly refuse when `AURA_READ_ONLY=1`.
- Never spawns shell=True; blocks dangerous system/destructive binaries
  (cmd, powershell, regedit, del, rm, format, shutdown, taskkill, etc.) and UNC paths.
- Local file inspection/opening strictly honors `_is_sensitive_path` (.env, .ssh, .aura,
  keys, system directories).
- OCR captures are written to a temporary PNG and removed in a finally block.
"""

from __future__ import annotations

import base64
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from mcp.server import MCPServer


mcp = MCPServer("AURA Desktop Server")

_BLOCKED_APPS = {
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "regedit", "regedit.exe", "reg", "reg.exe", "format", "format.com",
    "del", "erase", "rmdir", "rd", "rm", "shutdown", "shutdown.exe",
    "restart", "taskkill", "taskkill.exe", "diskpart", "diskpart.exe",
    "bcdedit", "bcdedit.exe", "wmic", "wmic.exe", "sc", "sc.exe",
    "net", "net.exe", "runas", "runas.exe",
}

_APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "edge": "msedge.exe",
    "msedge": "msedge.exe",
    "my edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "chrome": "chrome.exe",
    "my chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "code": "code.cmd",
    "vscode": "code.cmd",
    "vs code": "code.cmd",
    "visual studio code": "code.cmd",
    "terminal": "wt.exe",
    "wt": "wt.exe",
    "snip": "SnippingTool.exe",
    "snipping tool": "SnippingTool.exe",
    "task manager": "taskmgr.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "olk.exe",
    "discord": "Discord.exe",
    "spotify": "Spotify.exe",
    "slack": "slack.exe",
}

_WINDOWS_URIS: dict[str, str] = {
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "bluetooth": "ms-settings:bluetooth",
    "bluetooth settings": "ms-settings:bluetooth",
    "sound": "ms-settings:sound",
    "sound settings": "ms-settings:sound",
    "audio settings": "ms-settings:sound",
    "display": "ms-settings:display",
    "display settings": "ms-settings:display",
    "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi",
    "wifi settings": "ms-settings:network-wifi",
    "network settings": "ms-settings:network-wifi",
    "night light": "ms-settings:nightlight",
    "nightlight": "ms-settings:nightlight",
    "windows update": "ms-settings:windowsupdate",
    "update settings": "ms-settings:windowsupdate",
    "calendar": "outlookcal:",
    "mail": "outlookmail:",
    "store": "ms-windows-store:",
    "camera": "microsoft.windows.camera:",
    "clock": "ms-clock:",
    "alarms": "ms-clock:",
}

_MEDIA_KEYS: dict[str, int] = {
    "playpause": 0xB3,
    "play": 0xB3,
    "pause": 0xB3,
    "resume": 0xB3,
    "next": 0xB0,
    "nexttrack": 0xB0,
    "nextsong": 0xB0,
    "skip": 0xB0,
    "previous": 0xB1,
    "prev": 0xB1,
    "previoustrack": 0xB1,
    "previoussong": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "unmute": 0xAD,
    "volumeup": 0xAF,
    "volup": 0xAF,
    "volumedown": 0xAE,
    "voldown": 0xAE,
}

_SETTINGS_URI_RE = re.compile(r"^ms-settings:[a-z0-9-]*$", re.IGNORECASE)

_NOISY_WINDOW_TITLES = {
    "program manager",
    "msctfime ui",
    "default ime",
    "windows input experience",
    "microsoft text input application",
    "aura dynamic island",
    "nvidia geforce overlay",
    "nahimic",
    "xbox game bar",
}
_NOISY_WINDOW_PROCESSES = {
    "textinputhost",
    "nvidia share",
    "nvcontainer",
    "gamebar",
    "gamebarftserver",
}
_SENSITIVE_DIR_PARTS = {".ssh", ".gnupg", ".aura", ".aws", ".kube", ".docker", "appdata"}
_SENSITIVE_FILE_NAMES = {
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".env", "credentials.json",
    "secrets.json", "service_account.json", ".npmrc", ".pypirc", ".netrc",
    ".git-credentials", ".htpasswd", "token.json", "tokens.json",
}
_SENSITIVE_FILE_EXTS = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
_SENSITIVE_SYSTEM_DIRS = {"windows", "system32", "syswow64", "programdata"}


def _is_unc_path(raw: str | Path) -> bool:
    s = str(raw or "").strip()
    return s.startswith(("\\\\", "//"))


def _is_sensitive_path(path: Path) -> bool:
    """Return True if path points inside credentials, sensitive appdata, UNC shares, or Windows system internals."""
    try:
        if _is_unc_path(path):
            return True
        parts_low = [p.lower() for p in path.parts]
    except Exception:
        return True
    if not parts_low:
        return True
    name_low = path.name.lower()
    if name_low in _SENSITIVE_FILE_NAMES or name_low.startswith(".env."):
        return True
    if path.suffix.lower() in _SENSITIVE_FILE_EXTS:
        return True
    if len(parts_low) >= 2 and any(p in _SENSITIVE_SYSTEM_DIRS for p in parts_low[1:3]):
        return True

    # Check relative to Path.home() first so pytest tmp_path under AppData/Local/Temp isn't misflagged
    check_parts = parts_low
    try:
        home = Path.home()
        if path.is_relative_to(home):
            check_parts = [p.lower() for p in path.relative_to(home).parts]
    except Exception:
        pass

    for idx, part in enumerate(check_parts):
        if part not in _SENSITIVE_DIR_PARTS:
            continue
        if part == "appdata" and check_parts[idx:idx + 3] == ["appdata", "local", "temp"]:
            continue
        return True
    return False
_VALID_POSITIONS = {
    "left", "right", "top", "bottom",
    "top-left", "top-right", "bottom-left", "bottom-right",
    "center", "max", "maximize", "min", "minimize", "restore", "focus", "front",
}


def _is_read_only(read_only: bool | None = None) -> bool:
    if read_only is not None:
        return read_only
    return os.environ.get("AURA_READ_ONLY", "") == "1"


def _ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _run_powershell(script: str, env: dict[str, str] | None = None, timeout: int = 12) -> tuple[int, str, str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    kwargs: dict[str, Any] = {"shell": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        if hasattr(subprocess, "STARTUPINFO"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = si
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", _ps_encoded(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=merged_env,
        check=False,
        **kwargs,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ============================================================
# WIN32 MONITORS & WINDOWS
# ============================================================

def _get_user32():
    if sys.platform != "win32":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.keybd_event.restype = None
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    try:
        # Per-Monitor V2 DPI awareness so rects match real pixels.
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    return user32


def get_monitors() -> list[dict[str, Any]]:
    """Return list of connected display monitors and work areas."""
    user32 = _get_user32()
    if user32 is None:
        return []

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[dict[str, Any]] = []
    cb_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def _callback(hmon, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            m = info.rcMonitor
            w = info.rcWork
            monitors.append({
                "index": len(monitors) + 1,
                "name": info.szDevice,
                "primary": bool(info.dwFlags & 1),
                "bounds": {"left": m.left, "top": m.top, "width": m.right - m.left, "height": m.bottom - m.top},
                "work": {"left": w.left, "top": w.top, "width": w.right - w.left, "height": w.bottom - w.top},
            })
        return True

    user32.EnumDisplayMonitors(None, None, cb_type(_callback), 0)
    monitors.sort(key=lambda x: (not x["primary"], x["bounds"]["left"], x["bounds"]["top"]))
    for i, mon in enumerate(monitors, 1):
        mon["index"] = i
    return monitors


def _process_name_for_pid(pid: int) -> str:
    if sys.platform != "win32" or pid <= 0:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).stem.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_window_cloaked(hwnd: int) -> bool:
    """Return True if DWM cloaks the window (suspended UWP apps like hidden Settings)."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        cloaked = ctypes.c_int(0)
        hr = dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


def _is_clickthrough_overlay(user32: Any, hwnd: Any) -> bool:
    """Return True when a window is a click-through layered overlay (WS_EX_LAYERED | WS_EX_TRANSPARENT)."""
    get_long = getattr(user32, "GetWindowLongW", None)
    if get_long is None:
        return False
    try:
        ex_style = int(get_long(hwnd, -20))  # GWL_EXSTYLE
    except Exception:
        return False
    # WS_EX_TRANSPARENT = 0x20, WS_EX_LAYERED = 0x80000
    return bool((ex_style & 0x00080000) and (ex_style & 0x00000020))


def get_windows(max_items: int = 40) -> list[dict[str, Any]]:
    """Enumerate visible top-level windows with titles, process names, and bounds."""
    user32 = _get_user32()
    if user32 is None:
        return []

    rows: list[dict[str, Any]] = []
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if len(rows) >= max_items:
            return False
        if not user32.IsWindowVisible(hwnd) or _is_window_cloaked(hwnd) or _is_clickthrough_overlay(user32, hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title.lower() in _NOISY_WINDOW_TITLES:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 60 or height < 40:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = _process_name_for_pid(int(pid.value))
        if proc_name in _NOISY_WINDOW_PROCESSES:
            return True
        rows.append({
            "hwnd": int(hwnd),
            "title": title,
            "process": proc_name,
            "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom, "width": width, "height": height},
            "minimized": bool(user32.IsIconic(hwnd)),
            "maximized": bool(user32.IsZoomed(hwnd)),
        })
        return True

    user32.EnumWindows(cb_type(_callback), 0)
    return rows


def _find_windows(target: str) -> list[dict[str, Any]]:
    """Return matching top-level windows sorted by best match score (preferring non-minimized on ties)."""
    needle = target.strip().lower()
    if not needle:
        return []
    windows = get_windows(max_items=80)
    scored: list[tuple[tuple[int, bool], dict[str, Any]]] = []
    for win in windows:
        title = win["title"].lower()
        proc = win["process"].lower()
        score = 0
        if proc == needle or title == needle:
            score = 100
        elif proc.startswith(needle) or title.startswith(needle):
            score = 80
        elif needle in proc or needle in title:
            score = 60
        if score > 0:
            scored.append(((score, not bool(win.get("minimized"))), win))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [win for _, win in scored]


def _find_window(target: str) -> dict[str, Any] | None:
    hits = _find_windows(target)
    return hits[0] if hits else None


def _bring_window_to_front(hwnd: int, minimized: bool = False) -> bool:
    """Bring a window to foreground reliably across Windows thread focus restrictions."""
    user32 = _get_user32()
    if user32 is None:
        return False
    h = wintypes.HWND(int(hwnd))
    user32.ShowWindow(h, 9 if minimized else 5)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fg = user32.GetForegroundWindow()
    me = kernel32.GetCurrentThreadId()
    them = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(them and them != me and user32.AttachThreadInput(me, them, True))
    try:
        user32.keybd_event(0xE8, 0, 0, 0)
        user32.keybd_event(0xE8, 0, 2, 0)
        user32.BringWindowToTop(h)
        return bool(user32.SetForegroundWindow(h))
    finally:
        if attached:
            user32.AttachThreadInput(me, them, False)


_CHAIN_CLAUSE_RE = re.compile(
    r"^(?P<target>.+?)\s+(?:(?:to|on)\s+)?(?P<pos>"
    r"top-left|top-right|bottom-left|bottom-right|top left|top right|bottom left|bottom right"
    r"|left|right|top|bottom|center|max|maximize|min|minimize|restore|focus|front)"
    r"(?:\s+(?:on\s+)?monitor\s+(?P<mon>\d+))?$",
    re.IGNORECASE,
)


def _place_single_window(target: str, position: str = "left", monitor: int = 1) -> str:
    pos = position.strip().lower().replace("_", "-").replace(" ", "-")
    if pos not in _VALID_POSITIONS:
        return f"ERROR: Unsupported position '{position}'. Valid: {', '.join(sorted(_VALID_POSITIONS))}."

    win = _find_window(target)
    if win is None:
        return f"NOT FOUND: No open window matching '{target}'."

    user32 = _get_user32()
    hwnd = wintypes.HWND(win["hwnd"])

    if pos in {"min", "minimize"}:
        user32.ShowWindow(hwnd, 6)
        return f"Minimized '{win['title']}'."
    if pos in {"max", "maximize"}:
        user32.ShowWindow(hwnd, 3)
        _bring_window_to_front(win["hwnd"], minimized=False)
        return f"Maximized '{win['title']}'."
    if pos in {"restore", "focus", "front"}:
        _bring_window_to_front(win["hwnd"], minimized=win["minimized"])
        return f"Focused '{win['title']}'."

    mons = get_monitors()
    if not mons:
        return "ERROR: Could not enumerate monitors."
    mon = mons[monitor - 1] if 1 <= monitor <= len(mons) else mons[0]
    w = mon["work"]
    x, y, width, height = w["left"], w["top"], w["width"], w["height"]
    hw, hh = width // 2, height // 2

    box_map = {
        "left": (x, y, hw, height),
        "right": (x + hw, y, width - hw, height),
        "top": (x, y, width, hh),
        "bottom": (x, y + hh, width, height - hh),
        "top-left": (x, y, hw, hh),
        "top-right": (x + hw, y, width - hw, hh),
        "bottom-left": (x, y + hh, hw, height - hh),
        "bottom-right": (x + hw, y + hh, width - hw, height - hh),
        "center": (x + width // 8, y + height // 10, (width * 3) // 4, (height * 4) // 5),
    }
    nx, ny, nw, nh = box_map[pos]
    user32.ShowWindow(hwnd, 9)
    user32.MoveWindow(hwnd, nx, ny, nw, nh, True)
    _bring_window_to_front(win["hwnd"], minimized=False)
    return f"Placed '{win['title']}' at {pos} on monitor {mon['index']} ({nx},{ny} {nw}x{nh})."


def place_multiple_windows_impl(
    placements: list[tuple[str, str, int]],
    read_only: bool | None = None,
) -> str:
    """Place multiple windows in one layout pass (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to move, snap, or focus windows."
    if sys.platform != "win32":
        return "ERROR: Desktop window control is supported only on Windows."
    results = [_place_single_window(t, position=p, monitor=m) for t, p, m in placements if t.strip()]
    return " ".join(results) if results else "ERROR: No window placement specs provided."


def place_window_impl(target: str, position: str = "left", monitor: int = 1, read_only: bool | None = None) -> str:
    """Move, snap, focus, minimize, or maximize one window or '+'-chained layout specs."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to move, snap, or focus windows."
    if sys.platform != "win32":
        return "ERROR: Desktop window control is supported only on Windows."

    if "+" in target or ";" in target:
        parts = [p.strip() for p in re.split(r"[+;]", target) if p.strip()]
        specs: list[tuple[str, str, int]] = []
        for part in parts:
            m = _CHAIN_CLAUSE_RE.match(part)
            if m:
                specs.append((m.group("target").strip(), m.group("pos").strip(), int(m.group("mon") or monitor)))
            else:
                specs.append((part, position, monitor))
        return place_multiple_windows_impl(specs, read_only=False)

    return _place_single_window(target, position=position, monitor=monitor)


def close_window_impl(target: str, read_only: bool | None = None) -> str:
    """Gracefully close matching open window(s) via WM_CLOSE (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to close windows."
    if sys.platform != "win32":
        return "ERROR: Closing desktop windows is supported only on Windows."

    parts = [p.strip() for p in re.split(r"(?:\s*[+,;]\s*|\s+and\s+)", target or "", flags=re.IGNORECASE) if p.strip()]
    if not parts:
        return "ERROR: Target window or app name cannot be empty."

    user32 = _get_user32()
    closed_titles: list[str] = []
    seen_hwnds: set[int] = set()
    missing: list[str] = []

    for part in parts:
        hits = _find_windows(part)
        if not hits:
            missing.append(part)
            continue
        for win in hits:
            hwnd = int(win["hwnd"])
            if hwnd in seen_hwnds:
                continue
            seen_hwnds.add(hwnd)
            # WM_CLOSE = 0x0010 allows unsaved-work prompts rather than force-killing the process.
            user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)
            closed_titles.append(win["title"])

    if not closed_titles:
        return f"NOT FOUND: No open window matching '{target}'."
    msg = f"Closed {len(closed_titles)} window(s): {', '.join(closed_titles)}."
    if missing:
        msg += f" (Not open: {', '.join(missing)}.)"
    return msg


def media_control_impl(action: str = "playpause", read_only: bool | None = None) -> str:
    """Send Windows virtual media/volume transport keys (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to control media playback."
    canon = re.sub(r"[^a-z]", "", (action or "").lower())
    vk = _MEDIA_KEYS.get(canon)
    if vk is None:
        return f"ERROR: Unsupported media action '{action}'. Use playpause, next, previous, stop, mute, volumeup, or volumedown."
    if sys.platform != "win32":
        return "ERROR: Media controls are supported only on Windows."
    user32 = _get_user32()
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
    return f"Triggered media key: {canon}."


def _todo_file_path(storage_dir: Path | None = None) -> Path:
    if storage_dir is not None:
        base = Path(storage_dir)
    else:
        env_dir = os.environ.get("AURA_STORAGE_DIR", "").strip()
        if env_dir:
            base = Path(env_dir).expanduser()
        else:
            from ..config.settings import get_default_storage_dir

            base = get_default_storage_dir()
    return base / "Jarvis_Notes.md"


_TODO_TS_RE = re.compile(r"\s+_\(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\)_$")


def list_todo_items(storage_dir: Path | None = None) -> list[tuple[str, bool]]:
    """Return structured `(text, done)` items from Jarvis_Notes.md."""
    path = _todo_file_path(storage_dir)
    if not path.is_file():
        return []
    items: list[tuple[str, bool]] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("- [ ] "):
            txt = _TODO_TS_RE.sub("", line[6:].strip()).strip()
            if txt:
                items.append((txt, False))
        elif line.startswith(("- [x] ", "- [X] ")):
            txt = _TODO_TS_RE.sub("", line[6:].strip()).strip()
            if txt:
                items.append((txt, True))
    return items


def toggle_todo_item(text: str, done: bool, storage_dir: Path | None = None) -> list[tuple[str, bool]]:
    """Set completion state on a To-Do item in Jarvis_Notes.md and return updated items."""
    target = _TODO_TS_RE.sub("", (text or "").strip()).strip().lower()
    if not target:
        return list_todo_items(storage_dir)
    path = _todo_file_path(storage_dir)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    desired_prefix = "- [x] " if done else "- [ ] "
    changed = False
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped[:6] in ("- [ ] ", "- [x] ", "- [X] "):
            item_raw = stripped[6:].strip()
            item_clean = _TODO_TS_RE.sub("", item_raw).strip().lower()
            if item_clean == target or item_raw.lower() == target:
                lines[idx] = f"{desired_prefix}{item_raw}"
                changed = True
                break
    if changed:
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return list_todo_items(storage_dir)


def todo_note_impl(
    action: str = "read",
    text: str = "",
    *,
    storage_dir: Path | None = None,
    read_only: bool | None = None,
) -> str:
    """Read, add, complete, clear, or open persistent markdown To-Do notes."""
    act = (action or "read").strip().lower()
    path = _todo_file_path(storage_dir)

    if act in {"read", "list", "view", "show"}:
        if not path.is_file():
            return "(To-Do list is empty)"
        content = path.read_text(encoding="utf-8").strip()
        return content or "(To-Do list is empty)"

    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to modify or open To-Do notes."

    if act in {"add", "note", "append"}:
        item = " ".join((text or "").split()).strip()
        if not item:
            return "ERROR: Note or To-Do item text cannot be empty."
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "# Jarvis Notes & To-Do List\n\n" if not path.is_file() else ""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"- [ ] {item} _({stamp})_\n"
        with path.open("a", encoding="utf-8") as fh:
            if header:
                fh.write(header)
            fh.write(line)
        return f"Added to {path.name}: {item}"

    if act == "open":
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_text("# Jarvis Notes & To-Do List\n\n", encoding="utf-8")
        if sys.platform == "win32" and os.environ.get("AURA_HEADLESS_TEST") != "1":
            try:
                os.startfile(str(path))
            except OSError:
                subprocess.Popen(["notepad.exe", str(path)], shell=False)
        body = path.read_text(encoding="utf-8").strip()
        return f"Opened {path.name} ({path}).\n{body}"

    if act in {"done", "complete", "check"}:
        needle = (text or "").strip().lower()
        if not needle:
            return "ERROR: Specify the To-Do item text to mark complete."
        if not path.is_file():
            return "(To-Do list is empty)"
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = False
        for idx, ln in enumerate(lines):
            if ln.startswith("- [ ] ") and needle in ln.lower():
                lines[idx] = ln.replace("- [ ] ", "- [x] ", 1)
                updated = True
                break
        if not updated:
            return f"NOT FOUND: No open To-Do item matching '{text}'."
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"Marked completed in {path.name}: {text}"

    if act == "clear":
        if not path.is_file():
            return "(To-Do list is already empty)"
        if (text or "").strip().lower() == "all":
            path.write_text("# Jarvis Notes & To-Do List\n\n", encoding="utf-8")
            return f"Cleared all items from {path.name}."
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if not ln.startswith("- [x] ")]
        path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        return f"Cleared completed items from {path.name}."

    return f"ERROR: Unsupported To-Do action '{action}'. Use read, add, open, complete, or clear."


# ============================================================
# SAFE APP LAUNCHER
# ============================================================

def _is_unsafe_app_token(app_name: str, args: str) -> str | None:
    raw = f"{app_name} {args}".strip()
    if any(tok in raw for tok in ("&", "|", ";", "`", "$(", ">", "<", "\n", "\r")):
        return "ERROR: Shell control characters are not allowed in app launch."
    stem = Path(app_name.strip().strip('"')).name.lower()
    if stem in _BLOCKED_APPS or stem.removesuffix(".exe") in _BLOCKED_APPS:
        return f"BLOCKED: Refusing to launch restricted system utility '{stem}'."
    return None


_START_MENU_CACHE: dict[str, Any] = {"ts": 0.0, "roots": None, "links": []}


def _common_windows_app_paths(exe_name: str) -> list[Path]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LocalAppData", "")
    key = exe_name.lower()
    mapping: dict[str, list[Path]] = {
        "chrome.exe": [
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ],
        "msedge.exe": [
            Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ],
        "firefox.exe": [
            Path(pf) / "Mozilla Firefox" / "firefox.exe",
            Path(pf86) / "Mozilla Firefox" / "firefox.exe",
        ],
        "brave.exe": [
            Path(pf) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            Path(local) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        ],
        "code.cmd": [
            Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
            Path(pf) / "Microsoft VS Code" / "bin" / "code.cmd",
        ],
        "spotify.exe": [
            Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe",
        ],
        "discord.exe": [
            Path(local) / "Discord" / "Update.exe",
        ],
    }
    return [p for p in mapping.get(key, ()) if str(p)]


def _lookup_app_path(exe_name: str) -> str | None:
    hit = shutil.which(exe_name)
    if hit:
        return hit
    if sys.platform != "win32":
        return None
    for cand in _common_windows_app_paths(exe_name):
        if cand.is_file():
            return str(cand)
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_name in (exe_name, f"{exe_name}.exe"):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{key_name}") as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val and Path(val.strip('"')).exists():
                            return val.strip('"')
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _find_start_menu_shortcut(app_name: str) -> Path | None:
    if sys.platform != "win32":
        return None
    needle = app_name.strip().lower()
    if len(needle) < 2:
        return None
    roots = (
        Path(os.environ.get("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    )
    root_sig = tuple(str(r) for r in roots)
    now = time.monotonic()
    links: list[Path]
    if _START_MENU_CACHE["roots"] == root_sig and now - float(_START_MENU_CACHE["ts"]) < 60.0:
        links = _START_MENU_CACHE["links"]
    else:
        links = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                links.extend(root.rglob("*.lnk"))
            except OSError:
                continue
        _START_MENU_CACHE["roots"] = root_sig
        _START_MENU_CACHE["ts"] = now
        _START_MENU_CACHE["links"] = links

    for lnk in links:
        if lnk.stem.lower() == needle and lnk.exists():
            return lnk
    for lnk in links:
        if needle in lnk.stem.lower() and lnk.exists():
            return lnk
    return None


def launch_app_impl(app_name: str, args: str = "", read_only: bool | None = None) -> str:
    """Safely launch an allowlisted Windows application or Settings/app URI."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to launch applications."
    clean = app_name.strip()
    if not clean:
        return "ERROR: app_name cannot be empty."

    blocked = _is_unsafe_app_token(clean, args)
    if blocked:
        return blocked

    parsed_args = shlex.split(args) if args.strip() else []
    uri = _WINDOWS_URIS.get(clean.lower()) or (clean if _SETTINGS_URI_RE.match(clean) else None)
    if uri and not parsed_args:
        if sys.platform != "win32":
            return f"Opened Windows URI: {uri}"
        try:
            os.startfile(uri)
            return f"Opened {clean} ({uri})."
        except OSError as exc:
            return f"ERROR opening '{uri}': {exc}"

    mapped = _APP_ALIASES.get(clean.lower(), clean)
    if mapped.lower() in {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"} and parsed_args:
        try:
            from .jarvis_server import check_ssrf, resolve_site_target
        except ImportError:
            from aura.mcp.jarvis_server import check_ssrf, resolve_site_target
        normalized_args: list[str] = []
        for arg in parsed_args:
            tok = arg
            if not tok.startswith("-"):
                site_url = resolve_site_target(tok)
                if site_url:
                    tok = site_url
                if tok.lower().startswith(("http://", "https://")):
                    ssrf_err = check_ssrf(tok)
                    if ssrf_err:
                        return f"SSRF BLOCKED: {ssrf_err}"
            normalized_args.append(tok)
        parsed_args = normalized_args
    resolved = _lookup_app_path(mapped)

    if resolved:
        kwargs: dict[str, Any] = {"shell": False}
        if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen([resolved, *parsed_args], **kwargs)
        return f"Launched {Path(resolved).name}."

    lnk = _find_start_menu_shortcut(clean)
    if lnk and not parsed_args and sys.platform == "win32":
        os.startfile(str(lnk))
        return f"Launched shortcut '{lnk.stem}'."

    return f"NOT FOUND: Could not find application '{app_name}'."


# ============================================================
# FILE SEARCH (WINDOWS SEARCH INDEX + SAFE FALLBACK)
# ============================================================

def _normalize_name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _candidate_search_roots(home: Path, location: str = "") -> list[Path]:
    loc = (location or "").strip().lower()
    mapping: dict[str, list[Path]] = {
        "desktop": [home / "Desktop", home / "OneDrive" / "Desktop"],
        "downloads": [home / "Downloads"],
        "documents": [home / "Documents", home / "OneDrive" / "Documents"],
        "home": [home],
    }
    ordered = (
        mapping[loc]
        if loc in mapping
        else [
            home / "Desktop",
            home / "OneDrive" / "Desktop",
            home / "Documents",
            home / "OneDrive" / "Documents",
            home / "Downloads",
        ]
    )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in ordered:
        key = str(candidate).lower()
        if key not in seen and candidate.is_dir():
            seen.add(key)
            roots.append(candidate)
    return roots


_SKIP_SEARCH_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", ".git", "dist", "build", ".next", "coverage",
}
_DOC_PREVIEW_CANDIDATES = (
    "README.md", "README.txt", "README.rst", "README", "readme.md",
    "package.json", "pyproject.toml", "Cargo.toml", "requirements.txt",
)


def _is_sensitive_or_symlink_target(path: Path) -> bool:
    if _is_sensitive_path(path):
        return True
    try:
        resolved = path.resolve(strict=False)
        if _is_sensitive_path(resolved):
            return True
    except OSError:
        return True
    return False


def _score_local_match(path: Path, q_low: str, norm_q: str, q_tokens: list[str], depth: int) -> tuple[int, int, int, int] | None:
    if _is_sensitive_or_symlink_target(path):
        return None
    name_low = path.name.lower()
    stem_low = path.stem.lower()
    norm_name = _normalize_name_key(name_low)
    norm_stem = _normalize_name_key(stem_low)
    name_tokens = set(re.findall(r"[a-z0-9]+", name_low))
    if name_low == q_low:
        tier = 0
    elif stem_low == q_low:
        tier = 1
    elif norm_q and norm_name == norm_q:
        tier = 2
    elif norm_q and norm_stem == norm_q:
        tier = 3
    elif (
        q_low in name_low
        or (norm_q and norm_q in norm_name)
        or (len(q_tokens) >= 2 and all(t in name_tokens or t in norm_name for t in q_tokens))
    ):
        tier = 4
    else:
        return None
    # Prefer direct/exact, shallower depth, folders on equal score, and shorter names
    return (tier, depth, 0 if path.is_dir() else 1, len(path.name))


def _search_local_paths(query: str, max_results: int = 20, location: str = "") -> list[Path]:
    """Search Desktop/Documents/Downloads (files and folders) then Windows Search Index."""
    clean_q = re.sub(r"[^A-Za-z0-9._\-\s]", " ", query).strip()
    clean_q = re.sub(r"\s+", " ", clean_q)
    if len(_normalize_name_key(clean_q)) < 2:
        return []
    limit = max(1, min(int(max_results), 40))
    q_low = clean_q.lower()
    norm_q = _normalize_name_key(clean_q)
    q_tokens = re.findall(r"[a-z0-9]+", q_low)

    home = Path.home()
    roots = _candidate_search_roots(home, location=location)
    scored: list[tuple[tuple[int, int, int, int], Path]] = []
    seen_paths: set[str] = set()
    visited_dirs = 0
    max_visited_dirs = 1500
    max_depth = 4

    # Breadth-first scan level by level so direct Desktop/Downloads hits return in milliseconds
    queue: deque[tuple[Path, int]] = deque((r, 0) for r in roots)
    current_level = 0
    while queue and visited_dirs < max_visited_dirs:
        cur_dir, cur_depth = queue.popleft()
        if cur_depth > current_level:
            # Stop early as soon as a shallow level already yielded strong exact/stem/normalized matches
            if any(sc[0] <= 3 for sc, _ in scored):
                break
            current_level = cur_depth
        visited_dirs += 1
        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        is_link = entry.is_symlink()
                    except OSError:
                        is_link = True
                    if is_link:
                        continue
                    cand = Path(entry.path)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir and name.lower() in _SKIP_SEARCH_DIRS:
                        continue
                    sc = _score_local_match(cand, q_low, norm_q, q_tokens, cur_depth)
                    if sc is not None:
                        k = str(cand).lower()
                        if k not in seen_paths:
                            seen_paths.add(k)
                            scored.append((sc, cand))
                    if is_dir and cur_depth < max_depth:
                        queue.append((cand, cur_depth + 1))
        except OSError:
            continue

    scored.sort(key=lambda item: item[0])
    matches = [p for _, p in scored[:limit]]

    if not matches and not location and sys.platform == "win32":
        ps_q = clean_q.replace("'", "''")
        ps_script = f"""
$q = '{ps_q}'
$conn = New-Object -ComObject ADODB.Connection
try {{
    $conn.Open('Provider=Search.CollatorDSO;Extended Properties="Application=Windows";')
    $rs = $conn.Execute("SELECT TOP {limit} System.ItemPathDisplay FROM SystemIndex WHERE CONTAINS(System.FileName, '`"$q*`"') OR System.FileName LIKE '%$q%'")
    while (-not $rs.EOF) {{
        $p = $rs.Fields.Item(0).Value
        if ($p) {{ Write-Output $p }}
        $rs.MoveNext()
    }}
}} catch {{ }} finally {{
    if ($conn.State -eq 1) {{ $conn.Close() }}
}}
"""
        try:
            rc, out, _ = _run_powershell(ps_script, timeout=8)
            if rc == 0 and out:
                for line in out.splitlines():
                    raw = line.strip()
                    if not raw:
                        continue
                    cand = Path(raw)
                    if (
                        not _is_sensitive_or_symlink_target(cand)
                        and cand.exists()
                        and raw.lower() not in seen_paths
                    ):
                        seen_paths.add(raw.lower())
                        matches.append(cand)
        except Exception:
            pass

    return matches[:limit]


def find_files_impl(query: str, max_results: int = 20, location: str = "") -> str:
    """Find files and folders on this PC (Desktop/Documents/Downloads first, then Windows Search Index)."""
    clean_q = re.sub(r"[^A-Za-z0-9._\-\s]", " ", query).strip()
    clean_q = re.sub(r"\s+", " ", clean_q)
    if len(re.sub(r"[^A-Za-z0-9]", "", clean_q)) < 2:
        return "ERROR: Search query must contain at least 2 alphanumeric characters."
    matches = _search_local_paths(clean_q, max_results=max_results, location=location)
    if not matches:
        return f"No files found matching '{clean_q}'."
    return "\n".join(str(p) for p in matches)


def _resolve_local_path(target: str, location: str = "") -> tuple[Path | None, Path | None, str | None]:
    """
    Resolve a local file/folder path or Desktop/Documents/Downloads query.

    Returns (raw_candidate, resolved_path, error_message).
    """
    raw = (target or "").strip().strip("\"'")
    if not raw:
        return None, None, "ERROR: Please specify a file or folder path or name."
    if _is_unc_path(raw):
        return None, None, f"BLOCKED: Refusing to access network/UNC path '{raw}'."

    expanded = os.path.expandvars(os.path.expanduser(raw))
    if _is_unc_path(expanded):
        return None, None, f"BLOCKED: Refusing to access network/UNC path '{raw}'."

    is_explicit_abs = (
        (len(expanded) >= 2 and expanded[1] == ":")
        or raw.startswith(("~/", "~\\", "./", ".\\"))
        or Path(expanded).is_absolute()
    )

    candidate: Path | None = None
    if is_explicit_abs:
        direct = Path(expanded)
        if not direct.exists():
            return None, None, f"NOT FOUND: Path '{expanded}' does not exist."
        candidate = direct
    else:
        norm_rel = raw.replace("\\", "/").lstrip("/")
        if ".." in Path(norm_rel).parts:
            return None, None, "BLOCKED: Path traversal ('..') is not allowed."
        roots = _candidate_search_roots(Path.home(), location=location)
        for base in roots:
            probe = base / norm_rel
            if probe.exists():
                candidate = probe
                break

        if candidate is None and "/" in norm_rel:
            parts = [p for p in norm_rel.split("/") if p and p != "."]
            if parts:
                head_hits = _search_local_paths(parts[0], max_results=6, location=location)
                for hit in head_hits:
                    joined = hit.joinpath(*parts[1:])
                    if joined.exists():
                        candidate = joined
                        break

        if candidate is None:
            hits = _search_local_paths(raw, max_results=10, location=location)
            if not hits:
                stripped = re.sub(r"\s+(?:file|folder|directory|dir)$", "", raw, flags=re.IGNORECASE).strip()
                if stripped and stripped.lower() != raw.lower():
                    hits = _search_local_paths(stripped, max_results=10, location=location)
            if hits:
                candidate = hits[0]

        if candidate is None:
            direct = Path(expanded)
            if direct.exists():
                candidate = direct

    if candidate is None:
        loc_note = f" in {location.title()}" if location else " on Desktop, Documents, or Downloads"
        return None, None, f"NOT FOUND: Could not find file or folder '{raw}'{loc_note}."

    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate

    return candidate, resolved, None


def open_path_impl(
    target: str,
    in_explorer: bool = False,
    location: str = "",
    read_only: bool | None = None,
) -> str:
    """Directly open a local file or folder (or reveal it in File Explorer) without mouse clicks."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to open local files or folders."

    candidate, resolved, err = _resolve_local_path(target, location=location)
    if err is not None or resolved is None or candidate is None:
        return err or "NOT FOUND: Could not resolve local path."

    if len(resolved.parts) <= 1 or _is_sensitive_path(candidate) or _is_sensitive_path(resolved):
        return f"BLOCKED: Refusing to open sensitive or protected system path '{resolved}'."

    if sys.platform != "win32":
        return f"Opened path: {resolved}"

    try:
        if resolved.is_dir():
            os.startfile(str(resolved))
            return f"Opened folder in File Explorer: {resolved}"
        if in_explorer:
            subprocess.Popen(["explorer.exe", f"/select,{resolved}"], shell=False)
            return f"Opened File Explorer highlighting: {resolved}"
        os.startfile(str(resolved))
        return f"Opened file directly: {resolved}"
    except OSError as exc:
        return f"ERROR: Could not open '{resolved}': {exc}"


def _is_within_inspectable_roots(resolved: Path) -> bool:
    """Ensure local inspection remains inside user profile, active workspace, or temp dir."""
    allowed_roots: list[Path] = []
    try:
        allowed_roots.append(Path.home().resolve(strict=False))
    except OSError:
        pass
    try:
        allowed_roots.append(Path(tempfile.gettempdir()).resolve(strict=False))
    except OSError:
        pass
    ws_env = os.environ.get("AURA_WORKSPACE", "").strip()
    if ws_env:
        try:
            allowed_roots.append(Path(ws_env).resolve(strict=False))
        except OSError:
            pass

    for root in allowed_roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except Exception:
            continue
    return False


def _read_local_document_text(path: Path, start_line: int = 1, max_chars: int = 2600) -> tuple[str, str]:
    """Read text/code, PDF, or DOCX safely with bounded character output. Returns (meta, text)."""
    ext = path.suffix.lower()
    size_bytes = path.stat().st_size

    if ext == ".pdf":
        extracted = ""
        page_note = ""
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            chunks: list[str] = []
            for idx, page in enumerate(reader.pages[:12], 1):
                txt = (page.extract_text() or "").strip()
                if txt:
                    chunks.append(f"[Page {idx}]\n{txt}")
                if sum(len(c) for c in chunks) >= max_chars:
                    break
            extracted = "\n\n".join(chunks)
            page_note = f", {len(reader.pages)} pages"
        except Exception:
            try:
                import pymupdf

                with pymupdf.open(str(path)) as doc:
                    chunks = []
                    for idx, page in enumerate(doc[:12], 1):
                        txt = (page.get_text("text") or "").strip()
                        if txt:
                            chunks.append(f"[Page {idx}]\n{txt}")
                        if sum(len(c) for c in chunks) >= max_chars:
                            break
                    extracted = "\n\n".join(chunks)
                    page_note = f", {len(doc)} pages"
            except Exception as exc:
                return f"PDF ({size_bytes} bytes)", f"ERROR: Could not extract PDF text ({exc})."

        if not extracted.strip():
            return f"PDF ({size_bytes} bytes{page_note})", "(no extractable text found in PDF)"
        if len(extracted) > max_chars:
            extracted = extracted[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return f"PDF ({size_bytes} bytes{page_note})", extracted

    if ext == ".docx":
        try:
            import docx

            document = docx.Document(str(path))
            paras = [p.text.strip() for p in document.paragraphs if p.text.strip()]
            extracted = "\n".join(paras)
            if not extracted:
                return f"DOCX ({size_bytes} bytes)", "(document has no paragraph text)"
            if len(extracted) > max_chars:
                extracted = extracted[:max_chars] + f"\n... [truncated at {max_chars} chars]"
            return f"DOCX ({size_bytes} bytes)", extracted
        except Exception as exc:
            return f"DOCX ({size_bytes} bytes)", f"ERROR: Could not read DOCX ({exc})."

    with path.open("rb") as fh:
        sample = fh.read(4096)
        if b"\x00" in sample:
            return (
                f"binary ({size_bytes} bytes)",
                f"BINARY FILE: '{path.name}' ({size_bytes} bytes) is binary and cannot be displayed as text.",
            )

    # Read bounded UTF-8 slice from disk so huge log files never exhaust memory
    max_read_bytes = 512 * 1024
    with path.open("rb") as fh:
        raw_bytes = fh.read(max_read_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    truncated_disk = size_bytes > max_read_bytes

    lines = text.splitlines()
    total_lines = len(lines)
    start_idx = max(0, int(start_line) - 1)
    if start_idx >= total_lines and total_lines > 0:
        return f"text ({size_bytes} bytes, {total_lines} lines)", f"(start_line {start_line} exceeds {total_lines} lines)"

    selected = lines[start_idx:]
    joined = "\n".join(selected)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    elif truncated_disk:
        joined += "\n... [file exceeded 512 KB read cap]"
    meta = f"text ({size_bytes} bytes, {total_lines} lines{'+' if truncated_disk else ''})"
    return meta, joined


def inspect_path_impl(
    target: str,
    location: str = "",
    start_line: int = 1,
    max_chars: int = 2600,
) -> str:
    """Safely read/inspect a non-sensitive local file or folder (Desktop, Documents, Downloads, or explicit path; RO & RW safe)."""
    candidate, resolved, err = _resolve_local_path(target, location=location)
    if err is not None or resolved is None or candidate is None:
        return err or "NOT FOUND: Could not resolve local path."

    if (
        len(resolved.parts) <= 1
        or _is_sensitive_path(candidate)
        or _is_sensitive_path(resolved)
        or any(p.lower() == ".git" for p in resolved.parts)
        or not _is_within_inspectable_roots(resolved)
    ):
        return f"BLOCKED: Refusing to inspect sensitive or protected path '{resolved}'."

    cap = max(200, min(int(max_chars), 6000))

    if resolved.is_dir():
        dirs_list: list[str] = []
        files_list: list[tuple[str, Path]] = []
        hidden_skipped = 0
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return f"ERROR: Cannot list folder '{resolved}': {exc}"

        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.lower() in _SKIP_SEARCH_DIRS or _is_sensitive_or_symlink_target(entry):
                hidden_skipped += 1
                continue
            if entry.is_dir():
                dirs_list.append(f"[DIR] {name}/")
            else:
                try:
                    sz = entry.stat().st_size
                    sz_str = f"{sz / 1024:.1f} KB" if sz >= 1024 else f"{sz} B"
                except OSError:
                    sz_str = "?"
                files_list.append((f"[FILE] {name} ({sz_str})", entry))

        listing_lines = (dirs_list + [lbl for lbl, _ in files_list])[:40]
        total_items = len(dirs_list) + len(files_list)
        parts = [f"Folder: {resolved} ({len(dirs_list)} subdirs, {len(files_list)} files)"]
        if listing_lines:
            parts.append("\n".join(listing_lines))
            if total_items > len(listing_lines):
                parts.append(f"... ({total_items - len(listing_lines)} more entries)")
        else:
            parts.append("(empty or contains only hidden/skipped entries)")
        if hidden_skipped:
            parts.append(f"({hidden_skipped} hidden/sensitive/dependency entries omitted)")

        # Auto-preview README or manifest file so compound open+read+research succeeds in one call
        preview_file: Path | None = None
        by_name = {p.name.lower(): p for _, p in files_list}
        for cand_name in _DOC_PREVIEW_CANDIDATES:
            if cand_name.lower() in by_name:
                preview_file = by_name[cand_name.lower()]
                break
        if preview_file is None:
            for _, p in files_list:
                if p.suffix.lower() in {".md", ".txt", ".rst"}:
                    preview_file = p
                    break

        if preview_file is not None and not _is_sensitive_or_symlink_target(preview_file):
            preview_budget = max(400, cap - sum(len(x) for x in parts) - 80)
            _, preview_txt = _read_local_document_text(preview_file, start_line=1, max_chars=min(1400, preview_budget))
            parts.append(f"--- Preview: {preview_file.name} ---\n{preview_txt}")

        out = "\n".join(parts)
        return out[:cap] + ("\n... [truncated]" if len(out) > cap else "")

    if not resolved.is_file():
        return f"ERROR: '{resolved}' is neither a readable file nor a folder."

    try:
        meta, content = _read_local_document_text(resolved, start_line=start_line, max_chars=cap)
    except OSError as exc:
        return f"ERROR reading '{resolved}': {exc}"
    header = f"File: {resolved} [{meta}]"
    if start_line > 1:
        header += f" (from line {start_line})"
    out = f"{header}\n{content}"
    return out


read_local_file_impl = inspect_path_impl


# ============================================================
# SCREEN / WINDOW OCR (BUILT-IN WINRT OCR)
# ============================================================

def _ocr_image_file(img_path: str) -> tuple[bool, str]:
    """Run built-in Windows 10/11 WinRT OcrEngine on an image file path."""
    if not img_path or not Path(img_path).is_file():
        return False, "OCR image file not found."
    ps_ocr = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait()
    return $t.Result
}
$stream = $null
try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:AURA_OCR_IMAGE)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("en-US"))
    }
    if ($null -eq $engine) {
        Write-Output ""
        exit 0
    }
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    if ($res.Lines -and $res.Lines.Count -gt 0) {
        ($res.Lines | ForEach-Object { $_.Text }) -join "`n"
    } else {
        Write-Output $res.Text
    }
} finally {
    if ($null -ne $stream) { $stream.Dispose() }
}
"""
    rc, out, err = _run_powershell(ps_ocr, env={"AURA_OCR_IMAGE": img_path}, timeout=15)
    if rc != 0:
        return False, (err or f"exit {rc}")
    return True, out.strip()


def _clamp_bbox_to_screen(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    min_x, min_y, max_x, max_y = _get_screen_bounds()
    left = max(min_x, int(bbox[0]))
    top = max(min_y, int(bbox[1]))
    right = min(max_x, int(bbox[2]))
    bottom = min(max_y, int(bbox[3]))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def read_screen_ocr_impl(target: str = "") -> str:
    """Capture active screen or target window and read visible text via Windows WinRT OCR."""
    if sys.platform != "win32":
        return "ERROR: Built-in Windows OCR is supported only on Windows."

    bbox: tuple[int, int, int, int] | None = None
    label = "primary screen"
    if target.strip():
        win = _find_window(target)
        if win is None:
            return f"NOT FOUND: No open window matching '{target}'."
        if win.get("minimized"):
            return f"WINDOW MINIMIZED: '{win['title']}' is minimized; restore or focus it before reading its screen."
        r = win["rect"]
        clamped = _clamp_bbox_to_screen((r["left"], r["top"], r["right"], r["bottom"]))
        if clamped is None:
            return f"ERROR: Window '{win['title']}' has no visible on-screen area."
        bbox = clamped
        label = f"window '{win['title']}'"

    try:
        from PIL import ImageGrab
    except ImportError:
        return "ERROR: Pillow (PIL.ImageGrab) is required for screen capture."

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=bool(bbox))
        except OSError as exc:
            return f"ERROR: Screen capture unavailable ({exc}). Unlock desktop or run in an interactive Windows session."
        try:
            img.save(tmp_path)
        finally:
            try:
                img.close()
            except Exception:
                pass

        ok, res = _ocr_image_file(tmp_path)
        if not ok:
            return f"OCR ERROR ({label}): {res}"
        if not res:
            return f"[OCR on {label}]: (no readable text detected)"
        if len(res) > 4000:
            res = res[:4000] + "\n... [truncated]"
        return f"[OCR on {label}]:\n{res}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ============================================================
# CLIPBOARD, RECYCLE-BIN TRASH & WINDOWS UI AUTOMATION
# ============================================================

def clipboard_impl(action: str = "read", text: str = "", read_only: bool | None = None) -> str:
    """Read Windows clipboard (RO allowed) or set clipboard text (RW required)."""
    if sys.platform != "win32":
        return "ERROR: Clipboard access is supported only on Windows."
    act = action.strip().lower()
    if act in {"read", "get"}:
        rc, out, err = _run_powershell("Get-Clipboard -Raw", timeout=5)
        if rc != 0:
            return f"ERROR reading clipboard: {err or f'exit {rc}'}"
        clipped = out[:4000] + ("\n... [truncated]" if len(out) > 4000 else "")
        return clipped or "(clipboard is empty)"
    if act in {"write", "set", "copy"}:
        if _is_read_only(read_only):
            return "READ-ONLY BLOCKED: Switch to RW mode to write to clipboard."
        rc, _, err = _run_powershell("Set-Clipboard -Value $env:AURA_CLIP_TEXT", env={"AURA_CLIP_TEXT": text}, timeout=5)
        return "Copied text to clipboard." if rc == 0 else f"ERROR setting clipboard: {err or f'exit {rc}'}"
    return f"ERROR: Unsupported clipboard action '{action}'. Use 'read' or 'set'."


def trash_file_impl(path: str, read_only: bool | None = None) -> str:
    """Move a file or directory to the Windows Recycle Bin (undoable; RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to move files to the Recycle Bin."
    if sys.platform != "win32":
        return "ERROR: Recycle Bin move is supported only on Windows."
    raw = path.strip()
    if not raw:
        return "ERROR: path cannot be empty."
    if _is_unc_path(raw):
        return f"BLOCKED: Refusing to trash network/UNC path '{raw}'."
    raw_path = Path(raw).expanduser()
    target = raw_path.resolve(strict=False)
    if len(target.parts) <= 2:
        return f"BLOCKED: Refusing to trash top-level or root directory '{target}'."
    low_parts = {p.lower() for p in target.parts}
    if (
        low_parts.intersection({"windows", "program files", "program files (x86)", "system32", ".ssh", ".gnupg", ".aura"})
        or _is_sensitive_path(raw_path)
        or _is_sensitive_path(target)
    ):
        return f"BLOCKED: Refusing to trash protected system/security path '{target}'."
    if target == Path.home().resolve(strict=False):
        return "BLOCKED: Refusing to trash user home directory."
    if not target.exists():
        return f"NOT FOUND: '{target}' does not exist."

    method = "DeleteDirectory" if target.is_dir() else "DeleteFile"
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}($env:AURA_TRASH_PATH, 'OnlyErrorDialogs', 'SendToRecycleBin')"
    )
    rc, _, err = _run_powershell(ps, env={"AURA_TRASH_PATH": str(target)}, timeout=10)
    if rc != 0:
        return f"ERROR moving to Recycle Bin: {err or f'exit {rc}'}"
    if not target.exists():
        return f"Moved to Recycle Bin: {target}"
    return f"ERROR: Target still exists after Recycle Bin request: {target}"


def ui_see_impl(target: str, max_items: int = 60) -> str:
    """Inspect interactive buttons/tabs/inputs inside a window using Windows UI Automation."""
    if sys.platform != "win32":
        return "ERROR: Windows UI Automation is supported only on Windows."
    if not (target or "").strip():
        return "ERROR: target window name cannot be empty."
    win = _find_window(target)
    if win is None:
        return f"NOT FOUND: No open window matching '{target}'."
    limit = max(5, min(int(max_items), 120))
    ps = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new([int64]{int(win['hwnd'])}))
if ($null -eq $root) {{ Write-Output 'ERROR: Could not attach to window'; exit 1 }}
$ctProp = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
$typeConds = @(
    [System.Windows.Automation.ControlType]::Button,
    [System.Windows.Automation.ControlType]::MenuItem,
    [System.Windows.Automation.ControlType]::TabItem,
    [System.Windows.Automation.ControlType]::Edit,
    [System.Windows.Automation.ControlType]::CheckBox,
    [System.Windows.Automation.ControlType]::Hyperlink,
    [System.Windows.Automation.ControlType]::ComboBox,
    [System.Windows.Automation.ControlType]::RadioButton,
    [System.Windows.Automation.ControlType]::ListItem,
    [System.Windows.Automation.ControlType]::TreeItem
) | ForEach-Object {{ New-Object System.Windows.Automation.PropertyCondition($ctProp, $_) }}
$orTypes = New-Object System.Windows.Automation.OrCondition($typeConds)
$notEmpty = New-Object System.Windows.Automation.NotCondition((New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "")))
$cond = New-Object System.Windows.Automation.AndCondition($orTypes, $notEmpty)
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$count = 0
for ($i = 0; $i -lt $all.Count -and $count -lt {limit}; $i++) {{
    $el = $all.Item($i)
    $cur = $el.Current
    if ($cur.IsOffscreen) {{ continue }}
    $nm = ($cur.Name -replace '[\\r\\n]+', ' ').Trim()
    if ($nm) {{
        $shortType = $cur.ControlType.ProgrammaticName.Replace('ControlType.', '')
        Write-Output "[$shortType] $nm"
        $count++
    }}
}}
"""
    rc, out, err = _run_powershell(ps, timeout=10)
    if rc != 0:
        return f"ERROR inspecting UI of '{win['title']}': {err or f'exit {rc}'}"
    if out.strip():
        return out.strip()
    if win.get("minimized"):
        return f"Window '{win['title']}' is minimized; restore or focus it before inspecting UI controls."
    return f"No named interactive UI controls found inside '{win['title']}'."


def ui_click_impl(target: str, element_name: str, read_only: bool | None = None) -> str:
    """Click/invoke a named button, tab, or menu item inside a window (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to click UI elements inside applications."
    if sys.platform != "win32":
        return "ERROR: Windows UI Automation is supported only on Windows."
    if not (target or "").strip():
        return "ERROR: target window name cannot be empty."
    needle = element_name.strip()
    if not needle:
        return "ERROR: element_name cannot be empty."
    win = _find_window(target)
    if win is None:
        return f"NOT FOUND: No open window matching '{target}'."
    ps = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new([int64]{int(win['hwnd'])}))
if ($null -eq $root) {{ Write-Output 'ERROR: Window handle invalid'; exit 1 }}
$needle = $env:AURA_UI_TARGET.ToLowerInvariant()
$c1 = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty, $true)
$c2 = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsSelectionItemPatternAvailableProperty, $true)
$c3 = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsTogglePatternAvailableProperty, $true)
$c4 = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsExpandCollapsePatternAvailableProperty, $true)
$orPat = New-Object System.Windows.Automation.OrCondition($c1, $c2, $c3, $c4)
$notEmpty = New-Object System.Windows.Automation.NotCondition((New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "")))
$cond = New-Object System.Windows.Automation.AndCondition($orPat, $notEmpty)
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$chosen = $null
$chosenName = ''
for ($i = 0; $i -lt $all.Count; $i++) {{
    $el = $all.Item($i)
    $cur = $el.Current
    if (-not $cur.IsEnabled) {{ continue }}
    $nm = ($cur.Name -replace '[\\r\\n]+', ' ').Trim()
    if (-not $nm) {{ continue }}
    $low = $nm.ToLowerInvariant()
    if ($low -eq $needle) {{
        $chosen = $el
        $chosenName = $nm
        break
    }}
    if ($null -eq $chosen -and $low.Contains($needle)) {{
        $chosen = $el
        $chosenName = $nm
    }}
}}
if ($null -ne $chosen) {{
    $pat = $null
    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pat)) {{
        $pat.Invoke(); Write-Output "Clicked '$chosenName'"; exit 0
    }}
    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) {{
        $pat.Select(); Write-Output "Selected '$chosenName'"; exit 0
    }}
    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$pat)) {{
        $pat.Toggle(); Write-Output "Toggled '$chosenName'"; exit 0
    }}
    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pat)) {{
        $pat.Expand(); Write-Output "Expanded '$chosenName'"; exit 0
    }}
}}
Write-Output 'NOT_FOUND'
exit 2
"""
    rc, out, err = _run_powershell(ps, env={"AURA_UI_TARGET": needle}, timeout=10)
    if rc == 0 and out:
        return f"{out} in '{win['title']}'."
    if "NOT_FOUND" in out or rc == 2:
        return f"NOT FOUND: No invokable UI element matching '{element_name}' in '{win['title']}'."
    return f"ERROR clicking UI element: {err or out or f'exit {rc}'}"


def ui_set_impl(target: str, element_name: str, text: str, read_only: bool | None = None) -> str:
    """Set the value of a named text field inside a window via UI Automation ValuePattern (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to fill UI fields inside applications."
    if sys.platform != "win32":
        return "ERROR: Windows UI Automation is supported only on Windows."
    if not (target or "").strip():
        return "ERROR: target window name cannot be empty."
    needle = (element_name or "").strip()
    if not needle:
        return "ERROR: element_name cannot be empty."
    win = _find_window(target)
    if win is None:
        return f"NOT FOUND: No open window matching '{target}'."
    ps = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new([int64]{int(win['hwnd'])}))
if ($null -eq $root) {{ Write-Output 'ERROR: Window handle invalid'; exit 1 }}
$needle = $env:AURA_UI_TARGET.ToLowerInvariant()
$valCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsValuePatternAvailableProperty, $true)
$notEmpty = New-Object System.Windows.Automation.NotCondition((New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "")))
$cond = New-Object System.Windows.Automation.AndCondition($valCond, $notEmpty)
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$chosen = $null
$chosenName = ''
for ($i = 0; $i -lt $all.Count; $i++) {{
    $el = $all.Item($i)
    $cur = $el.Current
    if (-not $cur.IsEnabled) {{ continue }}
    $nm = ($cur.Name -replace '[\\r\\n]+', ' ').Trim()
    if (-not $nm) {{ continue }}
    $low = $nm.ToLowerInvariant()
    if ($low -eq $needle) {{
        $chosen = $el
        $chosenName = $nm
        break
    }}
    if ($null -eq $chosen -and $low.Contains($needle)) {{
        $chosen = $el
        $chosenName = $nm
    }}
}}
if ($null -ne $chosen) {{
    $pat = $null
    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)) {{
        $pat.SetValue($env:AURA_UI_VALUE)
        Write-Output "Set '$chosenName'"
        exit 0
    }}
}}
Write-Output 'NOT_FOUND'
exit 2
"""
    rc, out, err = _run_powershell(ps, env={"AURA_UI_TARGET": needle, "AURA_UI_VALUE": text}, timeout=10)
    if rc == 0 and out:
        return f"{out} in '{win['title']}'."
    if "NOT_FOUND" in out or rc == 2:
        return f"NOT FOUND: No editable ValuePattern field matching '{element_name}' in '{win['title']}'."
    return f"ERROR setting UI field: {err or out or f'exit {rc}'}"


# ============================================================
# BOUNDED MOUSE, KEYBOARD, HOTKEY & SCROLL CONTROL (PHASE 3)
# ============================================================

_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "windows": "win",
    "super": "win",
    "meta": "win",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
}

_BLOCKED_HOTKEY_SETS = (
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"win", "r"}),
    frozenset({"winleft", "r"}),
    frozenset({"winright", "r"}),
    frozenset({"win", "x"}),
    frozenset({"ctrl", "shift", "esc"}),
)


def _get_screen_bounds() -> tuple[int, int, int, int]:
    """Return (min_x, min_y, max_x, max_y) virtual or primary screen bounds."""
    try:
        import pyautogui

        sz = pyautogui.size()
        w, h = int(sz[0]), int(sz[1])
        if w > 0 and h > 0:
            mons = get_monitors()
            if mons and len(mons) > 1:
                min_x = min(m["bounds"]["left"] for m in mons)
                min_y = min(m["bounds"]["top"] for m in mons)
                max_x = max(m["bounds"]["left"] + m["bounds"]["width"] for m in mons)
                max_y = max(m["bounds"]["top"] + m["bounds"]["height"] for m in mons)
                return min_x, min_y, max_x, max_y
            return 0, 0, w, h
    except Exception:
        pass

    mons = get_monitors()
    if mons:
        return (
            min(m["bounds"]["left"] for m in mons),
            min(m["bounds"]["top"] for m in mons),
            max(m["bounds"]["left"] + m["bounds"]["width"] for m in mons),
            max(m["bounds"]["top"] + m["bounds"]["height"] for m in mons),
        )
    if sys.platform == "win32":
        u32 = _get_user32()
        if u32 is not None:
            return 0, 0, int(u32.GetSystemMetrics(0)), int(u32.GetSystemMetrics(1))
    return 0, 0, 1920, 1080


def mouse_click_impl(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    read_only: bool | None = None,
) -> str:
    """Click the mouse at bounded screen coordinates (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to perform mouse clicks."
    btn = (button or "left").strip().lower()
    if btn not in {"left", "right", "middle"}:
        return f"Error: Unsupported mouse button '{button}'. Use left, right, or middle."
    try:
        xi, yi = int(x), int(y)
    except (TypeError, ValueError):
        return "Error: x and y coordinates must be integers."

    min_x, min_y, max_x, max_y = _get_screen_bounds()
    if xi < min_x or xi > max_x or yi < min_y or yi > max_y:
        return (
            f"Error: Coordinates ({xi}, {yi}) are outside screen bounds "
            f"[{min_x}..{max_x}, {min_y}..{max_y}]."
        )
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.click(x=xi, y=yi, clicks=2 if double else 1, button=btn)
        return f"Clicked {btn} mouse button at ({xi}, {yi}) [double={bool(double)}]."
    except Exception as exc:
        return f"ERROR performing mouse click: {exc}"


def type_text_impl(text: str, press_enter: bool = False, read_only: bool | None = None) -> str:
    """Type bounded text into the active foreground window (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to type text on the desktop."
    if not text:
        return "Error: Cannot type empty text."
    capped = text[:2000]
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.write(capped, interval=0.01)
        if press_enter:
            pyautogui.press("enter")
        return f"Typed {len(capped)} characters into active window (press_enter={bool(press_enter)})."
    except Exception as exc:
        return f"ERROR typing text: {exc}"


def hotkey_impl(keys: str, read_only: bool | None = None) -> str:
    """Execute a safe keyboard shortcut combo such as 'ctrl+c' or 'alt+tab' (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to send keyboard shortcuts."
    raw_parts = [p.strip().lower() for p in re.split(r"[\s+]+", keys or "") if p.strip()]
    if not raw_parts or len(raw_parts) > 4:
        return "Error: Provide 1 to 4 keys separated by '+' (e.g. 'ctrl+c')."
    if not all(re.match(r"^[a-z0-9_-]{1,20}$", p) for p in raw_parts):
        return f"Error: Invalid key token in shortcut '{keys}'."

    canon_parts = [_KEY_ALIASES.get(p, p) for p in raw_parts]
    if frozenset(canon_parts) in _BLOCKED_HOTKEY_SETS:
        return f"SECURITY BLOCKED: Shortcut '{keys}' is restricted by AURA safety policy."

    normalized = "+".join(canon_parts)
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.hotkey(*canon_parts)
        return f"Executed keyboard shortcut: {normalized}."
    except Exception as exc:
        return f"ERROR executing shortcut '{normalized}': {exc}"


def desktop_scroll_impl(direction: str = "down", clicks: int = 5, read_only: bool | None = None) -> str:
    """Scroll the mouse wheel up or down in the active window (RW required)."""
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to scroll the active window."
    dir_low = (direction or "down").strip().lower()
    if dir_low not in {"up", "down"}:
        return f"Error: Unsupported scroll direction '{direction}'. Use 'up' or 'down'."
    try:
        steps = max(1, min(int(5 if clicks is None else clicks), 50))
    except (TypeError, ValueError):
        return "Error: clicks must be an integer."
    amount = -steps * 120 if dir_low == "down" else steps * 120
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.scroll(amount)
        return f"Scrolled {dir_low} by {steps} steps."
    except Exception as exc:
        return f"ERROR scrolling desktop: {exc}"


# ============================================================
# MCP TOOL EXPORTS
# ============================================================

@mcp.tool()
def desktop_list_monitors() -> str:
    """List connected Windows monitors and their usable work areas."""
    mons = get_monitors()
    return json.dumps(mons, indent=2) if mons else "No monitors detected."


@mcp.tool()
def desktop_list_windows(max_items: int = 40) -> str:
    """List currently visible top-level windows with titles, process names, and bounds."""
    limit = max(1, min(int(max_items), 80))
    wins = get_windows(max_items=limit)
    return json.dumps(wins, indent=2) if wins else "No visible top-level windows found."


@mcp.tool()
def desktop_place_window(target: str, position: str = "left", monitor: int = 1) -> str:
    """Move/snap/focus/minimize/maximize a matching open window (requires RW mode)."""
    return place_window_impl(target, position=position, monitor=monitor)


@mcp.tool()
def desktop_launch_app(app_name: str, args: str = "") -> str:
    """Launch an installed application by name or Start Menu shortcut (requires RW mode)."""
    return launch_app_impl(app_name, args=args)


@mcp.tool()
def desktop_find_files(query: str, max_results: int = 20, location: str = "") -> str:
    """Search files and folders on the local PC (Desktop, Documents, Downloads, or Windows Search Index)."""
    return find_files_impl(query, max_results=max_results, location=location)


@mcp.tool()
def desktop_open_path(target: str, in_explorer: bool = False, location: str = "") -> str:
    """Directly open a local file or folder by path or name (never use Explorer search or mouse; set in_explorer=True to highlight in File Explorer; requires RW mode)."""
    return open_path_impl(target, in_explorer=in_explorer, location=location)


@mcp.tool()
def desktop_read_screen_ocr(target: str = "") -> str:
    """Read visible text from the primary screen or a specific window title using built-in Windows OCR."""
    return read_screen_ocr_impl(target=target)


@mcp.tool()
def desktop_clipboard(action: str = "read", text: str = "") -> str:
    """Read the Windows clipboard (RO allowed) or copy text to clipboard (RW required)."""
    return clipboard_impl(action=action, text=text)


@mcp.tool()
def desktop_trash_file(path: str) -> str:
    """Move a file or directory to the Windows Recycle Bin (undoable; RW required)."""
    return trash_file_impl(path)


@mcp.tool()
def desktop_ui_see(target: str, max_items: int = 60) -> str:
    """List buttons, tabs, menus, and fields inside an open window via Windows UI Automation."""
    return ui_see_impl(target, max_items=max_items)


@mcp.tool()
def desktop_ui_click(target: str, element_name: str) -> str:
    """Click/invoke a named button, tab, or menu item inside an open window (RW required)."""
    return ui_click_impl(target, element_name=element_name)


@mcp.tool()
def desktop_mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click the mouse at bounded screen pixel coordinates (RW required)."""
    return mouse_click_impl(x=x, y=y, button=button, double=double)


@mcp.tool()
def desktop_type_text(text: str, press_enter: bool = False) -> str:
    """Type text into the currently focused desktop window (RW required)."""
    return type_text_impl(text=text, press_enter=press_enter)


@mcp.tool()
def desktop_hotkey(keys: str) -> str:
    """Send a safe keyboard shortcut like 'ctrl+c' or 'alt+tab' (dangerous OS combos blocked; RW required)."""
    return hotkey_impl(keys=keys)


@mcp.tool()
def desktop_scroll(direction: str = "down", clicks: int = 5) -> str:
    """Scroll the mouse wheel up or down inside the active desktop window (RW required)."""
    return desktop_scroll_impl(direction=direction, clicks=clicks)


@mcp.tool()
def desktop_close_window(target: str) -> str:
    """Gracefully close matching open window(s) via WM_CLOSE (RW required)."""
    return close_window_impl(target)


@mcp.tool()
def desktop_media_control(action: str = "playpause") -> str:
    """Control system media playback/volume (playpause, next, previous, stop, mute, volumeup, volumedown; RW required)."""
    return media_control_impl(action=action)


@mcp.tool()
def desktop_todo_note(action: str = "read", text: str = "") -> str:
    """Read, add, complete, clear, or open items in the user's persistent Jarvis_Notes.md To-Do list."""
    return todo_note_impl(action=action, text=text)


@mcp.tool()
def desktop_ui_set(target: str, element_name: str, text: str) -> str:
    """Fill a named text field inside an open window via UI Automation ValuePattern (RW required)."""
    return ui_set_impl(target, element_name=element_name, text=text)


def watch_window_impl(
    target: str,
    condition: str = "appears",
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.25,
) -> str:
    """Bounded read-only poll waiting until a matching window appears or disappears."""
    clean = (target or "").strip()
    if not clean:
        return "ERROR: target window name cannot be empty."
    cond = (condition or "appears").strip().lower()
    if cond not in {"appears", "disappears", "exists", "missing"}:
        return "ERROR: condition must be 'appears' or 'disappears'."
    want_present = cond in {"appears", "exists"}
    max_wait = max(0.0, min(15.0, float(timeout_seconds)))
    step_s = max(0.05, min(2.0, float(poll_interval)))

    deadline = time.monotonic() + max_wait
    while True:
        matches = _find_windows(clean)
        if want_present and matches:
            top = matches[0]
            return f"MATCHED: Window '{top.get('title', clean)}' ({top.get('process', '')}) is present."
        if not want_present and not matches:
            return f"MATCHED: Window '{clean}' is no longer open."
        if time.monotonic() >= deadline:
            state = "still open" if matches else "not found"
            return f"TIMEOUT after {max_wait:.1f}s: window '{clean}' is {state}."
        time.sleep(step_s)


@mcp.tool()
def desktop_watch_window(
    target: str,
    condition: str = "appears",
    timeout_seconds: float = 5.0,
) -> str:
    """Wait (up to 15s, read-only safe) until a matching window appears or disappears."""
    return watch_window_impl(target, condition=condition, timeout_seconds=timeout_seconds)


def system_info_impl() -> str:
    """Collect a safe read-only system health snapshot (OS, CPU, RAM, disk, battery, displays, windows)."""
    os_line = f"{platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}"
    cpu_count = os.cpu_count() or 1

    ram_line = "unavailable"
    battery_line = "unavailable"
    if sys.platform == "win32":
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("sullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            mem = _MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                total_gb = mem.ullTotalPhys / (1024 ** 3)
                avail_gb = mem.ullAvailPhys / (1024 ** 3)
                used_gb = max(0.0, total_gb - avail_gb)
                ram_line = f"{used_gb:.1f} / {total_gb:.1f} GB ({int(mem.dwMemoryLoad)}% used)"
        except Exception:
            pass

        try:
            class _SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", wintypes.DWORD),
                    ("BatteryFullLifeTime", wintypes.DWORD),
                ]

            pwr = _SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
                pct = int(pwr.BatteryLifePercent) & 0xFF
                ac = "AC connected" if pwr.ACLineStatus == 1 else "On battery"
                if pct <= 100:
                    battery_line = f"{pct}% ({ac})"
                else:
                    battery_line = ac
        except Exception:
            pass

    disk_line = "unavailable"
    try:
        du = shutil.disk_usage(Path.cwd())
        total_d = du.total / (1024 ** 3)
        free_d = du.free / (1024 ** 3)
        used_pct = ((du.total - du.free) / du.total * 100.0) if du.total else 0.0
        disk_line = f"{free_d:.1f} GB free / {total_d:.1f} GB total ({used_pct:.0f}% used)"
    except OSError:
        pass

    monitors = get_monitors()
    mon_count = len(monitors) if monitors else 1
    windows = get_windows(max_items=8)
    win_labels = [f"{w.get('process') or 'app'}: {w.get('title', '')[:45]}" for w in windows[:6]]
    win_line = ", ".join(win_labels) if win_labels else "(none visible)"

    return "\n".join([
        "System Status:",
        f"- OS: {os_line}",
        f"- CPU Cores: {cpu_count}",
        f"- RAM: {ram_line}",
        f"- Disk ({Path.cwd().drive or '/'}): {disk_line}",
        f"- Battery: {battery_line}",
        f"- Monitors: {mon_count}",
        f"- Open Windows: {win_line}",
    ])


@mcp.tool()
def desktop_system_info() -> str:
    """Get a safe read-only PC health snapshot (OS, CPU, RAM, disk, battery, monitors, open windows)."""
    return system_info_impl()


# ============================================================
# POINT-AND-TELL CURSOR INSPECTION (UIA + HIGH-DPI ZOOMED OCR)
# ============================================================

def _query_uia_at_point(x: int, y: int) -> dict[str, Any]:
    """Query Windows UI Automation for the control at screen pixel coordinates (x, y)."""
    if sys.platform != "win32":
        return {}
    ps = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AuraCursorDpi {{
    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}}
"@ -ErrorAction SilentlyContinue
try {{ [void][AuraCursorDpi]::SetProcessDpiAwarenessContext([IntPtr](-4)) }} catch {{}}
$pt = New-Object System.Windows.Point({int(x)}, {int(y)})
$el = [System.Windows.Automation.AutomationElement]::FromPoint($pt)
if ($null -eq $el) {{ Write-Output '{{}}'; exit 0 }}
$cur = $el.Current
$rect = $cur.BoundingRectangle
$hasRect = -not ([double]::IsInfinity($rect.Left) -or [double]::IsInfinity($rect.Top) -or [double]::IsInfinity($rect.Width) -or [double]::IsInfinity($rect.Height) -or [double]::IsNaN($rect.Left))
$val = ''
$pat = $null
if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)) {{
    try {{ $val = [string]$pat.Current.Value }} catch {{}}
}} elseif ($el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$pat)) {{
    try {{ $val = "ToggleState=" + $pat.Current.ToggleState.ToString() }} catch {{}}
}} elseif ($el.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$pat)) {{
    try {{ $val = [string]$pat.Current.Value }} catch {{}}
}} elseif ($el.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) {{
    try {{ if ($pat.Current.IsSelected) {{ $val = "Selected=True" }} }} catch {{}}
}}
if ([string]::IsNullOrWhiteSpace($val) -and $el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pat)) {{
    try {{
        $rng = $pat.RangeFromPoint($pt)
        if ($null -ne $rng) {{
            $rng.ExpandToEnclosingUnit([System.Windows.Automation.TextUnit]::Line)
            $val = [string]$rng.GetText(300)
        }}
    }} catch {{}}
}}
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$parent = $null
try {{ $parent = $walker.GetParent($el) }} catch {{}}
$ancType = ''
$ancName = ''
$probe = $parent
for ($step = 0; $step -lt 4 -and $null -ne $probe; $step++) {{
    try {{
        if ($probe -eq [System.Windows.Automation.AutomationElement]::RootElement) {{ break }}
        $pCur = $probe.Current
        $pName = [string]$pCur.Name
        if (-not [string]::IsNullOrWhiteSpace($pName)) {{
            $ancType = ($pCur.ControlType.ProgrammaticName -replace '^ControlType\\.', '')
            $ancName = $pName
            break
        }}
        $probe = $walker.GetParent($probe)
    }} catch {{ break }}
}}
$top = $el
for ($d = 0; $d -lt 40 -and $null -ne $top; $d++) {{
    $p = $null
    try {{ $p = $walker.GetParent($top) }} catch {{ break }}
    if ($null -eq $p -or $p -eq [System.Windows.Automation.AutomationElement]::RootElement) {{ break }}
    $top = $p
}}
$obj = [ordered]@{{
    control_type = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', '')
    name = [string]$cur.Name
    automation_id = [string]$cur.AutomationId
    class_name = [string]$cur.ClassName
    help_text = [string]$cur.HelpText
    value = $val
    is_enabled = [bool]$cur.IsEnabled
    rect = if ($hasRect) {{ @{{ left = [int]$rect.Left; top = [int]$rect.Top; width = [int]$rect.Width; height = [int]$rect.Height }} }} else {{ $null }}
    parent_type = if ($parent) {{ ($parent.Current.ControlType.ProgrammaticName -replace '^ControlType\\.', '') }} else {{ '' }}
    parent_name = if ($parent) {{ [string]$parent.Current.Name }} else {{ '' }}
    ancestor_type = $ancType
    ancestor_name = $ancName
    top_title = if ($top) {{ [string]$top.Current.Name }} else {{ '' }}
    top_pid = if ($top) {{ [int]$top.Current.ProcessId }} else {{ 0 }}
    top_hwnd = if ($top) {{ [int64]$top.Current.NativeWindowHandle }} else {{ 0 }}
}}
$obj | ConvertTo-Json -Compress
"""
    rc, out, _ = _run_powershell(ps, timeout=8)
    if rc != 0 or not out:
        return {}
    try:
        parsed = json.loads(out.splitlines()[-1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _resolve_window_at_point(x: int, y: int, uia_info: dict[str, Any]) -> dict[str, Any]:
    """Resolve the non-cloaked, non-overlay enclosing window at screen coordinates (x, y)."""
    uia_hwnd = int(uia_info.get("top_hwnd") or 0)
    uia_pid = int(uia_info.get("top_pid") or 0)
    uia_title = " ".join(str(uia_info.get("top_title") or "").split())
    uia_proc = _process_name_for_pid(uia_pid) if uia_pid > 0 else ""

    user32 = _get_user32()
    is_noisy_uia = (
        uia_title.lower() in _NOISY_WINDOW_TITLES
        or uia_proc in _NOISY_WINDOW_PROCESSES
        or (uia_hwnd > 0 and _is_window_cloaked(uia_hwnd))
        or (user32 is not None and uia_hwnd > 0 and _is_clickthrough_overlay(user32, wintypes.HWND(uia_hwnd)))
    )

    visible_windows = get_windows(max_items=40)
    # Match exact HWND first when valid & non-noisy
    if uia_hwnd and not is_noisy_uia:
        for win in visible_windows:
            if int(win.get("hwnd") or 0) == uia_hwnd:
                return win

    # Otherwise check Z-order visible windows containing (x, y)
    for win in visible_windows:
        if win.get("minimized"):
            continue
        r = win.get("rect") or {}
        if r.get("left", 0) <= x < r.get("right", 0) and r.get("top", 0) <= y < r.get("bottom", 0):
            return win

    # Win32 fallback when UIA returned a real window not in top-40 list (e.g., taskbar/desktop)
    if uia_hwnd and not is_noisy_uia:
        win_rect = None
        if user32 is not None:
            rect = wintypes.RECT()
            if user32.GetWindowRect(wintypes.HWND(uia_hwnd), ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    win_rect = {
                        "left": rect.left, "top": rect.top,
                        "right": rect.right, "bottom": rect.bottom,
                        "width": w, "height": h,
                    }
        return {
            "hwnd": uia_hwnd,
            "title": uia_title,
            "process": uia_proc,
            "rect": win_rect,
        }

    # Final Win32 WindowFromPoint fallback
    if user32 is not None and hasattr(user32, "WindowFromPoint"):
        try:
            hwnd_pt = user32.WindowFromPoint(wintypes.POINT(x, y))
            root_hwnd = int(user32.GetAncestor(hwnd_pt, 2) or hwnd_pt or 0) if hasattr(user32, "GetAncestor") else int(hwnd_pt or 0)
            if root_hwnd and not _is_window_cloaked(root_hwnd) and not _is_clickthrough_overlay(user32, wintypes.HWND(root_hwnd)):
                length = user32.GetWindowTextLengthW(wintypes.HWND(root_hwnd))
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(wintypes.HWND(root_hwnd), buf, length + 1)
                    title = buf.value.strip()
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(wintypes.HWND(root_hwnd), ctypes.byref(pid))
                proc_name = _process_name_for_pid(int(pid.value))
                if title.lower() not in _NOISY_WINDOW_TITLES and proc_name not in _NOISY_WINDOW_PROCESSES:
                    rect = wintypes.RECT()
                    win_rect = None
                    if user32.GetWindowRect(wintypes.HWND(root_hwnd), ctypes.byref(rect)):
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w > 0 and h > 0:
                            win_rect = {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom, "width": w, "height": h}
                    return {"hwnd": root_hwnd, "title": title, "process": proc_name, "rect": win_rect}
        except Exception:
            pass

    return {"hwnd": 0, "title": "", "process": uia_proc if not is_noisy_uia else "", "rect": None}


def _ocr_region_image(bbox: tuple[int, int, int, int], upscale: int = 1) -> tuple[bool, str]:
    """Capture screen bbox, optionally upscale for high-DPI OCR clarity, and run _ocr_image_file."""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return False, "Pillow (PIL.ImageGrab) is required for screen capture."

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()

    img = None
    save_img = None
    try:
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except OSError as exc:
            return False, f"Screen capture unavailable ({exc})."

        save_img = img
        if upscale > 1 and hasattr(img, "resize") and img.width > 0 and img.height > 0:
            if max(img.width * upscale, img.height * upscale) <= 1920:
                resampling = getattr(Image, "Resampling", Image)
                save_img = img.resize((img.width * upscale, img.height * upscale), resampling.LANCZOS)

        save_img.save(tmp_path)
        return _ocr_image_file(tmp_path)
    finally:
        for handle in (save_img, img):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def inspect_cursor_impl(
    x: int | None = None,
    y: int | None = None,
    radius: int = 180,
    include_window_ocr: bool = True,
    as_json: bool = False,
) -> str:
    """Inspect UI Automation element, enclosing window, and zoomed OCR text at the cursor (RO & RW safe)."""
    if sys.platform != "win32":
        return "ERROR: Cursor inspection is supported only on Windows."

    if (x is None) != (y is None):
        return "ERROR: Provide both x and y coordinates, or omit both to inspect the live cursor."

    if x is None and y is None:
        user32 = _get_user32()
        get_cursor = getattr(user32, "GetCursorPos", None) if user32 is not None else None
        if get_cursor is None:
            return "ERROR: Windows GetCursorPos API is unavailable."
        pt = wintypes.POINT()
        if not get_cursor(ctypes.byref(pt)):
            return "ERROR: Could not read live Windows cursor coordinates."
        xi, yi = int(pt.x), int(pt.y)
    else:
        if isinstance(x, bool) or isinstance(y, bool):
            return "ERROR: x and y coordinates must be integers."
        try:
            xi, yi = int(x), int(y)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "ERROR: x and y coordinates must be integers."
        min_x, min_y, max_x, max_y = _get_screen_bounds()
        if xi < min_x or xi > max_x or yi < min_y or yi > max_y:
            return (
                f"ERROR: Coordinates ({xi}, {yi}) are outside screen bounds "
                f"[{min_x}..{max_x}, {min_y}..{max_y}]."
            )

    try:
        rad = max(60, min(int(180 if radius is None else radius), 480))
    except (TypeError, ValueError):
        rad = 180
    uia = _query_uia_at_point(xi, yi)
    win = _resolve_window_at_point(xi, yi, uia)

    crop_bbox = _clamp_bbox_to_screen((xi - rad, yi - rad, xi + rad, yi + rad))
    if crop_bbox is None:
        min_x, min_y, max_x, max_y = _get_screen_bounds()
        crop_bbox = (min_x, min_y, max(min_x + 1, min(max_x, min_x + 2 * rad)), max(min_y + 1, min(max_y, min_y + 2 * rad)))

    crop_ok, crop_ocr = _ocr_region_image(crop_bbox, upscale=2)
    win_ocr_ok, win_ocr = False, ""
    win_rect = win.get("rect")
    if include_window_ocr and crop_ok and isinstance(win_rect, dict):
        win_bbox = _clamp_bbox_to_screen((
            int(win_rect["left"]),
            int(win_rect["top"]),
            int(win_rect["right"]),
            int(win_rect["bottom"]),
        ))
        if win_bbox is not None:
            win_area = (win_bbox[2] - win_bbox[0]) * (win_bbox[3] - win_bbox[1])
            crop_area = (crop_bbox[2] - crop_bbox[0]) * (crop_bbox[3] - crop_bbox[1])
            if win_area > crop_area * 1.35:
                win_ocr_ok, win_ocr = _ocr_region_image(win_bbox, upscale=1)

    def _clean(val: Any, limit: int = 300) -> str:
        txt = " ".join(str(val or "").split()).strip()
        return txt[:limit] + ("..." if len(txt) > limit else "")

    ctrl_type = _clean(uia.get("control_type"), 60) or "Unknown"
    ctrl_name = _clean(uia.get("name"), 240)
    auto_id = _clean(uia.get("automation_id"), 120)
    cls_name = _clean(uia.get("class_name"), 120)
    ctrl_val = _clean(uia.get("value"), 260)
    help_text = _clean(uia.get("help_text"), 240)
    parent_type = _clean(uia.get("parent_type"), 60)
    parent_name = _clean(uia.get("parent_name"), 180)
    anc_type = _clean(uia.get("ancestor_type"), 60)
    anc_name = _clean(uia.get("ancestor_name"), 180)
    win_title = _clean(win.get("title"), 180)
    win_proc = _clean(win.get("process"), 80)
    win_hwnd = int(win.get("hwnd") or 0)
    crop_ocr_clean = (crop_ocr or "").strip()
    if len(crop_ocr_clean) > 850:
        crop_ocr_clean = crop_ocr_clean[:850] + "\n... [truncated]"
    win_ocr_clean = (win_ocr or "").strip()
    if len(win_ocr_clean) > 1100:
        win_ocr_clean = win_ocr_clean[:1100] + "\n... [truncated]"

    payload = {
        "cursor": {"x": xi, "y": yi},
        "window": {
            "title": win_title,
            "process": win_proc,
            "hwnd": win_hwnd,
            "rect": win_rect,
        },
        "element": {
            "control_type": ctrl_type,
            "name": ctrl_name,
            "automation_id": auto_id,
            "class_name": cls_name,
            "value": ctrl_val,
            "help_text": help_text,
            "is_enabled": bool(uia.get("is_enabled", True)) if uia else None,
            "rect": uia.get("rect"),
            "parent_type": parent_type,
            "parent_name": parent_name,
            "ancestor_type": anc_type,
            "ancestor_name": anc_name,
        },
        "crop_bbox": {
            "left": crop_bbox[0],
            "top": crop_bbox[1],
            "right": crop_bbox[2],
            "bottom": crop_bbox[3],
        },
        "cursor_ocr": crop_ocr_clean if crop_ok else f"(unavailable: {crop_ocr})",
        "window_ocr": win_ocr_clean if win_ocr_ok else "",
    }
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    lines = [f"Cursor Inspection at ({xi}, {yi}):"]
    win_desc = f'"{win_title}"' if win_title else "(unnamed window)"
    win_meta = []
    if win_proc:
        win_meta.append(f"process: {win_proc}")
    if win_hwnd:
        win_meta.append(f"hwnd: {win_hwnd}")
    if isinstance(win_rect, dict):
        win_meta.append(f"rect: {win_rect.get('left')},{win_rect.get('top')} {win_rect.get('width')}x{win_rect.get('height')}")
    lines.append(f"- Window: {win_desc}" + (f" ({', '.join(win_meta)})" if win_meta else ""))

    el_label = f'"{ctrl_name}"' if ctrl_name else "(unnamed)"
    el_meta = []
    if auto_id:
        el_meta.append(f"AutomationId: {auto_id}")
    if cls_name:
        el_meta.append(f"Class: {cls_name}")
    el_rect = uia.get("rect")
    if isinstance(el_rect, dict):
        el_meta.append(f"Rect: {el_rect.get('left')},{el_rect.get('top')} {el_rect.get('width')}x{el_rect.get('height')}")
    if "is_enabled" in uia:
        el_meta.append(f"Enabled: {bool(uia['is_enabled'])}")
    lines.append(f"- Element: [{ctrl_type}] {el_label}" + (f" ({', '.join(el_meta)})" if el_meta else ""))

    if ctrl_val:
        lines.append(f"- Value/Text: {ctrl_val}")
    if help_text:
        lines.append(f"- HelpText: {help_text}")
    if parent_type or parent_name:
        lines.append(f'- Parent: [{parent_type or "Element"}] "{parent_name}"')
    if not ctrl_name and anc_name and (anc_name != parent_name or anc_type != parent_type):
        lines.append(f'- Interactive Ancestor: [{anc_type or "Element"}] "{anc_name}"')

    crop_hdr = f"- Cursor OCR ({crop_bbox[0]},{crop_bbox[1]}..{crop_bbox[2]},{crop_bbox[3]}):"
    if crop_ok:
        lines.append(f"{crop_hdr}\n{crop_ocr_clean or '(no readable text detected)'}")
    else:
        lines.append(f"{crop_hdr} (unavailable: {crop_ocr})")

    if win_ocr_ok and win_ocr_clean:
        lines.append(f"- Window OCR Context:\n{win_ocr_clean}")

    return "\n".join(lines)


@mcp.tool()
def desktop_inspect_cursor(
    x: int | None = None,
    y: int | None = None,
    radius: int = 180,
    include_window_ocr: bool = True,
    as_json: bool = False,
) -> str:
    """Inspect what the mouse cursor is pointing at (UIA control, enclosing window, and zoomed WinRT OCR) in one call (RO & RW safe)."""
    return inspect_cursor_impl(
        x=x,
        y=y,
        radius=radius,
        include_window_ocr=include_window_ocr,
        as_json=as_json,
    )


@mcp.tool()
def desktop_inspect_path(
    target: str,
    location: str = "",
    start_line: int = 1,
    max_chars: int = 2600,
) -> str:
    """Safely read/inspect a non-sensitive local file or folder (Desktop, Documents, Downloads, or explicit path; works in RO & RW)."""
    return inspect_path_impl(
        target=target,
        location=location,
        start_line=start_line,
        max_chars=max_chars,
    )


if __name__ == "__main__":
    mcp.run()
