"""
AURA V4 · ULTRON Windows Dynamic Island Floating Pill.

Dark vibranium steel with a cold arc-blue core, Per-Monitor v2 High-DPI crisp
rendering, 3x-supersampled PIL metallic bezels, top-flush hardware-docked
silhouette (`y = 0`), buttery-smooth spring morphing, and three states:
- 01 · Compact island (310x40, y=0): Ultron eye core, live arc-blue filament wave,
  persona voice indicator, mic toggle switch, active Stop ring, and power control
- 02 · Shuttered standby (220x32, y=0): Ultra-minimal closed eye + wake toggle
- 03 · Sleek opened island (960x520, y=0): Brow crest with vents, capsule header
  with model menu & @agent pills, 3-column Ask Aura HUD (Chat Well + Omni Composer
  with live mid-run Push/Steer, Executive Summary + Gmail, Agenda + interactive
  Todos progress bar), plus Agents pipeline & Hub & Focus views.
"""

from __future__ import annotations

import argparse
import calendar
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import inspect
import math
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
from typing import Any, Callable
import webbrowser

import httpx

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # pragma: no cover - optional fallback if Pillow absent
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

from .core.mic_listener import MicListener, is_stop_command
from .core.scope import SPECIALIST_AGENTS, normalize_agent_role


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
WM_HOTKEY = 0x0312
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x11
_OVERLAY_MUTEX_NAME = "Local\\AuraV4DynamicIslandMutex"
_OVERLAY_WAKE_EVENT_NAME = "Local\\AuraV4OverlayWakeEvent"
_ERROR_ALREADY_EXISTS = 183

_PERSONA_CYCLE = ("off", "jarvis", "ultron", "megatron")
_PERSONA_LABELS = {
    "off": "Voice: Off",
    "jarvis": "🎙 Jarvis",
    "ultron": "🎙 Ultron",
    "megatron": "🎙 Megatron",
}
_INLINE_METRICS_RE = re.compile(r"\[{1,2}Metrics\s*(?:\(([^)]+)\))?:[^\]]*\]{1,2}\s*", re.IGNORECASE)
_PLACEHOLDER = "Ask Aura anything, or type / for commands and @ for agents"
_PUSH_PLACEHOLDER = "Push steering instruction while Aura works (keeps output)..."
_TOOLS_FILTER_HINT = "Filter tools..."

_UI_FONT = "Segoe UI Variable Text" if sys.platform == "win32" else "Segoe UI"
_TECH_FONT = "Bahnschrift" if sys.platform == "win32" else "Segoe UI"
_MONO_FONT = "Cascadia Code" if sys.platform == "win32" else "Consolas"

_PALETTE = {
    "chroma": "#010203",
    "bg": "#181C23",
    "titanium": "#3B424F",
    "gunmetal": "#151920",
    "panel_mid": "#222731",
    "card": "#282E3A",
    "card_hi": "#313948",
    "well": "#111419",
    "well_lo": "#161A21",
    "hdr_bar": "#13161C",
    "code_bg": "#0E1116",
    "chip": "#2A303C",
    "chip_hover": "#3B4454",
    "border": "#576477",
    "border_hi": "#7D8DA3",
    "text": "#E8EDF4",
    "steel": "#C9D3E0",
    "muted": "#98A4B5",
    "accent": "#5CC3F2",
    "blue": "#5CC3F2",
    "arc_border": "#6FCBF4",
    "arc_text": "#B4E1F8",
    "deep_blue": "#1F6FA8",
    "cta_bg": "#237BB5",
    "cta_hover": "#2E8FD0",
    "amber": "#FBBF24",
    "pill_active_bg": "#1B3B56",
    "pill_active_fg": "#B4E1F8",
    "danger_bg": "#3A1D28",
    "danger_hover": "#552334",
    "danger_fg": "#FDA4AF",
}

_PIPELINE_NODES: tuple[tuple[str, str], ...] = (
    ("researcher", "Research"),
    ("planner", "Plan"),
    ("coder", "Code"),
    ("tester", "Test"),
    ("reviewer", "Review"),
)

_QUICK_AGENTS: tuple[tuple[str, str], ...] = (
    ("@auto", "auto"),
    ("@planner", "planner"),
    ("@coder", "coder"),
    ("@researcher", "researcher"),
    ("@gmail", "jarvis"),
    ("@calendar", "jarvis"),
)

_DEFAULT_MODELS: tuple[tuple[str, str, str], ...] = (
    ("qwen3.5:4b", "qwen3.5:4b · Ollama", "qwen3.5:4b"),
    ("qwen2.5-coder:7b", "qwen2.5-coder:7b · Ollama", "qwen2.5-coder:7b"),
)


def _format_cloud_model_label(spec: str) -> str:
    """Return a compact human-readable label for a cloud:* model spec."""
    raw = str(spec or "").strip()
    if raw == "cloud:auto":
        return "Cloud · Auto"
    if raw.startswith("cloud:custom/"):
        sub = raw[len("cloud:custom/"):].strip() or "auto"
        return "FreeLLMAPI · Auto" if sub.lower() == "auto" else f"FreeLLMAPI · {sub}"
    if raw.startswith("cloud:gemini/"):
        sub = raw[len("cloud:gemini/"):].strip()
        if sub == "gemini-3.8-flash":
            return "Gemini 3.8 · API"
        return f"Gemini · {sub}"
    if raw.startswith("cloud:"):
        body = raw[6:]
        if "/" in body:
            prov, mdl = body.split("/", 1)
            return f"{prov.title()} · {mdl}"
        return f"Cloud · {body}"
    return raw


def enable_high_dpi_awareness() -> bool:
    """Enable Windows Per-Monitor v2 DPI awareness before Tk initializes so UI text stays razor-sharp."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if fn is not None and fn(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        if shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


def _sync_tk_dpi_scaling(root: tk.Tk) -> float:
    """Sync Tk's logical-to-physical pixel scaling factor to actual monitor DPI for ClearType text."""
    try:
        current = float(root.tk.call("tk", "scaling"))
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            get_dpi_win = getattr(user32, "GetDpiForWindow", None)
            dpi = int(get_dpi_win(root.winfo_id())) if get_dpi_win else 0
            if dpi <= 0:
                get_dpi_sys = getattr(user32, "GetDpiForSystem", None)
                dpi = int(get_dpi_sys()) if get_dpi_sys else 96
            if 72 <= dpi <= 384:
                target = dpi / 72.0
                if abs(current - target) > 0.08:
                    root.tk.call("tk", "scaling", target)
                    return target
        return current
    except Exception:
        return 1.333


def _hex_rgb(hex_color: str) -> tuple[int, int, int]:
    clean = (hex_color or "#21252C").lstrip("#")
    if len(clean) != 6:
        return (33, 37, 44)
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def _lerp_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    u = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * u),
        int(c1[1] + (c2[1] - c1[1]) * u),
        int(c1[2] + (c2[2] - c1[2]) * u),
    )


def _render_ultron_eye_photo(size: int, shuttered: bool = False, bg_hex: str = "#171A20") -> Any:
    """Render a 3x-supersampled brushed-steel Ultron eye with cold arc-blue core."""
    if Image is None or ImageDraw is None or ImageTk is None:
        return None
    scale = 3
    s = max(24, size * scale)
    img = Image.new("RGBA", (s, s), _hex_rgb(bg_hex) + (255,))
    draw = ImageDraw.Draw(img, "RGBA")

    # Conic metallic steel bezel ring rendered from fine wedges
    cx = cy = s / 2.0
    outer_r = s * 0.48
    stops = [
        _hex_rgb("#4A5160"),
        _hex_rgb("#C9D3E0"),
        _hex_rgb("#6B7587"),
        _hex_rgb("#E6EDF5"),
        _hex_rgb("#4A5160"),
        _hex_rgb("#98A5B8"),
        _hex_rgb("#4A5160"),
    ]
    if shuttered:
        stops = [_hex_rgb("#343A46"), _hex_rgb("#6B7587"), _hex_rgb("#434A58"), _hex_rgb("#8693A6"), _hex_rgb("#343A46")]

    steps = 72
    box = [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r]
    for i in range(steps):
        frac = i / steps
        seg_pos = frac * (len(stops) - 1)
        idx = min(len(stops) - 2, int(seg_pos))
        col = _lerp_rgb(stops[idx], stops[idx + 1], seg_pos - idx)
        a0 = 200 + frac * 360.0
        a1 = 200 + ((i + 1) / steps) * 360.0 + 1.5
        draw.pieslice(box, start=a0, end=a1, fill=col + (255,))

    # Inner dark socket
    sock_pad = max(3, int(size * (0.10 if size <= 32 else 0.095) * scale))
    sock_box = [sock_pad, sock_pad, s - sock_pad, s - sock_pad]
    draw.ellipse(sock_box, fill=(19, 22, 27, 255))

    if shuttered:
        slit_h = max(scale * 2, int(s * 0.07))
        y0 = int(cy - slit_h / 2)
        y1 = int(cy + slit_h / 2)
        left_x = int(s * 0.24)
        right_x = int(s * 0.76)
        draw.rounded_rectangle([left_x, y0, right_x, y1], radius=slit_h, fill=(143, 160, 181, 235))
    else:
        iris_pad = max(sock_pad + scale * 2, int(size * 0.20 * scale))
        ir0, ir1 = iris_pad, s - iris_pad
        draw.ellipse([ir0, ir0, ir1, ir1], fill=(15, 58, 92, 255), outline=(92, 195, 242, 210), width=scale)
        mid_pad = int(s * 0.28)
        draw.ellipse([mid_pad, mid_pad, s - mid_pad, s - mid_pad], fill=(31, 111, 168, 220))
        core_pad = int(s * 0.36)
        draw.ellipse([core_pad, core_pad, s - core_pad, s - core_pad], fill=(92, 195, 242, 255))
        pin_pad = int(s * 0.42)
        draw.ellipse([pin_pad, pin_pad, s - pin_pad, s - pin_pad], fill=(234, 248, 255, 255))

    resized = img.resize((size, size), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(resized)


def _render_brow_crest_photo(width: int, expanded: bool = False, bg_hex: str = "#21252C") -> Any:
    """Render a 3x-supersampled brushed-steel Ultron brow crest with arc-blue core slit and vents."""
    if Image is None or ImageDraw is None or ImageTk is None:
        return None
    w = max(120, int(width))
    h = 12 if expanded else 9
    scale = 3
    sw, sh = w * scale, h * scale
    img = Image.new("RGBA", (sw, sh), _hex_rgb(bg_hex) + (255,))
    draw = ImageDraw.Draw(img, "RGBA")

    cw = (180 if expanded else 78) * scale
    top_l = (sw - cw) // 2
    top_r = top_l + cw
    bot_inset = (18 if expanded else 9) * scale
    crest_h = (10 if expanded else 7) * scale
    # Stepped metallic steel gradient trapezoid
    draw.polygon(
        [(top_l, 0), (top_r, 0), (top_r - bot_inset, crest_h), (top_l + bot_inset, crest_h)],
        fill=(146, 160, 180, 255),
        outline=(226, 234, 244, 255),
    )
    draw.polygon(
        [(top_l + scale * 2, scale), (top_r - scale * 2, scale), (top_r - bot_inset - scale * 2, crest_h - scale), (top_l + bot_inset + scale * 2, crest_h - scale)],
        fill=(102, 114, 134, 255),
    )

    # Arc-blue glowing slit
    slit_w = (88 if expanded else 30) * scale
    sl_l = (sw - slit_w) // 2
    sl_r = sl_l + slit_w
    slit_y = (3 if expanded else 2) * scale
    draw.rounded_rectangle(
        [sl_l - scale * 2, slit_y - scale, sl_r + scale * 2, slit_y + scale * 3],
        radius=scale * 2,
        fill=(31, 111, 168, 175),
    )
    draw.rounded_rectangle(
        [sl_l, slit_y, sl_r, slit_y + scale * 2],
        radius=scale,
        fill=(110, 208, 250, 255),
    )

    if expanded:
        mid = sw // 2
        for idx in range(4):
            lx = mid - (154 - idx * 10) * scale
            draw.polygon(
                [(lx, scale), (lx + 5 * scale, scale), (lx + 8 * scale, 10 * scale), (lx + 3 * scale, 10 * scale)],
                fill=(14, 18, 24, 255),
                outline=(104, 118, 138, 220),
            )
            rx = mid + (116 + idx * 10) * scale
            draw.polygon(
                [(rx + 3 * scale, scale), (rx + 8 * scale, scale), (rx + 5 * scale, 10 * scale), (rx, 10 * scale)],
                fill=(14, 18, 24, 255),
                outline=(104, 118, 138, 220),
            )

    resized = img.resize((w, h), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(resized)


def _render_switch_photo(active: bool, bg_hex: str = "#272C35") -> Any:
    """Render a 3x-supersampled metallic pill toggle switch with glowing arc-blue thumb."""
    if Image is None or ImageDraw is None or ImageTk is None:
        return None
    w, h = 42, 22
    scale = 3
    sw, sh = w * scale, h * scale
    img = Image.new("RGBA", (sw, sh), _hex_rgb(bg_hex) + (255,))
    draw = ImageDraw.Draw(img, "RGBA")
    pad = scale
    rim = (111, 203, 244, 255) if active else (96, 110, 128, 255)
    well = (22, 46, 68, 255) if active else (18, 22, 29, 255)
    draw.rounded_rectangle([pad, pad, sw - pad, sh - pad], radius=sh // 2, fill=well, outline=rim, width=scale)

    r_thumb = int(sh * 0.34)
    cy = sh // 2
    cx = (sw - sh // 2) if active else (sh // 2)
    if active:
        draw.ellipse([cx - r_thumb - 2 * scale, cy - r_thumb - 2 * scale, cx + r_thumb + 2 * scale, cy + r_thumb + 2 * scale], fill=(92, 195, 242, 85))
        draw.ellipse([cx - r_thumb, cy - r_thumb, cx + r_thumb, cy + r_thumb], fill=(92, 195, 242, 255), outline=(224, 246, 255, 255), width=scale)
    else:
        draw.ellipse([cx - r_thumb, cy - r_thumb, cx + r_thumb, cy + r_thumb], fill=(122, 130, 144, 255), outline=(180, 192, 208, 210), width=scale)
    return ImageTk.PhotoImage(img.resize((w, h), Image.Resampling.BILINEAR))


def _render_wave_photo(
    width: int,
    height: int,
    phase: float,
    active: bool,
    mic_enabled: bool,
    persona: str,
    bg_hex: str = "#171A20",
    amp_override: float | None = None,
) -> Any:
    """Render a 2x-supersampled multi-harmonic Ultron arc-blue voice oscilloscope wave."""
    if Image is None or ImageDraw is None or ImageTk is None:
        return None
    w = max(64, int(width))
    h = max(24, int(height))
    scale = 2
    sw, sh = w * scale, h * scale
    img = Image.new("RGBA", (sw, sh), _hex_rgb(bg_hex) + (255,))
    draw = ImageDraw.Draw(img, "RGBA")

    mid_y = sh / 2.0
    base_amp = amp_override if amp_override is not None else (14.5 if active else (11.5 if mic_enabled else 8.8))
    amp = float(base_amp) * scale
    if persona == "megatron":
        c_glow = (185, 28, 28, 120)
        c_mid = (239, 68, 68, 210)
        c_main = (248, 113, 113, 255)
        c_core = (254, 226, 226, 255)
    elif persona == "ultron":
        c_glow = (56, 189, 248, 125)
        c_mid = (96, 165, 250, 215)
        c_main = (147, 197, 253, 255)
        c_core = (239, 246, 255, 255)
    else:
        c_glow = (31, 111, 168, 125)
        c_mid = (46, 143, 208, 215)
        c_main = (92, 195, 242, 255)
        c_core = (230, 246, 255, 255)

    # Subtle horizon filament
    draw.line([(8 * scale, mid_y), (sw - 8 * scale, mid_y)], fill=(70, 95, 125, 90), width=1)

    pts_main: list[tuple[float, float]] = []
    pts_sec: list[tuple[float, float]] = []
    samples = 46
    for idx in range(samples + 1):
        u = idx / samples
        x = (6 * scale) + u * (sw - 12 * scale)
        # Wide sinusoidal envelope so peaks ripple visibly across the entire bar even in standby
        env = math.sin(u * math.pi) ** 1.05
        # Sharp multi-harmonic Ultron waveform with brisk high-frequency voice ripples
        w_primary = (
            0.62 * math.sin((u * 9.0 - phase * 1.15) * math.pi)
            + 0.28 * math.cos((u * 17.0 + phase * 1.85) * math.pi)
            + 0.16 * math.sin((u * 25.0 - phase * 2.4) * math.pi)
        )
        w_counter = (
            0.55 * math.sin((u * 6.5 + phase * 0.9) * math.pi)
            - 0.35 * math.cos((u * 13.0 - phase * 1.3) * math.pi)
        )
        pts_main.append((x, mid_y - amp * env * w_primary))
        pts_sec.append((x, mid_y + (amp * 0.72) * env * w_counter))

    draw.line(pts_main, fill=c_glow, width=6, joint="curve")
    draw.line(pts_sec, fill=c_mid, width=3, joint="curve")
    draw.line(pts_main, fill=c_main, width=3, joint="curve")
    draw.line(pts_main, fill=c_core, width=2 if (active or mic_enabled) else 1, joint="curve")

    cx, cy = sw / 2.0, mid_y
    pulse = 0.5 + 0.5 * math.sin(phase)
    gr = (5.0 + 2.5 * pulse if (active or mic_enabled) else 4.2 + 1.2 * pulse) * scale
    draw.ellipse([cx - gr * 1.5, cy - gr * 1.5, cx + gr * 1.5, cy + gr * 1.5], fill=c_glow)
    draw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], outline=c_main, width=2)
    pin = 2.2 * scale
    draw.ellipse([cx - pin, cy - pin, cx + pin, cy + pin], fill=c_core)

    return ImageTk.PhotoImage(img.resize((w, h), Image.Resampling.BILINEAR))


def _draw_brow_crest(canvas: tk.Canvas, width: int, expanded: bool = False, bg_hex: str = "#21252C") -> Any:
    """Draw the metallic Ultron brow crest (3x PIL supersampled when available, canvas polygon fallback)."""
    canvas.delete("all")
    w = max(120, int(width))
    h = 12 if expanded else 9
    photo = _render_brow_crest_photo(w, expanded=expanded, bg_hex=bg_hex)
    if photo is not None:
        canvas.create_image(w // 2, h // 2, image=photo)
        return photo

    cw = 168 if expanded else 72
    top_l = (w - cw) // 2
    top_r = top_l + cw
    bot_inset = 16 if expanded else 8
    crest_h = 10 if expanded else 6
    canvas.create_polygon(
        top_l, 0,
        top_r, 0,
        top_r - bot_inset, crest_h,
        top_l + bot_inset, crest_h,
        fill="#8E9AAE",
        outline="#DCE4EE",
    )
    slit_w = 80 if expanded else 28
    sl_l = (w - slit_w) // 2
    sl_r = sl_l + slit_w
    slit_y = 3 if expanded else 2
    canvas.create_rectangle(sl_l, slit_y, sl_r, slit_y + 2, fill="#5CC3F2", outline="")

    if expanded:
        mid = w // 2
        for idx in range(4):
            lx = mid - 148 + idx * 9
            canvas.create_polygon(lx, 2, lx + 4, 2, lx + 7, 9, lx + 3, 9, fill="#10131A", outline="#5A6475")
            rx = mid + 114 + idx * 9
            canvas.create_polygon(rx + 3, 2, rx + 7, 2, rx + 4, 9, rx, 9, fill="#10131A", outline="#5A6475")
    return None


@dataclass(frozen=True)
class IslandGeometry:
    x: int
    y: int
    width: int
    height: int


def compute_island_geometry(
    screen_w: int,
    screen_h: int,
    expanded: bool = False,
    top_margin: int = 0,
    shuttered: bool = False,
) -> IslandGeometry:
    """Compute top-flush Dynamic Island coordinates across Compact, Shuttered, and Expanded states."""
    if expanded:
        width, height = 960, 520
    elif shuttered:
        width, height = 220, 32
    else:
        width, height = 310, 40
    width = min(width, max(200, screen_w - 24))
    height = min(height, max(28, screen_h - 48))
    x = max(8, (screen_w - width) // 2)
    y = max(0, int(top_margin))
    return IslandGeometry(x=x, y=y, width=width, height=height)


def compute_spring_frames(
    start: IslandGeometry,
    end: IslandGeometry,
    steps: int = 16,
) -> list[IslandGeometry]:
    """Generate spring-damped interpolation frames for smooth Dynamic Island morphing."""
    count = max(1, int(steps))
    expanding = end.width > start.width or end.height > start.height
    frames: list[IslandGeometry] = []
    for i in range(1, count + 1):
        t = i / count
        if i == count:
            frames.append(end)
            break
        if expanding:
            # Critically-damped smooth deceleration (strictly monotonic settle with zero frame clipping)
            eased = 1.0 - ((1.0 - t) ** 3) * math.cos(t * math.pi * 0.35)
        else:
            # Smooth quintic-like deceleration for clean collapse / shutter without clipping
            eased = 1.0 - ((1.0 - t) ** 3.2)
        frames.append(
            IslandGeometry(
                x=round(start.x + (end.x - start.x) * eased),
                y=round(start.y + (end.y - start.y) * eased),
                width=max(120, round(start.width + (end.width - start.width) * eased)),
                height=max(24, round(start.height + (end.height - start.height) * eased)),
            )
        )
    return frames


def next_persona(current: str) -> str:
    cur = (current or "off").lower()
    if cur not in _PERSONA_CYCLE:
        return "jarvis"
    idx = _PERSONA_CYCLE.index(cur)
    return _PERSONA_CYCLE[(idx + 1) % len(_PERSONA_CYCLE)]


def format_pill_badge(
    busy: bool,
    state_text: str,
    persona: str,
    read_only: bool,
    agent_role: str = "auto",
) -> str:
    mode = "RO" if read_only else "RW"
    voice_tag = "J" if persona == "jarvis" else ("U" if persona == "ultron" else ("M" if persona == "megatron" else "Mute"))
    base = state_text or ("Thinking..." if busy else "AURA Jarvis")
    clean_agent = normalize_agent_role(agent_role)
    if clean_agent != "auto":
        return f"{base} · {clean_agent.title()} · {voice_tag} · {mode}"
    return f"{base}  ·  {voice_tag} · {mode}"


def clean_reply_and_metrics(answer: str, metrics: str = "") -> tuple[str, str]:
    """Strip verbose inline `[[Metrics ...]]` tags and return clean reply + compact meta."""
    body = _INLINE_METRICS_RE.sub("", answer or "").strip()
    meta = (metrics or "").strip()
    if meta.startswith("[") and meta.endswith("]"):
        m = re.search(r"Metrics\s*\(([^)]+)\)", meta, re.IGNORECASE)
        if m and m.group(1):
            meta = f"Model: {m.group(1)}"
        elif "Metrics" in meta:
            meta = ""
    return body, meta


def format_friendly_error(err: str) -> str:
    """Turn verbose transport traces into clear actionable Dynamic Island messages."""
    low = (err or "").lower()
    if "ollama" in low and ("cannot connect" in low or "failed to connect" in low):
        return "Local Ollama is offline — click 'Start Ollama' below to wake it cleanly, then retry."
    if "read timed out" in low or "timed out" in low:
        return "Request timed out waiting for the model. Retry or free VRAM if GPU memory is full."
    return (err or "Unknown error").strip()


def _get_top_hwnd(hwnd: int) -> int:
    """Resolve Tkinter's outer top-level Win32 wrapper HWND from its child frame HWND."""
    if sys.platform != "win32" or not hwnd:
        return int(hwnd or 0)
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        parent = user32.GetParent(wintypes.HWND(hwnd))
        return int(parent) if parent else int(hwnd)
    except Exception:
        return int(hwnd)


def signal_overlay_wake_event() -> bool:
    """Signal the running Dynamic Island process on Windows to surface and expand."""
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.CreateEventW(None, False, False, _OVERLAY_WAKE_EVENT_NAME)
        if not handle:
            return False
        try:
            return bool(k32.SetEvent(handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def apply_window_region(
    hwnd: int,
    width: int,
    height: int,
    radius: int,
    *,
    top_flush: bool = True,
    redraw: bool = True,
) -> bool:
    """Shape the borderless window into a top-flush notch (flat top, rounded bottom) or pill."""
    if sys.platform != "win32" or not hwnd or width <= 0 or height <= 0:
        return False
    try:
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32.CreateRoundRectRgn.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
        gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gdi32.CreateRectRgn.restype = wintypes.HRGN
        gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
        gdi32.CombineRgn.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        user32.SetWindowRgn.restype = ctypes.c_int

        w = int(width)
        h = int(height)
        r = max(2, min(int(radius), h * 2))
        top_hwnd = _get_top_hwnd(hwnd)
        targets = [top_hwnd] if top_hwnd == int(hwnd) else [top_hwnd, int(hwnd)]
        ok_any = False
        for target in targets:
            if top_flush:
                # Offset top arc above y=0 and clip to [0, 0, w, h] so top bezel seam is 100% flush.
                rgn = gdi32.CreateRoundRectRgn(0, -r, w + 1, h + 1, r, r)
                if not rgn:
                    continue
                clip = gdi32.CreateRectRgn(0, 0, w, h)
                if clip:
                    gdi32.CombineRgn(rgn, rgn, clip, 1)  # RGN_AND
                    gdi32.DeleteObject(clip)
            else:
                rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
                if not rgn:
                    continue
            if user32.SetWindowRgn(wintypes.HWND(target), rgn, bool(redraw)):
                ok_any = True
            else:
                gdi32.DeleteObject(rgn)
        return ok_any
    except Exception:
        return False


def apply_window_chrome(hwnd: int) -> dict[str, bool]:
    """Apply Win11 dark mode, toolwindow style, DWM halo removal, and optional capture exclusion."""
    flags = {"rounded": False, "dark": False, "excluded": False, "toolwindow": False}
    if sys.platform != "win32" or not hwnd:
        return flags
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

        target = _get_top_hwnd(hwnd)
        ex = user32.GetWindowLongW(wintypes.HWND(target), GWL_EXSTYLE)
        user32.SetWindowLongW(wintypes.HWND(target), GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)
        flags["toolwindow"] = True

        # Disable DWM non-client shadow/border halo so custom GDI SetWindowRgn silhouette renders cleanly.
        nc_disabled = ctypes.c_int(1)  # DWMNCRP_DISABLED
        dwm.DwmSetWindowAttribute(wintypes.HWND(target), 2, ctypes.byref(nc_disabled), ctypes.sizeof(nc_disabled))

        dark = ctypes.c_int(1)
        if dwm.DwmSetWindowAttribute(wintypes.HWND(target), 20, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
            flags["dark"] = True

        no_dwm_round = ctypes.c_int(1)  # DWMWCP_DONOTROUND (prevents 8px DWM box fighting SetWindowRgn)
        if dwm.DwmSetWindowAttribute(wintypes.HWND(target), 33, ctypes.byref(no_dwm_round), ctypes.sizeof(no_dwm_round)) == 0:
            flags["rounded"] = True

        no_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
        dwm.DwmSetWindowAttribute(wintypes.HWND(target), 34, ctypes.byref(no_border), ctypes.sizeof(no_border))

        if os.environ.get("AURA_PILL_STEALTH") == "1":
            flags["excluded"] = bool(user32.SetWindowDisplayAffinity(wintypes.HWND(target), WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        pass
    return flags


class _CpuSampler:
    """Zero-subprocess Windows CPU/RAM/Battery reader via ctypes."""

    def __init__(self) -> None:
        self._prev_idle = 0
        self._prev_total = 0

    def sample(self) -> dict[str, Any]:
        ram_pct = 0
        cpu_pct = 0
        battery_pct: int | None = None
        charging = False
        if sys.platform != "win32":
            return {"cpu_pct": 0, "ram_pct": 0, "battery_pct": None, "charging": False}
        try:
            k32 = ctypes.windll.kernel32

            class _Mem(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            mem = _Mem()
            mem.dwLength = ctypes.sizeof(mem)
            if k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                ram_pct = int(mem.dwMemoryLoad)

            class _Pwr(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", wintypes.BYTE),
                    ("BatteryFlag", wintypes.BYTE),
                    ("BatteryLifePercent", wintypes.BYTE),
                    ("SystemStatusFlag", wintypes.BYTE),
                    ("BatteryLifeTime", wintypes.DWORD),
                    ("BatteryFullLifeTime", wintypes.DWORD),
                ]

            pwr = _Pwr()
            if k32.GetSystemPowerStatus(ctypes.byref(pwr)) and pwr.BatteryLifePercent <= 100:
                battery_pct = int(pwr.BatteryLifePercent)
                charging = bool(pwr.ACLineStatus == 1)

            idle_ft, kernel_ft, user_ft = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if k32.GetSystemTimes(ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)):
                idle = (idle_ft.dwHighDateTime << 32) | idle_ft.dwLowDateTime
                kernel = (kernel_ft.dwHighDateTime << 32) | kernel_ft.dwLowDateTime
                user = (user_ft.dwHighDateTime << 32) | user_ft.dwLowDateTime
                total = kernel + user
                if self._prev_total and total > self._prev_total:
                    d_total = total - self._prev_total
                    d_idle = max(0, idle - self._prev_idle)
                    cpu_pct = max(0, min(100, round((1.0 - d_idle / d_total) * 100)))
                self._prev_idle = idle
                self._prev_total = total
        except Exception:
            pass
        return {"cpu_pct": cpu_pct, "ram_pct": ram_pct, "battery_pct": battery_pct, "charging": charging}


class JarvisBridgeClient:
    """Thin synchronous HTTP client connecting the Ultron island to AURA V4 server."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self.base_url = base_url.rstrip("/")

    def get_status(self) -> dict[str, Any]:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{self.base_url}/api/status")
            r.raise_for_status()
            return r.json()

    def send_chat(
        self,
        prompt: str,
        agent_role: str | None = None,
        push: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
        clean_agent = normalize_agent_role(agent_role)
        if clean_agent != "auto":
            payload["agent_role"] = clean_agent
        if push:
            payload["push"] = True
        with httpx.Client(timeout=180.0) as client:
            r = client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()

    def steer_chat(
        self,
        prompt: str,
        agent_role: str | None = None,
        source: str = "text",
        keep_output: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "source": str(source or "text"),
            "keep_output": bool(keep_output),
        }
        clean_agent = normalize_agent_role(agent_role)
        if clean_agent != "auto":
            payload["agent_role"] = clean_agent
        with httpx.Client(timeout=15.0) as client:
            r = client.post(f"{self.base_url}/api/chat/steer", json=payload)
            r.raise_for_status()
            return dict(r.json())

    def set_mode(self, mode: str) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(f"{self.base_url}/api/mode", json={"mode": mode})
            r.raise_for_status()
            return r.json()

    def set_model(self, model_override: str | None, low_vram: bool = False) -> dict[str, Any]:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{self.base_url}/api/model",
                json={"low_vram": low_vram, "model_override": model_override},
            )
            r.raise_for_status()
            return r.json()

    def set_voice(self, persona: str) -> dict[str, Any]:
        with httpx.Client(timeout=6.0) as client:
            r = client.post(
                f"{self.base_url}/api/voice/config",
                json={"persona": persona, "auto_speak": persona != "off"},
            )
            r.raise_for_status()
            return r.json()

    def speak_local(self, text: str, persona: str) -> None:
        if persona not in ("jarvis", "ultron", "megatron"):
            return
        with httpx.Client(timeout=15.0) as client:
            client.post(
                f"{self.base_url}/api/voice/speak",
                json={"text": text, "persona": persona, "play_local": True},
            )

    def unload_vram(self) -> dict[str, Any]:
        with httpx.Client(timeout=12.0) as client:
            r = client.post(f"{self.base_url}/api/vram/unload")
            r.raise_for_status()
            return r.json()

    def start_ollama(self) -> dict[str, Any]:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(f"{self.base_url}/api/ollama/start")
            r.raise_for_status()
            return r.json()

    def shutdown(self) -> None:
        try:
            with httpx.Client(timeout=8.0) as client:
                client.post(f"{self.base_url}/api/shutdown")
        except Exception:
            pass

    def stop(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.post(f"{self.base_url}/api/stop")
                res.raise_for_status()
                return dict(res.json())
        except Exception:
            return {"stopped": False}

    def stop_voice(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=2.0) as client:
                res = client.post(f"{self.base_url}/api/voice/stop")
                res.raise_for_status()
                return dict(res.json())
        except Exception:
            return {"stopped": False}

    def get_dashboard_data(self) -> dict[str, Any]:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{self.base_url}/api/dashboard-data")
            r.raise_for_status()
            return r.json()

    def todo_action(self, action: str, text: str = "", done: bool = True) -> dict[str, Any]:
        with httpx.Client(timeout=5.0) as client:
            r = client.post(
                f"{self.base_url}/api/todos",
                json={"action": action, "text": text, "done": done},
            )
            r.raise_for_status()
            return r.json()

    def get_apps_status(self) -> dict[str, Any]:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{self.base_url}/api/apps/status")
            r.raise_for_status()
            return r.json()

    def connect_apps(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=6.0) as client:
            r = client.post(f"{self.base_url}/api/apps/connect", json=payload)
            r.raise_for_status()
            return r.json()

    def show_overlay(self, expand: bool = True) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=3.0) as client:
                r = client.post(f"{self.base_url}/api/overlay/show", json={"expand": expand})
                r.raise_for_status()
                return dict(r.json())
        except Exception:
            return {"ok": False}


class DynamicIslandPill:
    """Always-on-top Windows Dynamic Island · AURA V4 Ultron 3-state HUD."""

    def __init__(self, server_url: str = "http://127.0.0.1:8000") -> None:
        enable_high_dpi_awareness()
        self.bridge = JarvisBridgeClient(server_url)
        self.expanded = False
        self.shuttered = False
        self.island_state = "compact"
        self.busy = False
        self.voice_speaking = False
        self.read_only = True
        self.persona = "off"
        self.ollama_up = True
        self.mic_enabled = False
        self.selected_agent = "auto"
        self.active_model_name = "qwen3.5:4b"
        self.available_models: list[str] = ["qwen3.5:4b", "qwen2.5-coder:7b", "qwen3.5:9b"]
        self.available_cloud_models: list[str] = []
        self.available_tools: list[dict[str, str]] = []
        self.active_tab = "chat"
        self._agent_cards: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self._agent_header_pills: dict[str, tk.Button] = {}
        self._pipeline_pills: dict[str, tk.Label] = {}
        self.tab_btns: dict[str, tk.Button] = {}
        self._img_refs: dict[str, Any] = {}
        self._reasoning_open = False
        self._tools_called_count = 0
        self._last_reasoning_text = "No reasoning trace recorded yet."
        self.state_label = "ULTRON VOICE · STANDBY"
        self._phase = 0.0
        self._wave_amp = 8.8
        self._orb_hover = False
        self._current_geo: IslandGeometry | None = None
        self._last_rgn_key: tuple[int, int, int, bool] | None = None
        self._spin_angle = 0.0
        self._stop_req_seq = 0
        self._mode_switching = False
        self._closing = False
        self._has_placeholder = True
        self._active_placeholder = _PLACEHOLDER
        self._steer_seq_seen = 0
        self._last_latest_step = ""
        self._voice_bargein_until = 0.0
        self._pending_collapse_drawer = False
        self._hotkey_stop = threading.Event()
        self._drag_offset = (0, 0)
        self._telemetry = _CpuSampler()
        self._focus_total_sec = 25 * 60
        self._focus_left_sec = 25 * 60
        self._focus_running = False
        self._last_clock_tick = 0.0
        self._agenda_cal_visible = False
        self._last_expand_time = 0.0
        self._had_focus = False
        self._popup_open = False
        self._prev_mouse_down = False
        self._model_popover_visible = False
        self._apps_popover_visible = False
        self._tools_popover_visible = False
        self._tools_pinned = False
        self._tools_hover_close_id: str | None = None
        self._tools_signature: tuple[tuple[str, str], ...] | None = None
        self._anim_after_id: str | None = None
        self._agent_progress_pct = 0
        self._chrome_flags: dict[str, bool] = {"rounded": False}
        self._todo_items: list[tuple[str, bool]] = []
        self._apps_status: dict[str, Any] = {}
        self._apps_drawer_visible = False
        self._todo_vars: list[tk.BooleanVar] = []
        self._last_wake_seq: int = 0
        self._event_woken_at: float | None = None
        self._wake_event_handle: int = 0
        self._telemetry_ticks: int = 0
        if sys.platform == "win32" and not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
                k32.CreateEventW.restype = wintypes.HANDLE
                self._wake_event_handle = int(k32.CreateEventW(None, False, False, _OVERLAY_WAKE_EVENT_NAME) or 0)
            except Exception:
                self._wake_event_handle = 0
        mic_kwargs: dict[str, Any] = {
            "on_transcript": lambda txt: self._schedule_ui(lambda: self._on_voice_transcript(txt)),
            "on_status": lambda st: self._schedule_ui(lambda: self._on_mic_status(st)),
            "on_error": lambda err: self._schedule_ui(lambda: self._on_mic_error(err)),
        }
        try:
            if "on_speech_onset" in inspect.signature(MicListener.__init__).parameters:
                mic_kwargs["on_speech_onset"] = lambda: self._schedule_ui(self._on_speech_onset)
        except Exception:
            pass
        self.mic_listener = MicListener(**mic_kwargs)

        self.root = tk.Tk()
        self.root.title("AURA V4 · Ultron Dynamic Island")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=_PALETTE["bg"])
        # Keep opacity at 1.0 (no WS_EX_LAYERED partial alpha) so Win32 ClearType subpixel rendering stays razor-sharp
        _sync_tk_dpi_scaling(self.root)

        self._load_eye_assets()
        self._build_ui()
        self.root.update_idletasks()
        self._chrome_flags = apply_window_chrome(self.root.winfo_id())
        self._apply_state_layout()
        self._apply_geometry()
        self._ensure_pinned_visible()
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<FocusIn>", self._on_root_focus_in)
        self.root.bind("<FocusOut>", self._on_root_focus_out)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown_app)
        self._start_hotkey_listener()
        self._refresh_status_async()
        self._animate_orb()
        self._tick_telemetry()

    def _load_eye_assets(self) -> None:
        self._img_refs["eye_compact"] = _render_ultron_eye_photo(28, shuttered=False, bg_hex=_PALETTE["gunmetal"])
        self._img_refs["eye_expanded"] = _render_ultron_eye_photo(44, shuttered=False, bg_hex=_PALETTE["hdr_bar"])
        self._img_refs["eye_shuttered"] = _render_ultron_eye_photo(22, shuttered=True, bg_hex=_PALETTE["gunmetal"])
        self._img_refs["eye_avatar"] = _render_ultron_eye_photo(22, shuttered=False, bg_hex=_PALETTE["well"])

    def _build_ui(self) -> None:
        self.frame = tk.Frame(
            self.root,
            bg=_PALETTE["bg"],
            highlightthickness=0,
        )
        self.frame.pack(fill="both", expand=True)

        # Top Brow Crest Strip (shared brushed-steel trapezoid + glowing arc-blue vents)
        self.brow_canvas = tk.Canvas(
            self.frame, height=12, bg=_PALETTE["bg"], highlightthickness=0, cursor="hand2"
        )
        self.brow_canvas.pack(side="top", fill="x", padx=2)
        self.brow_canvas.bind("<Button-1>", self._on_pill_click)
        self.brow_canvas.bind("<B1-Motion>", self._on_pill_drag)

        # --- State 02: Shuttered Standby Bar (220x32 top-flush) ---
        self.shuttered_bar = tk.Frame(self.frame, bg=_PALETTE["gunmetal"])
        self.shutter_eye_canvas = tk.Canvas(
            self.shuttered_bar, width=26, height=26, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.shutter_eye_canvas.pack(side="left", padx=(8, 6), pady=3)
        if self._img_refs.get("eye_shuttered") is not None:
            self.shutter_eye_canvas.create_image(13, 13, image=self._img_refs["eye_shuttered"])
        else:
            self.shutter_eye_canvas.create_oval(2, 2, 24, 24, outline="#6B7587", fill="#13161B", width=2)
            self.shutter_eye_canvas.create_line(7, 13, 19, 13, fill="#8FA0B5", width=2)

        self.shutter_lbl = tk.Label(
            self.shuttered_bar,
            text="AURA · STANDBY",
            bg=_PALETTE["gunmetal"],
            fg=_PALETTE["muted"],
            font=(_TECH_FONT, 8, "bold"),
            cursor="hand2",
        )
        self.shutter_lbl.pack(side="left", expand=True)

        self.wake_switch_canvas = tk.Canvas(
            self.shuttered_bar, width=44, height=24, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.wake_switch_canvas.pack(side="right", padx=(6, 8), pady=3)
        self._img_refs["wake_sw"] = self._draw_switch(self.wake_switch_canvas, active=False, bg_hex=_PALETTE["gunmetal"])

        self.shutter_eye_canvas.bind("<Button-1>", lambda _e: self.wake_from_shutter())
        self.shutter_lbl.bind("<Button-1>", lambda _e: self.wake_from_shutter())
        self.wake_switch_canvas.bind("<Button-1>", lambda _e: self.wake_from_shutter())
        self.shuttered_bar.bind("<B1-Motion>", self._on_pill_drag)

        # --- Shared Capsule Header (Compact 01 + Expanded 03) ---
        self.header = tk.Frame(
            self.frame,
            bg=_PALETTE["gunmetal"],
            height=54,
            highlightbackground=_PALETTE["border"],
            highlightthickness=1,
        )
        self.header.pack(side="top", fill="x", padx=8, pady=(0, 4))
        self.header.pack_propagate(False)

        # Ultron Eye Core (keeps orb_outer + orb_core IDs for animations/tests)
        self.orb_canvas = tk.Canvas(
            self.header, width=54, height=54, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.orb_canvas.pack(side="left", padx=(6, 4))
        self._orb_eye_item = None
        if self._img_refs.get("eye_compact") is not None:
            self._orb_eye_item = self.orb_canvas.create_image(27, 27, image=self._img_refs["eye_compact"])
        self.orb_outer = self.orb_canvas.create_oval(7, 7, 47, 47, outline=_PALETTE["accent"], width=1.6)
        self.orb_core = self.orb_canvas.create_oval(23, 23, 31, 31, fill=_PALETTE["accent"], outline="")

        # Expanded-only divider + prominent Model Selector pill button
        self.exp_div1 = tk.Frame(self.header, width=1, height=30, bg="#394456")
        self.model_btn = self._mk_btn(
            self.header,
            f"◈ {self.active_model_name} ▾",
            self._open_model_menu,
            bg=_PALETTE["well"],
            fg=_PALETTE["arc_text"],
            hover_bg=_PALETTE["pill_active_bg"],
            hover_fg="#FFFFFF",
            padx=5,
            pady=2,
            border_color=_PALETTE["arc_border"],
        )
        self.exp_div2 = tk.Frame(self.header, width=1, height=30, bg="#394456")

        # Expanded-only Sequential & Sub-Agent Pills bar (@auto @planner @coder @researcher @gmail @calendar)
        self.hdr_agents_frame = tk.Frame(self.header, bg=_PALETTE["hdr_bar"])
        for pill_lbl, role_target in _QUICK_AGENTS:
            pbtn = tk.Button(
                self.hdr_agents_frame,
                text=pill_lbl,
                command=lambda lbl=pill_lbl, rt=role_target: self._on_header_pill_click(lbl, rt),
                bg=_PALETTE["panel_mid"],
                fg="#DCE4EE",
                activebackground=_PALETTE["pill_active_bg"],
                activeforeground=_PALETTE["arc_text"],
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground="#5A667A",
                font=(_MONO_FONT, 7, "bold"),
                padx=3,
                pady=2,
                cursor="hand2",
            )
            pbtn.pack(side="left", padx=1)
            self._agent_header_pills[pill_lbl] = pbtn

        self.exp_div3 = tk.Frame(self.header, width=1, height=30, bg="#394456")

        # Unified Right Header Cluster packed BEFORE expanding middle wave_stack so it is never squeezed at high DPI
        self.hdr_right = tk.Frame(self.header, bg=_PALETTE["gunmetal"])
        self.hdr_right.pack(side="right", padx=(2, 6))

        # Pack Shutdown button FIRST on the right edge so it always renders visibly in Compact & Expanded states
        self.shutdown_btn = self._mk_btn(
            self.hdr_right,
            "⏻ Off",
            self.shutdown_app,
            bg=_PALETTE["danger_bg"],
            fg=_PALETTE["danger_fg"],
            hover_bg=_PALETTE["danger_hover"],
            hover_fg="#FFFFFF",
            padx=6,
            pady=2,
            border_color="#E11D48",
        )
        self.shutdown_btn.pack(side="right", padx=(3, 0))

        self.compact_voice_btn = self._mk_btn(
            self.hdr_right,
            "🔊",
            self._cycle_persona,
            bg=_PALETTE["well"],
            fg=_PALETTE["steel"],
            hover_bg=_PALETTE["titanium"],
            hover_fg=_PALETTE["accent"],
            padx=6,
            pady=2,
            border_color="#485364",
        )
        self.compact_voice_btn.pack(side="left", padx=(0, 4))

        self.mic_lbl_hdr = tk.Label(
            self.hdr_right, text="Mic", bg=_PALETTE["gunmetal"], fg="#DCE4EE", font=(_UI_FONT, 8, "bold")
        )
        self.mic_lbl_hdr.pack(side="left", padx=(0, 3))

        # Preserve attribute name pill.capsule_mic_btn (Canvas toggle switch)
        self.capsule_mic_btn = tk.Canvas(
            self.hdr_right, width=44, height=24, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.capsule_mic_btn.pack(side="left", padx=(0, 4))
        self._img_refs["cap_sw"] = self._draw_switch(self.capsule_mic_btn, active=False, bg_hex=_PALETTE["gunmetal"])
        self.capsule_mic_btn.bind("<Button-1>", lambda _e: self.toggle_mic())

        self.stop_canvas = tk.Canvas(
            self.hdr_right, width=22, height=22, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.stop_ring_arc = self.stop_canvas.create_arc(
            2, 2, 20, 20, start=0, extent=255, style="arc", outline=_PALETTE["accent"], width=2
        )
        self.stop_square = self.stop_canvas.create_rectangle(7, 7, 15, 15, fill="#E8EDF4", outline="#E8EDF4")
        self.stop_canvas.bind("<Button-1>", lambda _e: self.stop_active())

        # Packed only in expanded mode (kept compact pill free of clutter)
        self.hdr_stop_btn = self._mk_btn(
            self.hdr_right,
            "■ Stop",
            self.stop_active,
            bg=_PALETTE["card_hi"],
            fg=_PALETTE["text"],
            hover_bg=_PALETTE["danger_bg"],
            hover_fg=_PALETTE["danger_fg"],
            padx=9,
            pady=3,
            border_color="#7A899D",
        )

        self.apps_btn = self._mk_btn(
            self.hdr_right,
            "⊞ Apps",
            self._open_apps_popover,
            bg=_PALETTE["well"],
            fg=_PALETTE["arc_text"],
            hover_bg=_PALETTE["pill_active_bg"],
            hover_fg="#FFFFFF",
            padx=6,
            pady=2,
            border_color=_PALETTE["arc_border"],
        )

        self.shutter_btn = self._mk_btn(
            self.hdr_right,
            "◐",
            self.enter_shuttered,
            bg=_PALETTE["card_hi"],
            fg="#DCE4EE",
            hover_bg=_PALETTE["titanium"],
            hover_fg=_PALETTE["accent"],
            padx=6,
            pady=2,
            border_color="#7A899D",
        )

        # Arc-Blue Filament Waveform & Subtitle Stack (packed AFTER hdr_right so right controls keep exact width)
        self.wave_stack = tk.Frame(self.header, bg=_PALETTE["gunmetal"])
        self.wave_stack.pack(side="left", fill="both", expand=True, padx=(4, 4))

        self.wave_canvas = tk.Canvas(
            self.wave_stack, width=172, height=36, bg=_PALETTE["gunmetal"], highlightthickness=0, cursor="hand2"
        )
        self.wave_canvas.pack(side="top", expand=True)
        self._wave_img_item = self.wave_canvas.create_image(86, 18)
        # Keep dual filament vectors + glow + compatibility bars for fallback / tests
        self._fil_line_dim = self.wave_canvas.create_line(0, 18, 172, 18, fill=_PALETTE["deep_blue"], width=1.6, smooth=True)
        self._fil_line_main = self.wave_canvas.create_line(0, 18, 172, 18, fill="#8FD3F4", width=2.0, smooth=True)
        self._fil_glow = self.wave_canvas.create_oval(78, 10, 94, 26, outline=_PALETTE["accent"], width=1)
        self._fil_dot = self.wave_canvas.create_oval(84, 16, 88, 20, fill="#E6F6FF", outline="")
        self._wave_bars = [
            self.wave_canvas.create_rectangle(-10, -10, -8, -8, fill="#5CC3F2", outline="")
            for _ in range(4)
        ]

        self.pill_label = tk.Label(
            self.wave_stack,
            text="ULTRON VOICE · STANDBY",
            bg=_PALETTE["hdr_bar"],
            fg=_PALETTE["steel"],
            font=(_TECH_FONT, 8, "bold"),
            cursor="hand2",
        )

        for w in (self.orb_canvas, self.wave_canvas, self.pill_label, self.wave_stack):
            w.bind("<Button-1>", self._on_pill_click)
            w.bind("<B1-Motion>", self._on_pill_drag)
        for hw in (self.frame, self.header, self.orb_canvas, self.wave_canvas):
            hw.bind("<Enter>", lambda _e: setattr(self, "_orb_hover", True))
            hw.bind("<Leave>", lambda _e: setattr(self, "_orb_hover", False))

        # --- Expanded Notch Drawer (State 03: 960x520 top-flush) ---
        self.drawer = tk.Frame(self.frame, bg=_PALETTE["bg"])

        # Bottom Floating Navigation & Live Telemetry Bar (no duplicate Quit button)
        self.tab_bar = tk.Frame(self.drawer, bg=_PALETTE["bg"], height=36)
        self.tab_bar.pack(side="bottom", fill="x", padx=12, pady=(2, 6))
        self.tab_bar.pack_propagate(False)

        tab_track = tk.Frame(self.tab_bar, bg=_PALETTE["bg"])
        tab_track.pack(side="left")
        for tab_key, tab_title in (
            ("chat", "Ask Aura"),
            ("agents", "Agents"),
            ("deck", "Hub & Focus"),
        ):
            tbtn = tk.Button(
                tab_track,
                text=tab_title,
                command=lambda k=tab_key: self.switch_tab(k),
                bg=_PALETTE["bg"],
                fg="#DCE4EE",
                activebackground=_PALETTE["pill_active_bg"],
                activeforeground=_PALETTE["arc_text"],
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground="#5A667A",
                font=(_UI_FONT, 9, "bold"),
                padx=12,
                pady=3,
                cursor="hand2",
            )
            tbtn.pack(side="left", padx=(0, 6))
            self.tab_btns[tab_key] = tbtn

        self._mk_btn(
            tab_track,
            "Web UI",
            lambda: webbrowser.open(self.bridge.base_url),
            bg=_PALETTE["bg"],
            fg="#DCE4EE",
            padx=10,
            pady=3,
            border_color="#5A667A",
        ).pack(side="left", padx=(0, 8))

        self.mode_btn = self._mk_btn(
            tab_track, "RO", self._toggle_mode, bg=_PALETTE["well"], fg=_PALETTE["blue"], padx=9, pady=3, border_color="#485364"
        )
        self.mode_btn.pack(side="left", padx=(0, 4))

        self.voice_btn = self._mk_btn(
            tab_track, "Voice: Off", self._cycle_persona, bg=_PALETTE["well"], fg=_PALETTE["steel"], padx=9, pady=3, border_color="#485364"
        )
        self.voice_btn.pack(side="left", padx=(0, 4))

        self.ollama_btn = self._mk_btn(
            tab_track, "● Ollama", self._wake_ollama, bg=_PALETTE["well"], fg=_PALETTE["accent"], padx=9, pady=3, border_color="#485364"
        )
        self.ollama_btn.pack(side="left", padx=(0, 4))

        self.capsule_tel_lbl = tk.Label(
            self.tab_bar,
            text="CPU 2%  RAM 60%  AC Power · Click outside or Esc to hide",
            bg=_PALETTE["bg"],
            fg=_PALETTE["muted"],
            font=(_MONO_FONT, 8),
        )
        self.capsule_tel_lbl.pack(side="right", padx=(6, 0))

        # Kept for compatibility across methods
        self.clock_lbl = tk.Label(self.tab_bar, text="", bg=_PALETTE["bg"], fg=_PALETTE["muted"])
        self.bat_pill_lbl = tk.Label(self.tab_bar, text="", bg=_PALETTE["bg"], fg=_PALETTE["muted"])

        # Main Panel Host
        self.panel_host = tk.Frame(self.drawer, bg=_PALETTE["bg"])
        self.panel_host.pack(fill="both", expand=True, padx=12, pady=(2, 0))

        self._build_menus()
        self._build_model_popover()
        self._build_apps_popover()
        self._build_tools_popover()
        self._build_chat_panel()
        self._build_agents_panel()
        self._build_deck_panel()

        self.select_agent("auto")
        self.switch_tab("chat")

    def _build_menus(self) -> None:
        self.agent_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["text"],
            activebackground=_PALETTE["pill_active_bg"],
            activeforeground=_PALETTE["arc_text"],
            bd=1,
            font=(_UI_FONT, 9),
        )
        self.agent_menu.add_command(label="Auto (Sequential Pipeline)", command=lambda: self.select_agent("auto"))
        for role_key, spec in SPECIALIST_AGENTS.items():
            self.agent_menu.add_command(
                label=f"@{role_key} — {spec['label']}",
                command=lambda k=role_key: self.select_agent(k),
            )

        self.model_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["text"],
            activebackground=_PALETTE["pill_active_bg"],
            activeforeground=_PALETTE["arc_text"],
            bd=1,
            font=(_UI_FONT, 9),
        )
        self._populate_model_menu()

    def _build_model_popover(self) -> None:
        """Create the crisp in-island floating Model Selector popover matching Ultron HUD."""
        self.model_popover = tk.Frame(
            self.frame,
            bg="#232832",
            highlightbackground=_PALETTE["arc_border"],
            highlightthickness=1,
        )
        self._model_popover_body = tk.Frame(self.model_popover, bg="#232832")
        self._model_popover_body.pack(fill="both", expand=True, padx=6, pady=6)
        self._populate_model_popover()

    def _collect_model_choices(self) -> list[tuple[str, str]]:
        choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in self.available_models:
            seen.add(m)
            choices.append((m, f"{m} · Ollama" if ":" in m else m))
        if not choices:
            for _key, label, spec in _DEFAULT_MODELS:
                if spec in seen:
                    continue
                seen.add(spec)
                choices.append((spec, label))
        for cm in self.available_cloud_models:
            if cm in seen:
                continue
            seen.add(cm)
            choices.append((cm, _format_cloud_model_label(cm)))
        return choices

    def _populate_model_popover(self) -> None:
        for child in self._model_popover_body.winfo_children():
            child.destroy()
        hdr = tk.Label(
            self._model_popover_body,
            text="ACTIVE MODEL / PROVIDER",
            bg="#232832",
            fg=_PALETTE["muted"],
            font=(_TECH_FONT, 8, "bold"),
            anchor="w",
            padx=8,
            pady=3,
        )
        hdr.pack(fill="x")
        for spec, disp in self._collect_model_choices():
            selected = spec == self.active_model_name
            r_bg = _PALETTE["pill_active_bg"] if selected else "#232832"
            r_fg = "#FFFFFF" if selected else _PALETTE["steel"]
            prefix = "● " if selected else "   "
            row = tk.Button(
                self._model_popover_body,
                text=f"{prefix}{disp}",
                command=lambda s=spec, d=disp: self._pick_model_from_popover(s, d),
                bg=r_bg,
                fg=r_fg,
                activebackground=_PALETTE["cta_bg"],
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                highlightthickness=1 if selected else 0,
                highlightbackground=_PALETTE["arc_border"],
                font=(_UI_FONT, 9, "bold" if selected else "normal"),
                anchor="w",
                padx=10,
                pady=5,
                width=24,
                cursor="hand2",
            )
            row.pack(fill="x", pady=1)

    def _pick_model_from_popover(self, spec: str, disp: str) -> None:
        self._close_model_popover()
        self._select_model(spec, disp)

    def _close_model_popover(self) -> None:
        if getattr(self, "_model_popover_visible", False):
            self._model_popover_visible = False
            self.model_popover.place_forget()

    def _build_apps_popover(self) -> None:
        self.apps_popover = tk.Frame(
            self.root,
            bg=_PALETTE["bg"],
            highlightbackground=_PALETTE["arc_border"],
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        hdr = tk.Frame(self.apps_popover, bg=_PALETTE["bg"])
        hdr.pack(fill="x", pady=(0, 8))

        title_col = tk.Frame(hdr, bg=_PALETTE["bg"])
        title_col.pack(side="left")
        tk.Label(
            title_col,
            text="APPS & CONNECTORS",
            bg=_PALETTE["bg"],
            fg=_PALETTE["arc_text"],
            font=(_TECH_FONT, 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_col,
            text="Manage Workspace integrations, Cloud providers & active MCP plugins",
            bg=_PALETTE["bg"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
        ).pack(anchor="w")

        hdr_right = tk.Frame(hdr, bg=_PALETTE["bg"])
        hdr_right.pack(side="right")
        self._apps_filter_var = tk.StringVar(value="")
        self._apps_cat_filter = "all"
        self._apps_search_entry = tk.Entry(
            hdr_right,
            textvariable=self._apps_filter_var,
            bg=_PALETTE["well"],
            fg=_PALETTE["text"],
            insertbackground=_PALETTE["accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground="#485364",
            width=22,
            font=(_UI_FONT, 9),
        )
        self._apps_search_entry.insert(0, "Search apps & plugins...")
        self._apps_search_entry.configure(fg=_PALETTE["muted"])
        self._apps_search_entry.bind(
            "<FocusIn>",
            lambda _e: (
                self._apps_search_entry.delete(0, "end"),
                self._apps_search_entry.configure(fg=_PALETTE["text"]),
            )
            if self._apps_search_entry.get() == "Search apps & plugins..."
            else None,
        )
        self._apps_search_entry.bind("<KeyRelease>", lambda _e: self._filter_apps_cards())
        self._apps_search_entry.pack(side="left", padx=(0, 8), ipady=3)

        self._apps_cat_btns: dict[str, tk.Button] = {}
        for c_key, c_lbl in (("all", "All"), ("connected", "Connected"), ("mcp", "MCP")):
            cbtn = self._mk_btn(
                hdr_right,
                c_lbl,
                lambda k=c_key: self._set_apps_cat_filter(k),
                bg=_PALETTE["pill_active_bg"] if c_key == "all" else _PALETTE["well"],
                fg="#FFFFFF" if c_key == "all" else _PALETTE["steel"],
                padx=8,
                pady=2,
                border_color=_PALETTE["arc_border"] if c_key == "all" else "#485364",
            )
            cbtn.pack(side="left", padx=(0, 4))
            self._apps_cat_btns[c_key] = cbtn

        self._mk_btn(
            hdr_right,
            "✕ Close",
            self._close_apps_popover,
            bg=_PALETTE["card"],
            fg=_PALETTE["text"],
            padx=8,
            pady=2,
            border_color="#5A667A",
        ).pack(side="left", padx=(6, 0))

        self._apps_grid = tk.Frame(self.apps_popover, bg=_PALETTE["bg"])
        self._apps_grid.pack(fill="both", expand=True)
        self._apps_grid.columnconfigure(0, weight=1, uniform="appcol")
        self._apps_grid.columnconfigure(1, weight=1, uniform="appcol")

        self.apps_gmail_var = tk.BooleanVar(value=True)
        self.apps_cal_var = tk.BooleanVar(value=True)
        self._apps_cards: list[tuple[tk.Frame, str, str, int, int, int]] = []

        # --- Card 1: Gmail Connector ---
        gmail_card = tk.Frame(
            self._apps_grid,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        gmail_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        g_hdr = tk.Frame(gmail_card, bg=_PALETTE["panel_mid"])
        g_hdr.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            g_hdr,
            text="✉  Gmail",
            variable=self.apps_gmail_var,
            command=lambda: self._toggle_app_flag("gmail"),
            bg=_PALETTE["panel_mid"],
            fg="#E8EDF4",
            activebackground=_PALETTE["panel_mid"],
            selectcolor=_PALETTE["cta_bg"],
            highlightthickness=0,
            font=(_UI_FONT, 10, "bold"),
        ).pack(side="left")
        self.apps_gmail_state_lbl = tk.Label(
            g_hdr,
            text="Off",
            bg=_PALETTE["well"],
            fg=_PALETTE["muted"],
            font=(_MONO_FONT, 8, "bold"),
            padx=6,
            pady=1,
        )
        self.apps_gmail_state_lbl.pack(side="right")
        tk.Label(
            gmail_card,
            text="Read unread mail, search threads & compose replies via IMAP/SMTP.",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        tk.Label(
            gmail_card, text="Gmail Address", bg=_PALETTE["panel_mid"], fg=_PALETTE["steel"], font=(_UI_FONT, 8, "bold"), anchor="w"
        ).pack(fill="x")
        self.apps_gmail_addr_entry = tk.Entry(
            gmail_card, bg=_PALETTE["well"], fg=_PALETTE["text"], insertbackground=_PALETTE["accent"],
            relief="flat", highlightthickness=1, highlightbackground="#485364", font=(_UI_FONT, 9),
        )
        self.apps_gmail_addr_entry.insert(0, "you@gmail.com")
        self.apps_gmail_addr_entry.bind("<FocusIn>", lambda _e: self._clear_hint_entry(self.apps_gmail_addr_entry, "you@gmail.com"))
        self.apps_gmail_addr_entry.pack(fill="x", pady=(2, 6), ipady=3)

        tk.Label(
            gmail_card, text="Google App Password", bg=_PALETTE["panel_mid"], fg=_PALETTE["steel"], font=(_UI_FONT, 8, "bold"), anchor="w"
        ).pack(fill="x")
        self.apps_gmail_pass_entry = tk.Entry(
            gmail_card, bg=_PALETTE["well"], fg=_PALETTE["text"], insertbackground=_PALETTE["accent"],
            relief="flat", highlightthickness=1, highlightbackground="#485364", show="*", font=(_UI_FONT, 9),
        )
        self.apps_gmail_pass_entry.pack(fill="x", pady=(2, 4), ipady=3)

        # --- Card 2: Google Calendar Connector ---
        cal_card = tk.Frame(
            self._apps_grid,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        cal_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        c_hdr = tk.Frame(cal_card, bg=_PALETTE["panel_mid"])
        c_hdr.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            c_hdr,
            text="📅  Google Calendar",
            variable=self.apps_cal_var,
            command=lambda: self._toggle_app_flag("calendar"),
            bg=_PALETTE["panel_mid"],
            fg="#E8EDF4",
            activebackground=_PALETTE["panel_mid"],
            selectcolor=_PALETTE["cta_bg"],
            highlightthickness=0,
            font=(_UI_FONT, 10, "bold"),
        ).pack(side="left")
        self.apps_cal_state_lbl = tk.Label(
            c_hdr,
            text="Local",
            bg=_PALETTE["well"],
            fg=_PALETTE["muted"],
            font=(_MONO_FONT, 8, "bold"),
            padx=6,
            pady=1,
        )
        self.apps_cal_state_lbl.pack(side="right")
        tk.Label(
            cal_card,
            text="Sync daily briefings & agenda from private iCal (.ics) feed or local events.",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        tk.Label(
            cal_card, text="Private iCal URL (.ics)", bg=_PALETTE["panel_mid"], fg=_PALETTE["steel"], font=(_UI_FONT, 8, "bold"), anchor="w"
        ).pack(fill="x")
        self.apps_cal_url_entry = tk.Entry(
            cal_card, bg=_PALETTE["well"], fg=_PALETTE["text"], insertbackground=_PALETTE["accent"],
            relief="flat", highlightthickness=1, highlightbackground="#485364", font=(_UI_FONT, 9),
        )
        self.apps_cal_url_entry.insert(0, "Private iCal URL (.ics)")
        self.apps_cal_url_entry.bind("<FocusIn>", lambda _e: self._clear_hint_entry(self.apps_cal_url_entry, "Private iCal URL (.ics)"))
        self.apps_cal_url_entry.pack(fill="x", pady=(2, 8), ipady=3)

        cal_actions = tk.Frame(cal_card, bg=_PALETTE["panel_mid"])
        cal_actions.pack(fill="x", pady=(4, 0))
        self._mk_btn(
            cal_actions,
            "Save & Connect Apps",
            self._save_apps_config,
            bg=_PALETTE["cta_bg"],
            fg="#F2F8FF",
            hover_bg=_PALETTE["cta_hover"],
            padx=10,
            pady=3,
            border_color=_PALETTE["arc_border"],
        ).pack(side="right")
        self.apps_feedback_lbl = tk.Label(
            cal_actions, text="", bg=_PALETTE["panel_mid"], fg=_PALETTE["accent"], font=(_UI_FONT, 8)
        )
        self.apps_feedback_lbl.pack(side="left")

        # --- Card 3: Browser & Chrome MCP Plugin ---
        browser_card = tk.Frame(
            self._apps_grid,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        browser_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        bh = tk.Frame(browser_card, bg=_PALETTE["panel_mid"])
        bh.pack(fill="x", pady=(0, 4))
        tk.Label(
            bh, text="🌐  Browser & Research MCP", bg=_PALETTE["panel_mid"], fg="#E8EDF4", font=(_UI_FONT, 10, "bold")
        ).pack(side="left")
        tk.Label(
            bh, text="● Active · 17 tools", bg=_PALETTE["well"], fg=_PALETTE["accent"], font=(_MONO_FONT, 8, "bold"), padx=6, pady=1
        ).pack(side="right")
        tk.Label(
            browser_card,
            text="Instant Chrome/web opens, compact [e1] DOM automation & safe DuckDuckGo research.",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
            anchor="w",
            wraplength=410,
            justify="left",
        ).pack(fill="x", pady=(0, 6))
        self._mk_btn(
            browser_card,
            "Open Chrome",
            lambda: (self._close_apps_popover(), self._run_quick("open chrome")),
            bg=_PALETTE["well"],
            fg=_PALETTE["text"],
            padx=8,
            pady=2,
            border_color="#485364",
        ).pack(anchor="w")

        # --- Card 4: Desktop Vision & Cloud Plugins ---
        sys_card = tk.Frame(
            self._apps_grid,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        sys_card.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        sh = tk.Frame(sys_card, bg=_PALETTE["panel_mid"])
        sh.pack(fill="x", pady=(0, 4))
        tk.Label(
            sh, text="🖥  Desktop Vision & Cloud APIs", bg=_PALETTE["panel_mid"], fg="#E8EDF4", font=(_UI_FONT, 10, "bold")
        ).pack(side="left")
        tk.Label(
            sh, text="● Active · 37 MCP tools", bg=_PALETTE["well"], fg=_PALETTE["accent"], font=(_MONO_FONT, 8, "bold"), padx=6, pady=1
        ).pack(side="right")
        self._apps_cloud_lbl = tk.Label(
            sys_card,
            text="Desktop/Filesystem/Git MCP ready · Configure Cloud providers in Web UI.",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
            anchor="w",
            wraplength=410,
            justify="left",
        )
        self._apps_cloud_lbl.pack(fill="x", pady=(0, 6))
        cbtns = tk.Frame(sys_card, bg=_PALETTE["panel_mid"])
        cbtns.pack(fill="x")
        self._mk_btn(
            cbtns,
            "Inspect Tools",
            lambda: (self._close_apps_popover(), self._open_tools_popover(pinned=True)),
            bg=_PALETTE["well"],
            fg=_PALETTE["text"],
            padx=8,
            pady=2,
            border_color="#485364",
        ).pack(side="left", padx=(0, 6))
        self._mk_btn(
            cbtns,
            "Web Settings",
            lambda: webbrowser.open(self.bridge.base_url),
            bg=_PALETTE["well"],
            fg=_PALETTE["arc_text"],
            padx=8,
            pady=2,
            border_color="#485364",
        ).pack(side="left")

        self._apps_cards = [
            (gmail_card, "gmail email inbox workspace app", "workspace", 0, 0, 6),
            (cal_card, "google calendar schedule ics workspace app", "workspace", 0, 1, 6),
            (browser_card, "browser chrome research web mcp plugin", "mcp", 1, 0, 6),
            (sys_card, "desktop vision files git cloud gemini freellmapi mcp", "mcp", 1, 1, 6),
        ]

    def _set_apps_cat_filter(self, cat: str) -> None:
        self._apps_cat_filter = cat
        for k, btn in getattr(self, "_apps_cat_btns", {}).items():
            act = k == cat
            btn.configure(
                bg=_PALETTE["pill_active_bg"] if act else _PALETTE["well"],
                fg="#FFFFFF" if act else _PALETTE["steel"],
                highlightbackground=_PALETTE["arc_border"] if act else "#485364",
            )
        self._filter_apps_cards()

    def _filter_apps_cards(self) -> None:
        raw_q = (self._apps_filter_var.get() if hasattr(self, "_apps_filter_var") else "").strip().lower()
        q = "" if raw_q == "search apps & plugins..." else raw_q
        cat = getattr(self, "_apps_cat_filter", "all")
        apps = self._apps_status or {}
        g_conn = bool((apps.get("gmail") or {}).get("connected"))
        c_conn = bool((apps.get("calendar") or {}).get("connected"))
        for idx, (card, keywords, kind, row_i, col_i, pad) in enumerate(getattr(self, "_apps_cards", ())):
            match_q = (not q) or (q in keywords)
            if cat == "mcp":
                match_cat = kind == "mcp"
            elif cat == "connected":
                if idx == 0:
                    match_cat = g_conn
                elif idx == 1:
                    match_cat = c_conn
                else:
                    match_cat = True
            else:
                match_cat = True
            if match_q and match_cat:
                padx = (0, pad) if col_i == 0 else (pad, 0)
                pady = (0, 8) if row_i == 0 else (0, 0)
                card.grid(row=row_i, column=col_i, sticky="nsew", padx=padx, pady=pady)
            else:
                card.grid_remove()

    @staticmethod
    def _clear_hint_entry(entry: tk.Entry, hint: str) -> None:
        if entry.get().strip() == hint:
            entry.delete(0, "end")

    def _close_apps_popover(self) -> None:
        if getattr(self, "_apps_popover_visible", False):
            self._apps_popover_visible = False
            self.apps_popover.place_forget()

    def _open_apps_popover(self) -> None:
        if not self.expanded:
            self.expand()
        self._close_model_popover()
        self._close_tools_popover()
        if getattr(self, "_apps_popover_visible", False):
            self._close_apps_popover()
            return
        self._sync_apps_popover()
        self._filter_apps_cards()
        self._apps_popover_visible = True
        self.apps_popover.place(in_=self.panel_host, x=0, y=0, relwidth=1.0, relheight=1.0)
        self.apps_popover.lift()

    def _sync_apps_popover(self) -> None:
        apps = self._apps_status or {}
        g = apps.get("gmail") or {}
        c = apps.get("calendar") or {}
        if hasattr(self, "apps_gmail_var"):
            self.apps_gmail_var.set(bool(g.get("enabled", True)))
        if hasattr(self, "apps_cal_var"):
            self.apps_cal_var.set(bool(c.get("enabled", True)))
        if hasattr(self, "apps_gmail_state_lbl"):
            self.apps_gmail_state_lbl.configure(
                text=f"● Connected ({g.get('account') or 'ready'})" if g.get("connected") else ("Paused" if g.get("configured") else "Setup Needed"),
                fg=_PALETTE["accent"] if g.get("connected") else _PALETTE["muted"],
            )
        if hasattr(self, "apps_cal_state_lbl"):
            self.apps_cal_state_lbl.configure(
                text=f"● Connected ({c.get('source') or 'sync'})" if c.get("connected") else "Local Agenda",
                fg=_PALETTE["accent"] if c.get("connected") else _PALETTE["muted"],
            )
        if hasattr(self, "_apps_cloud_lbl"):
            cmodels = [ _format_cloud_model_label(m) for m in self.available_cloud_models ]
            cloud_txt = ", ".join(cmodels) if cmodels else "None configured in .env"
            self._apps_cloud_lbl.configure(text=f"Desktop/FS/Git MCP ready · Cloud APIs: {cloud_txt}")
        if hasattr(self, "apps_btn"):
            n = int(bool(g.get("connected"))) + int(bool(c.get("connected")))
            self.apps_btn.configure(text=f"⊞ Apps · {n}" if n else "⊞ Apps")

    def _toggle_app_flag(self, kind: str) -> None:
        payload = (
            {"gmail_enabled": bool(self.apps_gmail_var.get())}
            if kind == "gmail"
            else {"calendar_enabled": bool(self.apps_cal_var.get())}
        )

        def _job() -> None:
            try:
                res = self.bridge.connect_apps(payload)
                self._schedule_ui(lambda: self._after_apps_saved(res, "Updated"))
            except Exception as exc:
                self._schedule_ui(lambda: self.apps_feedback_lbl.configure(text=str(exc)[:36], fg=_PALETTE["amber"]))

        threading.Thread(target=_job, daemon=True).start()

    def _save_apps_config(self) -> None:
        payload: dict[str, Any] = {
            "gmail_enabled": bool(self.apps_gmail_var.get()),
            "calendar_enabled": bool(self.apps_cal_var.get()),
        }
        ga = self.apps_gmail_addr_entry.get().strip()
        gp = self.apps_gmail_pass_entry.get().strip()
        cu = self.apps_cal_url_entry.get().strip()
        if ga and ga != "you@gmail.com":
            payload["gmail_address"] = ga
        if gp:
            payload["gmail_app_password"] = gp
        if cu and cu != "Private iCal URL (.ics)":
            payload["gcal_ical_url"] = cu

        def _job() -> None:
            try:
                res = self.bridge.connect_apps(payload)
                self._schedule_ui(lambda: self._after_apps_saved(res, "Saved"))
            except Exception as exc:
                self._schedule_ui(lambda: self.apps_feedback_lbl.configure(text=str(exc)[:36], fg=_PALETTE["amber"]))

        threading.Thread(target=_job, daemon=True).start()

    def _after_apps_saved(self, res: dict[str, Any], msg: str) -> None:
        if isinstance(res.get("apps"), dict):
            self._apps_status = res["apps"]
            self._sync_apps_popover()
        if isinstance(res.get("dashboard"), dict):
            self._apply_dashboard(res["dashboard"])
        if hasattr(self, "apps_gmail_pass_entry"):
            self.apps_gmail_pass_entry.delete(0, "end")
        if hasattr(self, "apps_feedback_lbl"):
            self.apps_feedback_lbl.configure(text=msg, fg=_PALETTE["accent"])

    @staticmethod
    def _default_tools_catalog() -> list[dict[str, str]]:
        return [
            {"name": "desktop_open_path", "description": "Open local file/folder or reveal in File Explorer"},
            {"name": "desktop_find_files", "description": "Search Desktop, Documents, Downloads & PC index"},
            {"name": "desktop_launch_app", "description": "Launch installed apps or Windows Settings pages"},
            {"name": "desktop_place_window", "description": "Snap, focus, minimize, or maximize windows"},
            {"name": "desktop_read_screen_ocr", "description": "Read visible screen or window text via WinRT OCR"},
            {"name": "desktop_system_info", "description": "Inspect CPU, RAM, disk, battery & open windows"},
            {"name": "desktop_ui_see", "description": "Inspect buttons/tabs inside an open app via UIA"},
            {"name": "desktop_ui_click", "description": "Invoke named buttons or controls inside open apps"},
            {"name": "desktop_inspect_cursor", "description": "Inspect hovered UI element under the mouse cursor"},
            {"name": "desktop_inspect_path", "description": "Inspect local folder/README or read file outside repo"},
            {"name": "jarvis_open_website", "description": "Open websites in Chrome, Edge, or default browser"},
            {"name": "jarvis_play_youtube", "description": "Search and play requested YouTube video"},
            {"name": "jarvis_web_search", "description": "Search live web results and deep research"},
            {"name": "jarvis_gmail", "description": "Check unread Gmail inbox or draft replies"},
            {"name": "jarvis_calendar", "description": "Check today's agenda and upcoming events"},
            {"name": "jarvis_briefing", "description": "Proactive morning briefing across tasks & inbox"},
            {"name": "browser_navigate", "description": "Playwright browser navigation on live web pages"},
            {"name": "browser_snapshot", "description": "Inspect accessible DOM elements & links"},
            {"name": "browser_click", "description": "Click web elements cleanly in Playwright"},
            {"name": "read_file", "description": "Read workspace source files cleanly"},
            {"name": "edit_file", "description": "Apply targeted source code edits"},
            {"name": "search_files", "description": "Search across repository files"},
            {"name": "run_tests", "description": "Execute pytest test suites and verify passes"},
            {"name": "git_status", "description": "Check working tree and branch state"},
        ]

    @staticmethod
    def _tool_prompt_hint(tool_name: str) -> str:
        hints = {
            "desktop_open_path": "open <file or folder> on my desktop",
            "desktop_find_files": "find file ",
            "desktop_launch_app": "open ",
            "desktop_read_screen_ocr": "what is on my screen?",
            "desktop_system_info": "system info",
            "desktop_list_windows": "list windows",
            "desktop_list_monitors": "list monitors",
            "desktop_inspect_cursor": "what is under my cursor?",
            "desktop_inspect_path": "inspect local folder or file ",
            "jarvis_briefing": "morning briefing",
            "jarvis_calendar": "what is on my calendar today?",
            "jarvis_gmail": "check my unread emails",
            "jarvis_web_search": "research online about ",
            "jarvis_play_youtube": "play ",
            "browser_navigate": "open in browser ",
            "git_status": "git status",
        }
        return hints.get(tool_name, f"using {tool_name}: ")

    @staticmethod
    def _group_tools_for_display(tools: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
        buckets: dict[str, list[dict[str, str]]] = {
            "DESKTOP OS": [],
            "JARVIS": [],
            "BROWSER": [],
            "WORKSPACE & GIT": [],
        }
        for item in tools:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            entry = {"name": name, "description": str(item.get("description") or "").strip()}
            if name.startswith("desktop_"):
                buckets["DESKTOP OS"].append(entry)
            elif name.startswith("jarvis_"):
                buckets["JARVIS"].append(entry)
            elif name.startswith("browser_"):
                buckets["BROWSER"].append(entry)
            else:
                buckets["WORKSPACE & GIT"].append(entry)
        return [(cat, items) for cat, items in buckets.items() if items]

    def _build_tools_popover(self) -> None:
        """Create a sticky hover/click in-island Tools catalog popover (replaces buggy tk_popup)."""
        self.tools_popover = tk.Frame(
            self.frame,
            bg="#1F242E",
            highlightbackground=_PALETTE["arc_border"],
            highlightthickness=1,
        )
        hdr = tk.Frame(self.tools_popover, bg="#252B37")
        hdr.pack(fill="x")
        self._tools_hdr_lbl = tk.Label(
            hdr,
            text="TOOLS & MCP CATALOG",
            bg="#252B37",
            fg=_PALETTE["arc_text"],
            font=(_TECH_FONT, 8, "bold"),
            padx=8,
            pady=5,
        )
        self._tools_hdr_lbl.pack(side="left")
        self._tools_pin_lbl = tk.Label(
            hdr,
            text="Hover · Click to pin",
            bg="#252B37",
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8),
            padx=4,
        )
        self._tools_pin_lbl.pack(side="left")
        self._mk_btn(
            hdr,
            "×",
            self._close_tools_popover,
            bg="#252B37",
            fg=_PALETTE["steel"],
            padx=6,
            pady=1,
            border_color="#252B37",
        ).pack(side="right", padx=2)

        quick_bar = tk.Frame(self.tools_popover, bg="#1F242E")
        quick_bar.pack(fill="x", padx=6, pady=(5, 4))
        for col in range(3):
            quick_bar.columnconfigure(col, weight=1)
        for idx, (lbl, cmd_prompt) in enumerate((
            ("✦ Briefing", "morning briefing"),
            ("📅 Agenda", "what is on my calendar today?"),
            ("🖥 Screen", "what is on my screen?"),
            ("🪟 Windows", "list windows"),
            ("✓ Todos", "list my todos"),
            ("🧹 Free VRAM", "__VRAM__"),
        )):
            r, c = divmod(idx, 3)
            action = (
                (lambda: (self._close_tools_popover(), self._unload_vram()))
                if cmd_prompt == "__VRAM__"
                else (lambda p=cmd_prompt: (self._close_tools_popover(), self._run_quick(p)))
            )
            self._mk_btn(
                quick_bar,
                lbl,
                action,
                bg=_PALETTE["well"],
                fg=_PALETTE["steel"],
                padx=5,
                pady=3,
                border_color="#485364",
            ).grid(row=r, column=c, sticky="ew", padx=2, pady=2)

        list_wrap = tk.Frame(self.tools_popover, bg="#1B2029")

        filter_row = tk.Frame(
            self.tools_popover,
            bg="#151922",
            highlightbackground="#3A4558",
            highlightthickness=1,
        )
        filter_row.pack(fill="x", padx=6, pady=(0, 4))
        self._tools_filter_var = tk.StringVar()
        self._tools_filter_has_hint = True
        self._tools_filter_entry = tk.Entry(
            filter_row,
            textvariable=self._tools_filter_var,
            bg="#151922",
            fg=_PALETTE["muted"],
            insertbackground="#FFFFFF",
            relief="flat",
            bd=0,
            font=(_UI_FONT, 8),
        )
        self._tools_filter_entry.pack(side="left", fill="x", expand=True, padx=6, pady=3)
        self._tools_filter_entry.insert(0, _TOOLS_FILTER_HINT)

        def _on_filter_focus_in(_e: tk.Event[Any] | None = None) -> None:
            if getattr(self, "_tools_filter_has_hint", False):
                self._tools_filter_has_hint = False
                self._tools_filter_entry.delete(0, "end")
                self._tools_filter_entry.configure(fg=_PALETTE["text"])

        def _on_filter_focus_out(_e: tk.Event[Any] | None = None) -> None:
            if not self._tools_filter_entry.get().strip():
                self._tools_filter_has_hint = True
                self._tools_filter_entry.delete(0, "end")
                self._tools_filter_entry.insert(0, _TOOLS_FILTER_HINT)
                self._tools_filter_entry.configure(fg=_PALETTE["muted"])

        def _clear_tools_filter() -> None:
            self._tools_filter_has_hint = True
            self._tools_filter_entry.delete(0, "end")
            self._tools_filter_entry.insert(0, _TOOLS_FILTER_HINT)
            self._tools_filter_entry.configure(fg=_PALETTE["muted"])
            self._populate_tools_popover()

        self._tools_filter_entry.bind("<FocusIn>", _on_filter_focus_in, add="+")
        self._tools_filter_entry.bind("<FocusOut>", _on_filter_focus_out, add="+")
        self._tools_filter_entry.bind("<KeyRelease>", lambda _e: self._populate_tools_popover(), add="+")
        self._mk_btn(
            filter_row,
            "✕",
            _clear_tools_filter,
            bg="#151922",
            fg=_PALETTE["muted"],
            padx=5,
            pady=0,
            border_color="#151922",
        ).pack(side="right", padx=1)

        list_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._tools_canvas = tk.Canvas(
            list_wrap,
            bg="#1B2029",
            highlightthickness=0,
            bd=0,
        )
        self._tools_scroll = tk.Scrollbar(
            list_wrap,
            orient="vertical",
            command=self._tools_canvas.yview,
        )
        self._tools_canvas.configure(yscrollcommand=self._tools_scroll.set)
        self._tools_scroll.pack(side="right", fill="y")
        self._tools_canvas.pack(side="left", fill="both", expand=True)

        self._tools_list_frame = tk.Frame(self._tools_canvas, bg="#1B2029")
        self._tools_canvas_win = self._tools_canvas.create_window((0, 0), window=self._tools_list_frame, anchor="nw")
        self._tools_list_frame.bind(
            "<Configure>",
            lambda _e: self._tools_canvas.configure(scrollregion=self._tools_canvas.bbox("all")),
        )
        self._tools_canvas.bind(
            "<Configure>",
            lambda e: self._tools_canvas.itemconfigure(self._tools_canvas_win, width=max(260, e.width)),
        )
        self._tools_canvas.bind("<MouseWheel>", self._on_tools_mousewheel, add="+")
        self._bind_tools_hover_recursive(self.tools_popover)
        self._populate_tools_popover()

    def _on_tools_mousewheel(self, event: tk.Event[Any]) -> None:
        try:
            delta = int(-1 * (getattr(event, "delta", 0) / 120))
            if delta == 0 and getattr(event, "delta", 0) != 0:
                delta = -1 if event.delta > 0 else 1
            if delta:
                self._tools_canvas.yview_scroll(delta, "units")
        except Exception:
            pass

    def _bind_tools_hover_recursive(self, widget: tk.Widget) -> None:
        try:
            widget.bind("<Enter>", self._on_tools_enter, add="+")
            widget.bind("<Leave>", self._on_tools_leave, add="+")
            for child in widget.winfo_children():
                self._bind_tools_hover_recursive(child)
        except Exception:
            pass

    def _get_tools_filter_query(self) -> str:
        if getattr(self, "_tools_filter_has_hint", True) or not hasattr(self, "_tools_filter_entry"):
            return ""
        raw = self._tools_filter_entry.get().strip()
        if not raw or raw == _TOOLS_FILTER_HINT:
            return ""
        return raw.lower()

    def _populate_tools_popover(self) -> None:
        if not hasattr(self, "_tools_list_frame"):
            return
        tools = self.available_tools or self._default_tools_catalog()
        query = self._get_tools_filter_query()
        filtered = (
            [
                t
                for t in tools
                if query in str(t.get("name") or "").lower()
                or query in str(t.get("description") or "").lower()
            ]
            if query
            else tools
        )
        sig = (
            query,
            tuple((str(t.get("name") or ""), str(t.get("description") or "")) for t in filtered),
        )
        if sig == self._tools_signature and self._tools_list_frame.winfo_children():
            return
        self._tools_signature = sig
        for child in self._tools_list_frame.winfo_children():
            child.destroy()

        grouped = self._group_tools_for_display(filtered)
        if query:
            self._tools_hdr_lbl.configure(text=f"TOOLS ({len(filtered)}/{len(tools)})")
        else:
            self._tools_hdr_lbl.configure(text=f"AVAILABLE TOOLS ({len(tools)})")
        if not grouped:
            tk.Label(
                self._tools_list_frame,
                text="No tools match filter.",
                bg="#1B2029",
                fg=_PALETTE["muted"],
                font=(_UI_FONT, 8),
                padx=8,
                pady=10,
                anchor="w",
            ).pack(fill="x")
        for cat, items in grouped:
            cat_lbl = tk.Label(
                self._tools_list_frame,
                text=f"{cat} · {len(items)}",
                bg="#232935",
                fg=_PALETTE["accent"],
                font=(_TECH_FONT, 7, "bold"),
                anchor="w",
                padx=8,
                pady=3,
            )
            cat_lbl.pack(fill="x", pady=(4, 1))
            for entry in items:
                tname = entry["name"]
                tdesc = entry["description"]
                row = tk.Frame(self._tools_list_frame, bg="#1B2029", cursor="hand2")
                row.pack(fill="x", padx=2, pady=1)
                name_lbl = tk.Label(
                    row,
                    text=tname,
                    bg="#1B2029",
                    fg="#E6F1FF",
                    font=(_MONO_FONT, 8, "bold"),
                    anchor="w",
                    padx=6,
                )
                name_lbl.pack(fill="x")
                if tdesc:
                    desc_lbl = tk.Label(
                        row,
                        text=tdesc,
                        bg="#1B2029",
                        fg=_PALETTE["muted"],
                        font=(_UI_FONT, 8),
                        anchor="w",
                        wraplength=292,
                        justify="left",
                        padx=6,
                    )
                    desc_lbl.pack(fill="x")

                def _on_pick(_e: tk.Event[Any] | None = None, n: str = tname) -> None:
                    self._use_tool_template(n)

                def _hi(bg_c: str, r: tk.Frame = row) -> None:
                    try:
                        r.configure(bg=bg_c)
                        for ch in r.winfo_children():
                            ch.configure(bg=bg_c)
                    except Exception:
                        pass

                for target_w in (row, *row.winfo_children()):
                    target_w.bind("<Button-1>", _on_pick, add="+")
                    target_w.bind("<MouseWheel>", self._on_tools_mousewheel, add="+")
                row.bind("<Enter>", lambda _e, r=row: _hi("#283142", r), add="+")
                row.bind("<Leave>", lambda _e, r=row: _hi("#1B2029", r), add="+")

        self._bind_tools_hover_recursive(self._tools_list_frame)
        try:
            self._tools_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _use_tool_template(self, tool_name: str) -> None:
        hint = self._tool_prompt_hint(tool_name)
        self._close_tools_popover()
        self._clear_placeholder()
        self.entry.delete(0, "end")
        self.entry.insert(0, hint)
        self.entry.focus_set()

    def _cancel_tools_hover_close(self) -> None:
        cid = getattr(self, "_tools_hover_close_id", None)
        if cid:
            self._tools_hover_close_id = None
            try:
                self.root.after_cancel(cid)
            except Exception:
                pass

    def _is_cursor_over_tools(self) -> bool:
        try:
            px, py = self.root.winfo_pointerxy()
            bx = self.tools_btn.winfo_rootx()
            by = self.tools_btn.winfo_rooty()
            bw = self.tools_btn.winfo_width()
            bh = self.tools_btn.winfo_height()
            if bx - 6 <= px <= bx + bw + 6 and by - 14 <= py <= by + bh + 6:
                return True
            if getattr(self, "_tools_popover_visible", False):
                tx = self.tools_popover.winfo_rootx()
                ty = self.tools_popover.winfo_rooty()
                tw = self.tools_popover.winfo_width()
                th = self.tools_popover.winfo_height()
                if tx - 6 <= px <= tx + tw + 6 and ty - 6 <= py <= ty + th + 14:
                    return True
        except Exception:
            return False
        return False

    def _on_tools_enter(self, _e: tk.Event[Any] | None = None) -> None:
        self._cancel_tools_hover_close()
        if not self.expanded or self._closing:
            return
        self._open_tools_popover(pinned=bool(getattr(self, "_tools_pinned", False)))

    def _on_tools_leave(self, _e: tk.Event[Any] | None = None) -> None:
        if getattr(self, "_tools_pinned", False) or not getattr(self, "_tools_popover_visible", False):
            return
        self._cancel_tools_hover_close()
        try:
            self._tools_hover_close_id = self.root.after(220, self._check_tools_hover_leave)
        except Exception:
            pass

    def _check_tools_hover_leave(self) -> None:
        self._tools_hover_close_id = None
        if getattr(self, "_tools_pinned", False) or not getattr(self, "_tools_popover_visible", False):
            return
        if not self._is_cursor_over_tools():
            self._close_tools_popover()

    def _sync_tools_popover_header(self) -> None:
        if hasattr(self, "_tools_pin_lbl"):
            self._tools_pin_lbl.configure(
                text="Pinned · Click Tools or outside to close" if self._tools_pinned else "Hover · Click Tools to pin",
                fg=_PALETTE["accent"] if self._tools_pinned else _PALETTE["muted"],
            )
        if hasattr(self, "tools_btn"):
            self.tools_btn.configure(
                highlightbackground=_PALETTE["arc_border"] if self._tools_popover_visible else "#5A667A"
            )

    def _open_tools_popover(self, *, pinned: bool = False) -> None:
        self._cancel_tools_hover_close()
        self._close_model_popover()
        self._close_apps_popover()
        if not self.expanded:
            self.expand()
        if pinned:
            self._tools_pinned = True
        if getattr(self, "_tools_popover_visible", False):
            self._sync_tools_popover_header()
            self.tools_popover.lift()
            return
        self._populate_tools_popover()
        self._tools_popover_visible = True
        self._sync_tools_popover_header()
        try:
            self.frame.update_idletasks()
            fw = max(480, self.frame.winfo_width() or 900)
            pop_w = 340
            bx_rel = max(12, self.tools_btn.winfo_rootx() - self.frame.winfo_rootx())
            by_rel = max(120, self.tools_btn.winfo_rooty() - self.frame.winfo_rooty())
            pop_h = min(304, max(190, by_rel - 64))
            px = max(12, min(bx_rel, fw - pop_w - 12))
            py = max(62, by_rel - pop_h - 4)
            self.tools_popover.configure(width=pop_w, height=pop_h)
            self.tools_popover.pack_propagate(False)
            self.tools_popover.place(in_=self.frame, x=px, y=py, width=pop_w, height=pop_h)
        except Exception:
            self.tools_popover.place(in_=self.frame, x=24, y=180)
        self.tools_popover.lift()

    def _toggle_tools_popover(self) -> None:
        if getattr(self, "_tools_popover_visible", False) and getattr(self, "_tools_pinned", False):
            self._close_tools_popover()
            return
        self._open_tools_popover(pinned=True)

    def _close_tools_popover(self) -> None:
        self._cancel_tools_hover_close()
        self._tools_pinned = False
        if getattr(self, "_tools_popover_visible", False):
            self._tools_popover_visible = False
            try:
                self.tools_popover.place_forget()
            except Exception:
                pass
        self._sync_tools_popover_header()

    def _populate_model_menu(self) -> None:
        self.model_menu.delete(0, "end")
        for spec, label in self._collect_model_choices():
            prefix = "● " if spec == self.active_model_name else "   "
            self.model_menu.add_command(
                label=f"{prefix}{label}",
                command=lambda s=spec, d=label: self._select_model(s, d),
            )

    def _build_chat_panel(self) -> None:
        self.chat_panel = tk.Frame(self.panel_host, bg=_PALETTE["bg"])

        # Right Column (Agenda + interactive Todos) & Middle Column (Executive Summary + Gmail)
        right_col = tk.Frame(self.chat_panel, bg=_PALETTE["bg"], width=238)
        right_col.pack(side="right", fill="y", padx=(8, 0))
        right_col.pack_propagate(False)

        mid_col = tk.Frame(self.chat_panel, bg=_PALETTE["bg"], width=232)
        mid_col.pack(side="right", fill="y", padx=(8, 0))
        mid_col.pack_propagate(False)

        # Left Column (Chat Well #14171C + Omni Composer)
        left_col = tk.Frame(
            self.chat_panel,
            bg=_PALETTE["well"],
            highlightbackground="#4E5869",
            highlightthickness=1,
        )
        left_col.pack(side="left", fill="both", expand=True)

        # Collapsible Reasoning Steps Header
        rs_wrap = tk.Frame(
            left_col,
            bg=_PALETTE["card"],
            highlightbackground="#5A6678",
            highlightthickness=1,
        )
        rs_wrap.pack(side="top", fill="x", padx=10, pady=(8, 4))

        self.reasoning_btn = tk.Button(
            rs_wrap,
            text="▾  Reasoning steps",
            command=self._toggle_reasoning,
            bg=_PALETTE["card"],
            fg=_PALETTE["text"],
            activebackground=_PALETTE["card_hi"],
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            font=(_UI_FONT, 9, "bold"),
            padx=10,
            pady=5,
            cursor="hand2",
            anchor="w",
        )
        self.reasoning_btn.pack(side="left", fill="x", expand=True)

        self.agent_btn = tk.Button(
            rs_wrap,
            text="✦ Auto ▾",
            command=self._open_agent_menu,
            bg=_PALETTE["well"],
            fg=_PALETTE["accent"],
            activebackground=_PALETTE["pill_active_bg"],
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            font=(_UI_FONT, 9, "bold"),
            padx=8,
            pady=3,
            cursor="hand2",
        )
        self.agent_btn.pack(side="right", padx=(4, 6), pady=4)

        self.tools_called_badge = tk.Label(
            rs_wrap,
            text="0 tools called",
            bg=_PALETTE["well"],
            fg=_PALETTE["arc_text"],
            font=(_MONO_FONT, 8, "bold"),
            padx=9,
            pady=3,
        )
        self.tools_called_badge.pack(side="right", padx=(4, 2), pady=4)

        self.reasoning_detail_lbl = tk.Label(
            left_col,
            text=self._last_reasoning_text,
            bg=_PALETTE["well_lo"],
            fg=_PALETTE["muted"],
            font=(_MONO_FONT, 8),
            anchor="w",
            justify="left",
            wraplength=500,
            padx=12,
            pady=5,
        )

        # Omni Composer (packed BOTTOM so transcript never pushes it below expanded card height)
        self.input_row = tk.Frame(
            left_col,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#7E8DA2",
            highlightthickness=1,
        )
        self.input_row.pack(side="bottom", fill="x", padx=10, pady=(4, 8))

        entry_top = tk.Frame(self.input_row, bg=_PALETTE["panel_mid"])
        entry_top.pack(fill="x", padx=10, pady=(7, 3))

        self.entry = tk.Entry(
            entry_top,
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["muted"],
            insertbackground="#FFFFFF",
            relief="flat",
            bd=0,
            font=(_UI_FONT, 10),
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(2, 6), pady=2)
        self.entry.insert(0, _PLACEHOLDER)
        self.entry.bind("<Button-1>", self._focus_entry)
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<KeyPress>", self._on_entry_key)
        self.entry.bind("<FocusOut>", self._restore_placeholder)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.input_row.bind("<Button-1>", self._focus_entry)

        self.composer_stop_btn = self._mk_btn(
            entry_top,
            "■ Stop Aura",
            self.stop_active,
            bg=_PALETTE["gunmetal"],
            fg=_PALETTE["steel"],
            hover_bg=_PALETTE["danger_bg"],
            hover_fg=_PALETTE["danger_fg"],
            padx=8,
            pady=2,
            border_color="#5A667A",
        )
        self.composer_stop_btn.pack(side="right", padx=(4, 0))

        self.mic_btn = self._mk_btn(
            entry_top,
            "🎙 Mic",
            self.toggle_mic,
            bg=_PALETTE["gunmetal"],
            fg=_PALETTE["steel"],
            hover_bg=_PALETTE["titanium"],
            hover_fg=_PALETTE["accent"],
            padx=8,
            pady=2,
            border_color="#5A667A",
        )
        self.mic_btn.pack(side="right")

        chips_row = tk.Frame(self.input_row, bg=_PALETTE["panel_mid"])
        chips_row.pack(fill="x", padx=8, pady=(2, 7))

        self.tools_btn = self._mk_btn(
            chips_row,
            "▴ Tools",
            self._toggle_tools_popover,
            bg=_PALETTE["card"],
            fg=_PALETTE["steel"],
            padx=6,
            pady=2,
            border_color="#5A667A",
        )
        self.tools_btn.bind("<Enter>", self._on_tools_enter, add="+")
        self.tools_btn.bind("<Leave>", self._on_tools_leave, add="+")
        self.tools_btn.pack(side="left", padx=(0, 3))

        for c_lbl, c_cmd in (
            ("DeepSearch", lambda: self._run_quick("research online about ")),
            ("Think", lambda: self._run_quick("@planner analyze step-by-step: ")),
            ("Screen", lambda: self._run_quick("what is on my screen?")),
            ("Git", lambda: self._run_quick("git status")),
            ("Attach", self._attach_file_path),
        ):
            self._mk_btn(
                chips_row,
                c_lbl,
                c_cmd,
                bg=_PALETTE["card"],
                fg="#DCE4EE",
                hover_bg=_PALETTE["card_hi"],
                hover_fg=_PALETTE["arc_text"],
                padx=4,
                pady=2,
                border_color="#5A667A",
            ).pack(side="left", padx=(0, 2))

        self.send_btn = self._mk_btn(
            chips_row,
            "Send",
            self._on_send_or_stop,
            bg=_PALETTE["cta_bg"],
            fg="#F2F8FF",
            hover_bg=_PALETTE["cta_hover"],
            hover_fg="#FFFFFF",
            padx=10,
            pady=2,
            border_color=_PALETTE["arc_border"],
        )
        self.send_btn.pack(side="right")

        # Chat Response Well
        self.output = tk.Text(
            left_col,
            wrap="word",
            height=8,
            bg=_PALETTE["well"],
            fg=_PALETTE["text"],
            insertbackground="#FFFFFF",
            relief="flat",
            bd=0,
            font=(_UI_FONT, 10),
            padx=12,
            pady=6,
            spacing1=3,
            spacing3=5,
            cursor="arrow",
        )
        self.output.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        self.output.tag_configure("welcome", foreground=_PALETTE["steel"], font=(_UI_FONT, 10))
        self.output.tag_configure("you_tag", foreground=_PALETTE["accent"], font=(_MONO_FONT, 9, "bold"))
        self.output.tag_configure("you_body", foreground="#FFFFFF", font=(_UI_FONT, 10, "bold"))
        self.output.tag_configure("jarvis_tag", foreground=_PALETTE["accent"], font=(_MONO_FONT, 9, "bold"))
        self.output.tag_configure("ultron_tag", foreground="#93C5FD", font=(_MONO_FONT, 9, "bold"))
        self.output.tag_configure("megatron_tag", foreground="#F87171", font=(_MONO_FONT, 9, "bold"))
        self.output.tag_configure("ans_body", foreground="#E8EDF4", font=(_UI_FONT, 10))
        self.output.tag_configure("code_blk", background=_PALETTE["code_bg"], foreground="#EAF2FA", font=(_MONO_FONT, 9))
        self.output.tag_configure("meta_note", foreground=_PALETTE["muted"], font=(_MONO_FONT, 8))
        self.output.tag_configure("steer_tag", foreground="#FBBF24", font=(_UI_FONT, 9, "bold"))
        self.output.tag_configure("err_body", foreground=_PALETTE["danger_fg"], font=(_UI_FONT, 10))
        self.output.insert(
            "end",
            "◈ AURA  Ready in vibranium Ultron HUD — pick @auto/@planner/@coder, speak, or tap Summarize my day.\n",
            "welcome",
        )
        self.output.configure(state="disabled")

        # --- Middle Column: Executive Summary + Gmail ---
        sum_card = tk.Frame(
            mid_col,
            bg=_PALETTE["card"],
            highlightbackground="#627185",
            highlightthickness=1,
        )
        sum_card.pack(side="top", fill="x", pady=(0, 8))

        self._build_section_header(sum_card, "EXECUTIVE SUMMARY")

        self._mk_btn(
            sum_card,
            "Summarize my day",
            lambda: self._run_quick("morning briefing"),
            bg=_PALETTE["cta_bg"],
            fg="#F2F8FF",
            hover_bg=_PALETTE["cta_hover"],
            hover_fg="#FFFFFF",
            padx=12,
            pady=6,
            border_color=_PALETTE["arc_border"],
        ).pack(fill="x", padx=12, pady=(2, 8))

        tk.Label(
            sum_card,
            text="LIVE BRIEFING",
            bg=_PALETTE["card"],
            fg=_PALETTE["muted"],
            font=(_UI_FONT, 8, "bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 3))

        self.briefing_list_frame = tk.Frame(sum_card, bg=_PALETTE["card"])
        self.briefing_list_frame.pack(fill="x", padx=12, pady=(0, 6))
        self._render_briefing(["No workspace summary synced yet."])

        gmail_card = tk.Frame(
            mid_col,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
        )
        gmail_card.pack(fill="both", expand=True)
        self._build_section_header(gmail_card, "GMAIL", right_btn=("Inbox", lambda: self._run_quick("check my unread emails")))

        self.gmail_list_frame = tk.Frame(gmail_card, bg=_PALETTE["panel_mid"])
        self.gmail_list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self._render_gmail([])

        # --- Right Column: Agenda + Interactive Todos + Live Agent Progress ---
        agenda_card = tk.Frame(
            right_col,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
        )
        agenda_card.pack(side="top", fill="x", pady=(0, 8))
        now = datetime.now()
        self._build_section_header(
            agenda_card,
            "AGENDA",
            right_text=now.strftime("%a %d %b"),
            right_btn=("Cal", self._toggle_agenda_calendar),
        )

        self.agenda_list_frame = tk.Frame(agenda_card, bg=_PALETTE["panel_mid"])
        self.agenda_list_frame.pack(fill="x", padx=12, pady=(0, 6))
        self._render_agenda([])

        # Optional collapsible Monthly Calendar inside Agenda card
        self.agenda_cal_frame = tk.Frame(agenda_card, bg=_PALETTE["well"])
        for col_idx, wd in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            self.agenda_cal_frame.columnconfigure(col_idx, weight=1)
            tk.Label(
                self.agenda_cal_frame, text=wd, bg=_PALETTE["well"], fg=_PALETTE["muted"], font=(_MONO_FONT, 8, "bold")
            ).grid(row=0, column=col_idx, pady=(2, 2))
        for r_idx, week in enumerate(calendar.monthcalendar(now.year, now.month), start=1):
            for c_idx, day in enumerate(week):
                if day == 0:
                    continue
                is_today = day == now.day
                day_lbl = tk.Label(
                    self.agenda_cal_frame,
                    text=str(day),
                    bg=_PALETTE["cta_bg"] if is_today else _PALETTE["well"],
                    fg="#F2F8FF" if is_today else _PALETTE["steel"],
                    font=(_MONO_FONT, 8, "bold" if is_today else "normal"),
                    width=2,
                    cursor="hand2",
                )
                day_lbl.grid(row=r_idx, column=c_idx, padx=1, pady=1)
                date_str = f"{now.strftime('%B')} {day}, {now.year}"
                day_lbl.bind("<Button-1>", lambda _e, ds=date_str: self._run_quick(f"check my calendar for {ds}"))

        # Interactive Todos Card + Live Active Agent & Completion Progress below Todos
        todos_card = tk.Frame(
            right_col,
            bg=_PALETTE["panel_mid"],
            highlightbackground="#556173",
            highlightthickness=1,
        )
        todos_card.pack(fill="both", expand=True)

        todo_hdr = tk.Frame(todos_card, bg=_PALETTE["panel_mid"])
        todo_hdr.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(todo_hdr, text="◆", bg=_PALETTE["panel_mid"], fg=_PALETTE["accent"], font=(_UI_FONT, 9)).pack(
            side="left", padx=(0, 6)
        )
        tk.Label(
            todo_hdr, text="TODOS", bg=_PALETTE["panel_mid"], fg="#E8EDF4", font=(_TECH_FONT, 9, "bold")
        ).pack(side="left")
        self.todo_count_lbl = tk.Label(
            todo_hdr, text="0 of 0 done", bg=_PALETTE["panel_mid"], fg=_PALETTE["steel"], font=(_UI_FONT, 8, "bold")
        )
        self.todo_count_lbl.pack(side="right")

        self.todo_prog_canvas = tk.Canvas(
            todos_card, height=5, bg=_PALETTE["well"], highlightthickness=0
        )
        self.todo_prog_canvas.pack(fill="x", padx=12, pady=(0, 6))
        self._todo_prog_fill = self.todo_prog_canvas.create_rectangle(0, 0, 0, 5, fill=_PALETTE["accent"], outline="")

        self.todo_list_frame = tk.Frame(todos_card, bg=_PALETTE["panel_mid"])
        self.todo_list_frame.pack(fill="x", padx=10, pady=(0, 4))
        self._todo_vars = []
        self._render_todos(self._todo_items)

        todo_add_row = tk.Frame(todos_card, bg=_PALETTE["panel_mid"])
        todo_add_row.pack(fill="x", padx=12, pady=(2, 4))
        self.todo_add_entry = tk.Entry(
            todo_add_row,
            bg=_PALETTE["well"],
            fg=_PALETTE["text"],
            insertbackground=_PALETTE["accent"],
            relief="flat",
            font=(_UI_FONT, 9),
        )
        self.todo_add_entry.pack(side="left", fill="x", expand=True, ipady=2, padx=(0, 4))
        self.todo_add_entry.bind("<Return>", lambda _e: self._add_todo_item())
        self._mk_btn(
            todo_add_row,
            "+ Add",
            self._add_todo_item,
            bg=_PALETTE["card_hi"],
            fg="#DCE4EE",
            padx=8,
            pady=1,
            border_color="#67758A",
        ).pack(side="right")

        # Divider + Active Agent Execution & Completion Progress under Todos
        tk.Frame(todos_card, height=1, bg="#465163").pack(fill="x", padx=12, pady=(8, 6))

        ap_hdr = tk.Frame(todos_card, bg=_PALETTE["panel_mid"])
        ap_hdr.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(ap_hdr, text="◆", bg=_PALETTE["panel_mid"], fg=_PALETTE["accent"], font=(_UI_FONT, 8)).pack(
            side="left", padx=(0, 5)
        )
        tk.Label(
            ap_hdr, text="AGENT PIPELINE", bg=_PALETTE["panel_mid"], fg="#E8EDF4", font=(_TECH_FONT, 8, "bold")
        ).pack(side="left")
        self.agent_prog_pct_lbl = tk.Label(
            ap_hdr, text="Ready · 0%", bg=_PALETTE["panel_mid"], fg=_PALETTE["arc_text"], font=(_MONO_FONT, 8, "bold")
        )
        self.agent_prog_pct_lbl.pack(side="right")

        self.agent_status_lbl = tk.Label(
            todos_card,
            text="✦ Auto (@planner → @coder)",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["steel"],
            font=(_UI_FONT, 8, "bold"),
            anchor="w",
        )
        self.agent_status_lbl.pack(fill="x", padx=12, pady=(0, 4))

        self.agent_prog_canvas = tk.Canvas(
            todos_card, height=6, bg=_PALETTE["well"], highlightthickness=0
        )
        self.agent_prog_canvas.pack(fill="x", padx=12, pady=(0, 8))
        self._agent_prog_fill = self.agent_prog_canvas.create_rectangle(
            0, 0, 12, 6, fill=_PALETTE["accent"], outline=""
        )

    def _build_section_header(
        self,
        parent: tk.Frame,
        title: str,
        *,
        right_text: str = "",
        right_btn: tuple[str, Callable[[], Any]] | None = None,
    ) -> None:
        hdr = tk.Frame(parent, bg=parent.cget("bg"))
        hdr.pack(fill="x", padx=12, pady=(8, 5))
        tk.Label(hdr, text="◆", bg=parent.cget("bg"), fg=_PALETTE["accent"], font=(_UI_FONT, 9)).pack(
            side="left", padx=(0, 6)
        )
        tk.Label(
            hdr, text=title, bg=parent.cget("bg"), fg="#E8EDF4", font=(_TECH_FONT, 9, "bold")
        ).pack(side="left")
        if right_btn is not None:
            btn_lbl, btn_cmd = right_btn
            self._mk_btn(
                hdr,
                btn_lbl,
                btn_cmd,
                bg=_PALETTE["well"],
                fg=_PALETTE["arc_text"],
                padx=7,
                pady=1,
                border_color="#4D5767",
            ).pack(side="right", padx=(4, 0))
        if right_text:
            tk.Label(hdr, text=right_text, bg=parent.cget("bg"), fg=_PALETTE["muted"], font=(_UI_FONT, 8, "bold")).pack(
                side="right"
            )

    def _build_agents_panel(self) -> None:
        self.agents_panel = tk.Frame(
            self.panel_host, bg=_PALETTE["well"], highlightbackground="#4E5869", highlightthickness=1
        )
        pipe_row = tk.Frame(self.agents_panel, bg=_PALETTE["gunmetal"])
        pipe_row.pack(fill="x", padx=12, pady=(12, 8))
        tk.Label(
            pipe_row, text="SEQUENTIAL PIPELINE:", bg=_PALETTE["gunmetal"], fg=_PALETTE["muted"], font=(_TECH_FONT, 9, "bold")
        ).pack(side="left", padx=(10, 8), pady=6)
        for i, (role_id, role_lbl) in enumerate(_PIPELINE_NODES):
            if i > 0:
                tk.Label(pipe_row, text="→", bg=_PALETTE["gunmetal"], fg="#64748B", font=(_UI_FONT, 9, "bold")).pack(
                    side="left"
                )
            node = tk.Label(
                pipe_row,
                text=role_lbl,
                bg=_PALETTE["panel_mid"],
                fg=_PALETTE["steel"],
                font=(_MONO_FONT, 9, "bold"),
                padx=9,
                pady=3,
                cursor="hand2",
            )
            node.pack(side="left", padx=4, pady=5)
            node.bind("<Button-1>", lambda _e, r=role_id: self.select_agent(r, switch_to_chat=True))
            self._pipeline_pills[role_id] = node

        agents_grid = tk.Frame(self.agents_panel, bg=_PALETTE["well"])
        agents_grid.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        agents_grid.columnconfigure(0, weight=1)
        agents_grid.columnconfigure(1, weight=1)

        agent_items: list[tuple[str, str, str]] = [
            ("auto", "✦ Auto Orchestrator", "Sequential multi-stage handoffs · safe 1-by-1"),
        ] + [
            (k, f"@{k} · {v['label']}", v["description"])
            for k, v in SPECIALIST_AGENTS.items()
        ]
        for idx, (akey, atitle, adesc) in enumerate(agent_items):
            r, c = divmod(idx, 2)
            card = tk.Frame(
                agents_grid, bg=_PALETTE["gunmetal"], highlightbackground="#4E5869", highlightthickness=1, cursor="hand2"
            )
            card.grid(row=r, column=c, sticky="nsew", padx=5, pady=5)
            agents_grid.rowconfigure(r, weight=1)
            lbl_title = tk.Label(
                card, text=atitle, bg=_PALETTE["gunmetal"], fg=_PALETTE["text"], font=(_UI_FONT, 9, "bold"), anchor="w", cursor="hand2"
            )
            lbl_title.pack(fill="x", padx=10, pady=(6, 2))
            lbl_desc = tk.Label(
                card,
                text=adesc,
                bg=_PALETTE["gunmetal"],
                fg=_PALETTE["steel"],
                font=(_UI_FONT, 8),
                anchor="w",
                wraplength=390,
                justify="left",
                cursor="hand2",
            )
            lbl_desc.pack(fill="x", padx=10, pady=(0, 6))
            for w in (card, lbl_title, lbl_desc):
                w.bind("<Button-1>", lambda _e, role=akey: self.select_agent(role, switch_to_chat=True))
            self._agent_cards[akey] = (card, lbl_title, lbl_desc)

    def _build_deck_panel(self) -> None:
        self.deck_panel = tk.Frame(self.panel_host, bg=_PALETTE["bg"])
        self.deck_panel.columnconfigure(0, weight=1)
        self.deck_panel.columnconfigure(1, weight=1)
        self.deck_panel.rowconfigure(0, weight=1)
        self.deck_panel.rowconfigure(1, weight=1)

        # Card 1: Audio, Voice Persona & Media Transport
        media_card = tk.Frame(
            self.deck_panel, bg=_PALETTE["panel_mid"], highlightbackground="#556173", highlightthickness=1
        )
        media_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self._build_section_header(media_card, "AUDIO & VOICE PERSONA")
        media_btns = tk.Frame(media_card, bg=_PALETTE["panel_mid"])
        media_btns.pack(fill="x", padx=12, pady=(4, 10))
        for m_lbl, m_cmd in (("⏮", "previous track"), ("⏯", "play pause media"), ("⏭", "next track"), ("🔇", "mute audio")):
            self._mk_btn(
                media_btns,
                m_lbl,
                lambda p=m_cmd: self._run_quick(p),
                bg=_PALETTE["well"],
                fg="#E8EDF4",
                padx=10,
                pady=8,
                border_color="#485364",
            ).pack(side="left", expand=True, fill="x", padx=3)
        self._mk_btn(
            media_card,
            "Cycle Jarvis / Ultron / Megatron",
            self._cycle_persona,
            bg=_PALETTE["pill_active_bg"],
            fg=_PALETTE["arc_text"],
            padx=10,
            pady=6,
            border_color=_PALETTE["arc_border"],
        ).pack(fill="x", padx=12, pady=(2, 10))

        # Card 2: Focus Sprint Timer
        focus_card = tk.Frame(
            self.deck_panel, bg=_PALETTE["panel_mid"], highlightbackground="#556173", highlightthickness=1
        )
        focus_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        self._build_section_header(focus_card, "FOCUS SPRINT TIMER")

        focus_mid = tk.Frame(focus_card, bg=_PALETTE["panel_mid"])
        focus_mid.pack(fill="x", padx=12, pady=4)
        self.focus_canvas = tk.Canvas(focus_mid, width=54, height=54, bg=_PALETTE["panel_mid"], highlightthickness=0)
        self.focus_canvas.pack(side="left", padx=(0, 12))
        self.focus_canvas.create_oval(5, 5, 49, 49, outline="#1A202C", width=4)
        self._focus_arc = self.focus_canvas.create_arc(
            5, 5, 49, 49, start=90, extent=-359, style="arc", outline=_PALETTE["accent"], width=4
        )
        self.focus_time_lbl = tk.Label(
            focus_mid, text="25:00", bg=_PALETTE["panel_mid"], fg="#E8EDF4", font=(_MONO_FONT, 16, "bold")
        )
        self.focus_time_lbl.pack(side="left")

        self.focus_toggle_btn = self._mk_btn(
            focus_mid,
            "Start",
            self._toggle_focus_timer,
            bg=_PALETTE["cta_bg"],
            fg="#F2F8FF",
            hover_bg=_PALETTE["cta_hover"],
            hover_fg="#FFFFFF",
            padx=14,
            pady=5,
            border_color=_PALETTE["arc_border"],
        )
        self.focus_toggle_btn.pack(side="right")

        preset_row = tk.Frame(focus_card, bg=_PALETTE["panel_mid"])
        preset_row.pack(fill="x", padx=12, pady=(8, 10))
        for mins in (5, 15, 25, 50):
            self._mk_btn(
                preset_row,
                f"{mins}m",
                lambda m=mins: self._set_focus_minutes(m),
                bg=_PALETTE["well"],
                fg=_PALETTE["steel"],
                padx=8,
                pady=4,
                border_color="#485364",
            ).pack(side="left", expand=True, fill="x", padx=2)
        self._mk_btn(
            preset_row,
            "Reset",
            self._reset_focus_timer,
            bg=_PALETTE["danger_bg"],
            fg=_PALETTE["danger_fg"],
            padx=8,
            pady=4,
            border_color="#6E3342",
        ).pack(side="left", padx=(4, 0))

        # Card 3: System Telemetry & GPU
        sys_card = tk.Frame(
            self.deck_panel, bg=_PALETTE["panel_mid"], highlightbackground="#556173", highlightthickness=1
        )
        sys_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        self._build_section_header(sys_card, "SYSTEM TELEMETRY & GPU")
        self.deck_tel_lbl = tk.Label(
            sys_card,
            text="Sampling system telemetry...",
            bg=_PALETTE["panel_mid"],
            fg=_PALETTE["text"],
            font=(_MONO_FONT, 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.deck_tel_lbl.pack(fill="x", padx=12, pady=(4, 10))
        pwr_row = tk.Frame(sys_card, bg=_PALETTE["panel_mid"])
        pwr_row.pack(fill="x", padx=12, pady=(0, 10))
        self._mk_btn(
            pwr_row, "Free VRAM", self._unload_vram, bg=_PALETTE["danger_bg"], fg=_PALETTE["danger_fg"], padx=10, pady=6, border_color="#6E3342"
        ).pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._mk_btn(
            pwr_row, "Start Ollama", self._wake_ollama, bg=_PALETTE["pill_active_bg"], fg=_PALETTE["arc_text"], padx=10, pady=6, border_color=_PALETTE["arc_border"]
        ).pack(side="left", expand=True, fill="x", padx=(5, 0))

        # Card 4: Window Snap & Quick Tools
        win_card = tk.Frame(
            self.deck_panel, bg=_PALETTE["panel_mid"], highlightbackground="#556173", highlightthickness=1
        )
        win_card.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(6, 0))
        self._build_section_header(win_card, "DESKTOP CONTROL & WORKFLOWS")
        win_grid = tk.Frame(win_card, bg=_PALETTE["panel_mid"])
        win_grid.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        win_grid.columnconfigure(0, weight=1)
        win_grid.columnconfigure(1, weight=1)
        for idx, (w_lbl, w_cmd) in enumerate((
            ("◧ Snap Left", "snap active window left"),
            ("◨ Snap Right", "snap active window right"),
            ("🗖 Maximize", "maximize active window"),
            ("✉ Unread Gmail", "check my unread emails"),
        )):
            r, c = divmod(idx, 2)
            self._mk_btn(
                win_grid,
                w_lbl,
                lambda p=w_cmd: self._run_quick(p),
                bg=_PALETTE["well"],
                fg=_PALETTE["text"],
                padx=8,
                pady=6,
                border_color="#485364",
            ).grid(row=r, column=c, sticky="nsew", padx=3, pady=3)

    @staticmethod
    def _mk_btn(
        parent: tk.Widget,
        text: str,
        cmd: Callable[[], Any],
        *,
        bg: str,
        fg: str,
        hover_bg: str | None = None,
        hover_fg: str | None = None,
        padx: int = 10,
        pady: int = 4,
        border_color: str | None = None,
    ) -> tk.Button:
        hbg = hover_bg or _PALETTE["chip_hover"]
        hfg = hover_fg or "#FFFFFF"
        btn = tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=bg,
            fg=fg,
            activebackground=hbg,
            activeforeground=hfg,
            relief="flat",
            bd=0,
            highlightthickness=1 if border_color else 0,
            highlightbackground=border_color or bg,
            font=(_UI_FONT, 9, "bold"),
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        btn.bind("<Enter>", lambda _e, b=btn, c=hbg, f=hfg: b.configure(bg=c, fg=f))
        btn.bind("<Leave>", lambda _e, b=btn, c=bg, f=fg: b.configure(bg=c, fg=f))
        return btn

    @staticmethod
    def _draw_switch(canvas: tk.Canvas, active: bool, bg_hex: str | None = None) -> Any:
        canvas.delete("all")
        photo = _render_switch_photo(active=active, bg_hex=bg_hex or _PALETTE["gunmetal"])
        if photo is not None:
            canvas.create_image(22, 12, image=photo)
            return photo
        canvas.create_oval(1, 2, 21, 22, fill=_PALETTE["well"], outline="#4A5464")
        canvas.create_oval(20, 2, 40, 22, fill=_PALETTE["well"], outline="#4A5464")
        canvas.create_rectangle(11, 2, 31, 22, fill=_PALETTE["well"], outline="")
        canvas.create_line(11, 2, 31, 2, fill="#4A5464")
        canvas.create_line(11, 22, 31, 22, fill="#4A5464")
        if active:
            canvas.create_oval(21, 4, 37, 20, fill=_PALETTE["accent"], outline="#B4E1F8")
        else:
            canvas.create_oval(4, 4, 20, 20, fill="#6E6A60", outline="#8B867C")
        return None

    def _schedule_ui(self, fn: Callable[[], None]) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _focus_entry(self, _e: tk.Event[Any] | None = None) -> None:
        try:
            self.root.focus_force()
            self.entry.focus_set()
        except Exception:
            pass
        self._clear_placeholder()

    def _open_tools_menu(self) -> None:
        self._toggle_tools_popover()

    def _open_agent_menu(self) -> None:
        self._close_model_popover()
        self._close_tools_popover()
        self._popup_open = True
        try:
            bx = self.agent_btn.winfo_rootx()
            by = self.agent_btn.winfo_rooty() + max(24, self.agent_btn.winfo_height() + 2)
            self.agent_menu.tk_popup(bx, by)
        finally:
            self._popup_open = False
            self._last_expand_time = time.monotonic()
            try:
                self.agent_menu.grab_release()
            except Exception:
                pass

    def _open_model_menu(self) -> None:
        self._close_apps_popover()
        self._close_tools_popover()
        if self.expanded:
            if getattr(self, "_model_popover_visible", False):
                self._close_model_popover()
                return
            self._populate_model_popover()
            self._model_popover_visible = True
            try:
                bx = max(12, self.model_btn.winfo_rootx() - self.frame.winfo_rootx())
            except Exception:
                bx = 76
            self.model_popover.place(in_=self.frame, x=bx, y=64)
            self.model_popover.lift()
            return
        self._popup_open = True
        try:
            self._populate_model_menu()
            bx = self.model_btn.winfo_rootx()
            by = self.model_btn.winfo_rooty() + max(28, self.model_btn.winfo_height() + 4)
            self.model_menu.tk_popup(bx, by)
        finally:
            self._popup_open = False
            self._last_expand_time = time.monotonic()
            try:
                self.model_menu.grab_release()
            except Exception:
                pass

    def _select_model(self, model_spec: str, display_label: str) -> None:
        self.active_model_name = model_spec
        self._update_header()

        def _job() -> None:
            try:
                st = self.bridge.set_model(model_spec)
                self._schedule_ui(lambda: self._apply_status(st))
                self._schedule_ui(lambda: self._append_rich([(f"Switched active model to {display_label}.\n", "meta_note")]))
            except Exception as exc:
                self._schedule_ui(lambda: self._append_rich([(f"Model switch error: {exc}\n", "err_body")]))

        threading.Thread(target=_job, daemon=True).start()

    def _on_header_pill_click(self, label: str, role_target: str) -> None:
        if label == "@gmail":
            if not self.expanded:
                self.expand()
            self._run_quick("check my unread emails")
            return
        if label == "@calendar":
            if not self.expanded:
                self.expand()
            self._run_quick("what is on my calendar today?")
            return
        self.select_agent(role_target, switch_to_chat=True)

    def _toggle_reasoning(self) -> None:
        self._reasoning_open = not self._reasoning_open
        if self._reasoning_open:
            self.reasoning_btn.configure(text="▴  Reasoning steps")
            self.reasoning_detail_lbl.pack(side="top", fill="x", padx=12, pady=(0, 6), before=self.output)
        else:
            self.reasoning_btn.configure(text="▾  Reasoning steps")
            self.reasoning_detail_lbl.pack_forget()

    def _toggle_agenda_calendar(self) -> None:
        self._agenda_cal_visible = not self._agenda_cal_visible
        if self._agenda_cal_visible:
            self.agenda_list_frame.pack_forget()
            self.agenda_cal_frame.pack(fill="x", padx=12, pady=(0, 8))
        else:
            self.agenda_cal_frame.pack_forget()
            self.agenda_list_frame.pack(fill="x", padx=12, pady=(0, 8))

    def _render_briefing(self, lines: list[str]) -> None:
        if not hasattr(self, "briefing_list_frame"):
            return
        for child in self.briefing_list_frame.winfo_children():
            child.destroy()
        for line in (lines or ["No workspace summary yet."])[:4]:
            row = tk.Frame(self.briefing_list_frame, bg=_PALETTE["card"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text="◆", bg=_PALETTE["card"], fg=_PALETTE["arc_border"], font=(_UI_FONT, 8)).pack(
                side="left", anchor="n", padx=(0, 7)
            )
            tk.Label(
                row,
                text=line,
                bg=_PALETTE["card"],
                fg="#E8EDF4",
                font=(_UI_FONT, 9),
                anchor="w",
                wraplength=196,
                justify="left",
            ).pack(side="left", fill="x")

    def _render_gmail(self, messages: list[dict[str, Any]]) -> None:
        if not hasattr(self, "gmail_list_frame"):
            return
        for child in self.gmail_list_frame.winfo_children():
            child.destroy()
        if not messages:
            tk.Label(
                self.gmail_list_frame,
                text="Gmail not connected — click Apps in the top-right to link Gmail.",
                bg=_PALETTE["panel_mid"],
                fg=_PALETTE["muted"],
                font=(_UI_FONT, 8),
                wraplength=196,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(2, 6))
            return
        for msg in messages[:3]:
            sender = str(msg.get("sender") or "Gmail")
            ts = str(msg.get("time") or "")
            subj = str(msg.get("subject") or "")
            item = tk.Frame(
                self.gmail_list_frame,
                bg=_PALETTE["well"],
                highlightbackground="#363E4D",
                highlightthickness=1,
            )
            item.pack(fill="x", pady=(0, 6))
            top = tk.Frame(item, bg=_PALETTE["well"])
            top.pack(fill="x", padx=10, pady=(6, 1))
            tk.Label(top, text=sender, bg=_PALETTE["well"], fg=_PALETTE["text"], font=(_UI_FONT, 9, "bold")).pack(
                side="left"
            )
            tk.Label(top, text=ts, bg=_PALETTE["well"], fg=_PALETTE["muted"], font=(_UI_FONT, 8)).pack(side="right")
            tk.Label(
                item,
                text=subj,
                bg=_PALETTE["well"],
                fg=_PALETTE["steel"],
                font=(_UI_FONT, 9),
                anchor="w",
                wraplength=186,
                justify="left",
            ).pack(fill="x", padx=10, pady=(0, 6))

    def _render_agenda(self, events: list[dict[str, Any]]) -> None:
        if not hasattr(self, "agenda_list_frame"):
            return
        for child in self.agenda_list_frame.winfo_children():
            child.destroy()
        if not events:
            tk.Label(
                self.agenda_list_frame,
                text="No events scheduled today. Connect Calendar in Apps or ask AURA.",
                bg=_PALETTE["panel_mid"],
                fg=_PALETTE["muted"],
                font=(_UI_FONT, 8),
                wraplength=196,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(2, 4))
            return
        for ev_item in events[:3]:
            when = str(ev_item.get("start_time") or "Today")
            title = str(ev_item.get("title") or "(Untitled event)")
            ev = tk.Frame(self.agenda_list_frame, bg=_PALETTE["well"], highlightbackground="#363E4D", highlightthickness=1)
            ev.pack(fill="x", pady=(0, 6))
            tk.Label(
                ev,
                text=when,
                bg=_PALETTE["well"],
                fg=_PALETTE["arc_border"],
                font=(_MONO_FONT, 8, "bold"),
                justify="left",
                width=11,
            ).pack(side="left", padx=(8, 6), pady=5)
            tk.Label(
                ev, text=title, bg=_PALETTE["well"], fg=_PALETTE["text"], font=(_UI_FONT, 9, "bold"), anchor="w"
            ).pack(side="left", fill="x", expand=True)

    def _render_todos(self, items: list[tuple[str, bool]]) -> None:
        self._todo_items = list(items)
        if not hasattr(self, "todo_list_frame"):
            return
        for child in self.todo_list_frame.winfo_children():
            child.destroy()
        self._todo_vars = []
        if not self._todo_items:
            tk.Label(
                self.todo_list_frame,
                text="No to-dos added yet.",
                bg=_PALETTE["panel_mid"],
                fg=_PALETTE["muted"],
                font=(_UI_FONT, 8),
                anchor="w",
            ).pack(fill="x", pady=2)
            self._refresh_todo_progress()
            return
        for idx, (label, checked) in enumerate(self._todo_items):
            var = tk.BooleanVar(value=checked)
            self._todo_vars.append(var)
            cb = tk.Checkbutton(
                self.todo_list_frame,
                text=label,
                variable=var,
                command=lambda i=idx: self._on_todo_toggle(i),
                bg=_PALETTE["panel_mid"],
                fg="#E8EDF4",
                activebackground=_PALETTE["panel_mid"],
                activeforeground="#FFFFFF",
                selectcolor=_PALETTE["cta_bg"],
                highlightthickness=0,
                bd=0,
                font=(_UI_FONT, 9),
                anchor="w",
                cursor="hand2",
            )
            cb.pack(fill="x", pady=1)
        self._refresh_todo_progress()

    def _add_todo_item(self) -> None:
        if not hasattr(self, "todo_add_entry"):
            return
        txt = self.todo_add_entry.get().strip()
        if not txt:
            return
        self.todo_add_entry.delete(0, "end")

        def _job() -> None:
            try:
                snap = self.bridge.todo_action("add", text=txt)
                self._schedule_ui(lambda: self._apply_dashboard(snap))
            except Exception:
                pass

        threading.Thread(target=_job, daemon=True).start()

    def _on_todo_toggle(self, idx: int) -> None:
        if 0 <= idx < len(self._todo_items):
            txt, _ = self._todo_items[idx]
            new_done = bool(self._todo_vars[idx].get())
            self._todo_items[idx] = (txt, new_done)

            def _job() -> None:
                try:
                    snap = self.bridge.todo_action("toggle", text=txt, done=new_done)
                    self._schedule_ui(lambda: self._apply_dashboard(snap))
                except Exception:
                    pass

            threading.Thread(target=_job, daemon=True).start()
        self._refresh_todo_progress()

    def _refresh_todo_progress(self) -> None:
        total = len(self._todo_vars)
        done = sum(1 for v in self._todo_vars if v.get())
        self.todo_count_lbl.configure(text=f"{done} of {total} done")
        w = max(120, self.todo_prog_canvas.winfo_width() or 240)
        fill_w = int(w * (done / total)) if total > 0 else 0
        self.todo_prog_canvas.coords(self._todo_prog_fill, 0, 0, fill_w, 5)

    def _apply_dashboard(self, snap: dict[str, Any]) -> None:
        if not isinstance(snap, dict):
            return
        if isinstance(snap.get("briefing"), list):
            self._render_briefing([str(x) for x in snap["briefing"]])
        if isinstance(snap.get("gmail"), list):
            self._render_gmail([dict(x) for x in snap["gmail"] if isinstance(x, dict)])
        if isinstance(snap.get("agenda"), list):
            self._render_agenda([dict(x) for x in snap["agenda"] if isinstance(x, dict)])
        if isinstance(snap.get("todos"), list):
            parsed = [
                (str(t.get("text", "")), bool(t.get("done", False)))
                for t in snap["todos"]
                if isinstance(t, dict) and str(t.get("text", "")).strip()
            ]
            self._render_todos(parsed)
        if isinstance(snap.get("apps"), dict):
            self._apps_status = snap["apps"]
            self._sync_apps_popover()

    def _attach_file_path(self) -> None:
        try:
            chosen = filedialog.askopenfilename(title="Attach file to AURA prompt")
        except Exception:
            chosen = ""
        if not chosen:
            return
        self._clear_placeholder()
        cur = self.entry.get().strip()
        add = f"read and inspect {chosen}" if not cur else f"{cur} ({chosen})"
        self.entry.delete(0, "end")
        self.entry.insert(0, add)
        self.entry.focus_set()

    def switch_tab(self, tab: str) -> None:
        """Switch the active Dynamic Island drawer panel ('chat', 'agents', 'deck')."""
        target = tab if tab in ("chat", "agents", "deck") else "chat"
        self.active_tab = target
        for panel in (self.chat_panel, self.agents_panel, self.deck_panel):
            panel.pack_forget()
        {"chat": self.chat_panel, "agents": self.agents_panel, "deck": self.deck_panel}[target].pack(
            fill="both", expand=True
        )
        for key, btn in self.tab_btns.items():
            active = key == target
            btn.configure(
                bg=_PALETTE["pill_active_bg"] if active else _PALETTE["bg"],
                fg=_PALETTE["arc_text"] if active else "#C3CCD8",
                highlightbackground=_PALETTE["arc_border"] if active else "#5A667A",
            )

    def select_agent(self, agent_role: str, *, switch_to_chat: bool = False) -> None:
        """Select a sequential specialist agent ('auto' or SPECIALIST_AGENTS key)."""
        norm = normalize_agent_role(agent_role)
        self.selected_agent = norm
        short = "Auto" if norm == "auto" else norm.capitalize()
        self.agent_btn.configure(
            text=f"✦ {short} ▾",
            bg=_PALETTE["pill_active_bg"] if norm != "auto" else _PALETTE["well"],
            fg=_PALETTE["arc_text"] if norm != "auto" else _PALETTE["accent"],
        )
        for lbl, pbtn in self._agent_header_pills.items():
            is_sel = lbl == f"@{norm}"
            pbtn.configure(
                bg=_PALETTE["pill_active_bg"] if is_sel else _PALETTE["panel_mid"],
                fg=_PALETTE["arc_text"] if is_sel else "#C3CCD8",
                highlightbackground=_PALETTE["arc_border"] if is_sel else "#5A667A",
            )
        for role_id, node in self._pipeline_pills.items():
            lit = norm in ("auto", role_id)
            node.configure(
                bg=_PALETTE["pill_active_bg"] if role_id == norm else _PALETTE["panel_mid"],
                fg=_PALETTE["arc_text"] if lit else _PALETTE["muted"],
            )
        for key, (card, title_lbl, desc_lbl) in self._agent_cards.items():
            selected = key == norm
            card_bg = _PALETTE["pill_active_bg"] if selected else _PALETTE["gunmetal"]
            border = _PALETTE["arc_border"] if selected else "#4E5869"
            card.configure(bg=card_bg, highlightbackground=border)
            title_lbl.configure(bg=card_bg, fg="#FFFFFF" if selected else _PALETTE["text"])
            desc_lbl.configure(bg=card_bg)
        self._refresh_agent_progress(self._agent_progress_pct, "Running" if self.busy else ("Done" if self._agent_progress_pct == 100 else "Ready"))
        self._update_header()
        if switch_to_chat:
            self.switch_tab("chat")
            self._focus_entry()

    def _refresh_agent_progress(self, pct: int, status_word: str = "Ready") -> None:
        clamped = max(0, min(100, int(pct)))
        self._agent_progress_pct = clamped
        norm = getattr(self, "selected_agent", "auto")
        if norm == "auto":
            agent_desc = "✦ Auto (@planner → @coder)"
        elif norm in SPECIALIST_AGENTS:
            agent_desc = f"@{norm} · {SPECIALIST_AGENTS[norm]['label']}"
        else:
            agent_desc = f"@{norm}"
        try:
            self.agent_status_lbl.configure(text=agent_desc)
            self.agent_prog_pct_lbl.configure(
                text=f"{status_word} · {clamped}%",
                fg=_PALETTE["accent"] if clamped > 0 else _PALETTE["arc_text"],
            )
            w = max(120, self.agent_prog_canvas.winfo_width() or 240)
            fill_w = int(w * (clamped / 100.0)) if clamped > 0 else 10
            self.agent_prog_canvas.coords(self._agent_prog_fill, 0, 0, max(6, fill_w), 6)
        except Exception:
            pass

    def _toggle_focus_timer(self) -> None:
        self._focus_running = not self._focus_running
        self.focus_toggle_btn.configure(text="Pause" if self._focus_running else "Start")

    def _reset_focus_timer(self) -> None:
        self._focus_running = False
        self._focus_left_sec = self._focus_total_sec
        self.focus_toggle_btn.configure(text="Start")
        self._render_focus_clock()

    def _set_focus_minutes(self, mins: int) -> None:
        self._focus_running = False
        self._focus_total_sec = max(60, int(mins) * 60)
        self._focus_left_sec = self._focus_total_sec
        self.focus_toggle_btn.configure(text="Start")
        self._render_focus_clock()

    def _render_focus_clock(self) -> None:
        m, s = divmod(max(0, self._focus_left_sec), 60)
        self.focus_time_lbl.configure(text=f"{m:02d}:{s:02d}")
        frac = self._focus_left_sec / max(1, self._focus_total_sec)
        extent = max(-359.0, min(-1.0, -359.0 * frac))
        self.focus_canvas.itemconfigure(self._focus_arc, extent=extent)

    def _tick_telemetry(self) -> None:
        if self._closing:
            return
        now_t = time.monotonic()
        if self._focus_running and now_t - self._last_clock_tick >= 0.9:
            self._focus_left_sec = max(0, self._focus_left_sec - 1)
            if self._focus_left_sec == 0:
                self._focus_running = False
                self.focus_toggle_btn.configure(text="Done")
            self._render_focus_clock()
        self._last_clock_tick = now_t

        stats = self._telemetry.sample()
        bat_str = f"Battery {stats['battery_pct']}%" if stats["battery_pct"] is not None else "AC Power"
        self.capsule_tel_lbl.configure(
            text=f"CPU {stats['cpu_pct']}%   RAM {stats['ram_pct']}%   {bat_str}   Enter to send · Esc to hide"
        )
        self.bat_pill_lbl.configure(text=bat_str)
        self.deck_tel_lbl.configure(
            text=f"CPU Load:  {stats['cpu_pct']}%\nRAM Usage: {stats['ram_pct']}%\nPower:     {bat_str}\nModel:     {self.active_model_name}"
        )
        self._ensure_pinned_visible()
        self._telemetry_ticks += 1
        poll_every = 1 if (self.busy or self.voice_speaking) else 2
        if self._telemetry_ticks % poll_every == 0 and not os.environ.get("PYTEST_CURRENT_TEST"):
            self._refresh_status_async()
        self.root.after(1000, self._tick_telemetry)

    def _sync_mic_buttons(self, state: str) -> None:
        cfg = {
            "off": ("🎙 Mic", _PALETTE["gunmetal"], _PALETTE["steel"], False),
            "listening": ("🎙 Listening", _PALETTE["pill_active_bg"], _PALETTE["arc_text"], True),
            "loading": ("🎙 Loading...", _PALETTE["pill_active_bg"], _PALETTE["amber"], True),
        }[state]
        btn_txt, btn_bg, fg, active = cfg
        self.mic_btn.configure(text=btn_txt, bg=btn_bg, fg=fg)
        self.mic_btn.bind("<Leave>", lambda _e, b=self.mic_btn, c=btn_bg, f=fg: b.configure(bg=c, fg=f))
        sw_bg = _PALETTE["hdr_bar"] if self.expanded else _PALETTE["gunmetal"]
        self._img_refs["cap_sw"] = self._draw_switch(self.capsule_mic_btn, active=active, bg_hex=sw_bg)
        if hasattr(self, "mic_lbl_hdr"):
            if self.expanded:
                self.mic_lbl_hdr.configure(
                    text="Mic on" if active else "Mic",
                    fg=_PALETTE["arc_text"] if active else "#DCE4EE",
                )
            else:
                self.mic_lbl_hdr.configure(text="Mic", fg="#DCE4EE")

    def toggle_mic(self) -> None:
        if self._closing:
            return
        if self.shuttered and not self.mic_enabled:
            self.wake_from_shutter()
        if self.mic_enabled:
            self.mic_enabled = False
            self.mic_listener.stop()
            self._sync_mic_buttons("off")
            if not self.busy:
                self.state_label = "ULTRON VOICE · STANDBY"
                self._update_header()
            return

        if not self.expanded:
            self.expand()
        if self.persona == "off":
            self.persona = "jarvis"
            self._update_header()
            threading.Thread(target=lambda: self._safe_set_voice("jarvis"), daemon=True).start()

        self.mic_enabled = True
        self._sync_mic_buttons("listening")
        self.state_label = "ULTRON VOICE · LISTENING"
        self._update_header()
        if not self.mic_listener.start():
            self.mic_enabled = False
            self._sync_mic_buttons("off")
            self.state_label = "ULTRON VOICE · STANDBY"
            self._update_header()

    def _safe_set_voice(self, target: str) -> None:
        try:
            res = self.bridge.set_voice(target)
            st = res.get("status")
            if st:
                self._schedule_ui(lambda: self._apply_status(st))
        except Exception:
            pass

    def _on_mic_status(self, status: str) -> None:
        if self._closing:
            return
        if status == "loading" and self.mic_enabled:
            self._sync_mic_buttons("loading")
            if not self.busy:
                self.state_label = "ULTRON VOICE · LOADING"
                self._update_header()
        elif status == "listening" and self.mic_enabled:
            self._sync_mic_buttons("listening")
            if not self.busy:
                self.state_label = "ULTRON VOICE · LISTENING"
                self._update_header()
        elif status == "off":
            self.mic_enabled = False
            self._sync_mic_buttons("off")
            if not self.busy:
                self.state_label = "ULTRON VOICE · STANDBY"
                self._update_header()

    def _on_mic_error(self, err: str) -> None:
        self.mic_enabled = False
        self._sync_mic_buttons("off")
        if not self.busy:
            self.state_label = "ULTRON VOICE · STANDBY"
            self._update_header()
        self._append_rich([(f"Mic error: {err}\n", "err_body")])

    def _on_speech_onset(self) -> None:
        """Immediate user speech barge-in: silence TTS at once without wiping partial output."""
        if self._closing or not self.mic_enabled or not self.voice_speaking:
            return
        self._voice_bargein_until = time.monotonic() + 2.5
        self.voice_speaking = False
        if not self.busy:
            self.state_label = "ULTRON VOICE · LISTENING"
        self._sync_stop_controls()
        self._update_header()
        threading.Thread(target=self.bridge.stop_voice, daemon=True).start()

    def _on_voice_transcript(self, text: str) -> None:
        clean = (text or "").strip()
        if not clean or self._closing or not self.mic_enabled:
            return
        active = bool(self.busy or self.voice_speaking)
        if is_stop_command(clean, during_playback=active):
            if active:
                self.stop_active()
            return
        if self.voice_speaking and not self.busy:
            return
        if self.busy:
            self._push_steer(clean, source="voice")
            return
        if not self.expanded:
            self.expand()
        if self.active_tab != "chat":
            self.switch_tab("chat")
        self._clear_placeholder()
        self.entry.delete(0, "end")
        self.entry.insert(0, clean)
        self._submit()

    def _clear_placeholder(self, _e: tk.Event[Any] | None = None) -> None:
        if self._has_placeholder:
            self._has_placeholder = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=_PALETTE["text"])

    def _on_entry_key(self, event: tk.Event[Any]) -> None:
        if self._has_placeholder and event.keysym not in (
            "Return", "Escape", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"
        ):
            self._clear_placeholder()

    def _restore_placeholder(self, _e: tk.Event[Any] | None = None) -> None:
        if not self.entry.get().strip():
            self._has_placeholder = True
            self.entry.delete(0, "end")
            self.entry.insert(0, getattr(self, "_active_placeholder", _PLACEHOLDER))
            self.entry.configure(fg=_PALETTE["muted"])

    def _refresh_placeholder_text(self) -> None:
        target = _PUSH_PLACEHOLDER if self.busy else _PLACEHOLDER
        self._active_placeholder = target
        if self._has_placeholder and hasattr(self, "entry"):
            try:
                self.entry.delete(0, "end")
                self.entry.insert(0, target)
                self.entry.configure(fg=_PALETTE["muted"])
            except Exception:
                pass

    def _apply_geometry_box(self, g: IslandGeometry, *, radius: int | None = None, animate_step: bool = False) -> None:
        self._current_geo = g
        if radius is not None:
            r = int(radius)
        elif g.height <= 36:
            r = max(24, int(g.height))
        elif g.height <= 48:
            r = max(36, int(g.height))
        else:
            r = round(38 + 10 * min(1.0, (g.height - 48) / 140.0))
        top_flush = g.y <= 2
        rgn_key = (int(g.width), int(g.height), r, top_flush)

        if sys.platform == "win32" and not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                top_hwnd = _get_top_hwnd(self.root.winfo_id())
                if top_hwnd:
                    u32 = ctypes.WinDLL("user32", use_last_error=True)
                    u32.SetWindowPos.argtypes = [
                        wintypes.HWND,
                        wintypes.HWND,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        wintypes.UINT,
                    ]
                    u32.SetWindowPos.restype = wintypes.BOOL
                    # SWP_NOZORDER | SWP_NOREDRAW | SWP_NOACTIVATE = 0x001C
                    u32.SetWindowPos(wintypes.HWND(top_hwnd), None, g.x, g.y, g.width, g.height, 0x001C)
                    if rgn_key != self._last_rgn_key:
                        self._last_rgn_key = rgn_key
                        apply_window_region(top_hwnd, g.width, g.height, r, top_flush=top_flush, redraw=(not animate_step))
            except Exception:
                pass

        self.root.geometry(f"{g.width}x{g.height}+{g.x}+{g.y}")
        if not animate_step:
            self.root.update_idletasks()
            if rgn_key != self._last_rgn_key:
                self._last_rgn_key = rgn_key
                apply_window_region(self.root.winfo_id(), g.width, g.height, r, top_flush=top_flush, redraw=True)

    def _apply_geometry(self, animate_from: IslandGeometry | None = None) -> None:
        if getattr(self, "_anim_after_id", None):
            try:
                self.root.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        target = compute_island_geometry(sw, sh, expanded=self.expanded, shuttered=self.shuttered)
        no_anim = bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AURA_NO_ANIM") == "1")
        if animate_from is None or no_anim or (animate_from.width == target.width and animate_from.height == target.height):
            self._pending_collapse_drawer = False
            if self.expanded and not self.drawer.winfo_manager():
                self.drawer.pack(fill="both", expand=True)
            elif not self.expanded and self.drawer.winfo_manager():
                self.drawer.pack_forget()
            self._apply_geometry_box(target, animate_step=False)
            self._img_refs["brow"] = _draw_brow_crest(
                self.brow_canvas, target.width, expanded=self.expanded, bg_hex=_PALETTE["bg"]
            )
            return

        # Pre-pack drawer BEFORE step 0 when expanding so Tkinter computes child geometry once instead of stalling mid-flight.
        if self.expanded and not self.drawer.winfo_manager():
            self.drawer.pack(fill="both", expand=True)
            self.root.update_idletasks()

        frames = compute_spring_frames(animate_from, target, steps=18)

        def _step(idx: int) -> None:
            if self._closing or idx >= len(frames):
                self._anim_after_id = None
                return
            is_last = idx == len(frames) - 1
            frame_geo = frames[idx]
            if not self.expanded and self._pending_collapse_drawer and is_last:
                self._pending_collapse_drawer = False
                if self.drawer.winfo_manager():
                    self.drawer.pack_forget()
                self.frame.configure(bg=_PALETTE["gunmetal"], highlightthickness=0)
            self._apply_geometry_box(frame_geo, animate_step=(not is_last))
            if is_last:
                self._img_refs["brow"] = _draw_brow_crest(
                    self.brow_canvas, frame_geo.width, expanded=self.expanded, bg_hex=_PALETTE["bg"]
                )
                self._anim_after_id = None
            else:
                self._anim_after_id = self.root.after(9, lambda: _step(idx + 1))

        _step(0)

    def _apply_state_layout(self, *, defer_drawer: bool = False, keep_drawer: bool = False) -> None:
        # Cleanly repack only the widgets belonging to compact / shuttered / expanded
        self._close_model_popover()
        self._close_apps_popover()
        self._close_tools_popover()
        self.shuttered_bar.pack_forget()
        if not keep_drawer:
            self.drawer.pack_forget()
        self.header.pack_forget()
        self.brow_canvas.pack_forget()
        self.exp_div1.pack_forget()
        self.model_btn.pack_forget()
        self.exp_div2.pack_forget()
        self.hdr_agents_frame.pack_forget()
        self.exp_div3.pack_forget()
        self.pill_label.pack_forget()
        self.compact_voice_btn.pack_forget()
        self.mic_lbl_hdr.pack_forget()
        self.hdr_stop_btn.pack_forget()
        self.apps_btn.pack_forget()
        self.shutter_btn.pack_forget()
        self.shutdown_btn.pack_forget()

        if self.shuttered and not self.expanded:
            self.frame.configure(bg=_PALETTE["gunmetal"], highlightthickness=0)
            self.shuttered_bar.pack(fill="both", expand=True)
            self._img_refs["wake_sw"] = self._draw_switch(self.wake_switch_canvas, active=False, bg_hex=_PALETTE["gunmetal"])
            self.island_state = "shuttered"
            return

        if self.expanded:
            self.island_state = "expanded"
            self.frame.configure(bg=_PALETTE["bg"], highlightthickness=0)
            self.brow_canvas.pack(side="top", fill="x", padx=4)
            self.header.configure(bg=_PALETTE["hdr_bar"], height=56, highlightthickness=1)
            self.orb_canvas.configure(bg=_PALETTE["hdr_bar"], width=42, height=42)
            self.orb_canvas.pack_configure(padx=(4, 2))
            self.wave_stack.configure(bg=_PALETTE["hdr_bar"])
            self.wave_canvas.configure(bg=_PALETTE["hdr_bar"], width=68, height=28)
            self.hdr_right.configure(bg=_PALETTE["hdr_bar"])
            self.hdr_right.pack_configure(padx=(2, 6))
            self.mic_lbl_hdr.configure(bg=_PALETTE["hdr_bar"])
            self.capsule_mic_btn.configure(bg=_PALETTE["hdr_bar"])
            self.stop_canvas.configure(bg=_PALETTE["hdr_bar"])
            if self._orb_eye_item and self._img_refs.get("eye_expanded") is not None:
                self.orb_canvas.coords(self._orb_eye_item, 21, 21)
                self.orb_canvas.itemconfigure(self._orb_eye_item, image=self._img_refs["eye_expanded"])
            self.header.pack(side="top", fill="x", padx=8, pady=(1, 4))
            self.exp_div1.pack(side="left", padx=2, after=self.orb_canvas)
            self.model_btn.configure(bg=_PALETTE["well"])
            self.model_btn.pack(side="left", padx=1, after=self.exp_div1)
            self.exp_div2.pack(side="left", padx=2, after=self.model_btn)
            self.hdr_agents_frame.pack(side="left", padx=1, after=self.exp_div2)
            self.exp_div3.pack(side="left", padx=2, after=self.hdr_agents_frame)
            self.pill_label.pack(side="bottom", pady=(0, 2))
            self.shutdown_btn.configure(text="⏻ Off", padx=6, pady=2)
            self.shutdown_btn.pack(side="right", padx=(3, 0))
            self.mic_lbl_hdr.pack(side="left", padx=(0, 3), before=self.capsule_mic_btn)
            self.apps_btn.pack(side="left", padx=(0, 3))
            self.shutter_btn.pack(side="left", padx=(0, 0))
            if not defer_drawer:
                self.drawer.pack(fill="both", expand=True)
        else:
            self.island_state = "compact"
            self.frame.configure(bg=_PALETTE["bg"] if keep_drawer else _PALETTE["gunmetal"], highlightthickness=0)
            self.header.configure(bg=_PALETTE["gunmetal"], height=40, highlightthickness=0)
            self.orb_canvas.configure(bg=_PALETTE["gunmetal"], width=32, height=32)
            self.orb_canvas.pack_configure(padx=(4, 2))
            self.wave_stack.configure(bg=_PALETTE["gunmetal"])
            self.wave_canvas.configure(bg=_PALETTE["gunmetal"], width=92, height=28)
            self.hdr_right.configure(bg=_PALETTE["gunmetal"])
            self.hdr_right.pack_configure(padx=(2, 6))
            self.capsule_mic_btn.configure(bg=_PALETTE["gunmetal"])
            self.stop_canvas.configure(bg=_PALETTE["gunmetal"])
            if self._orb_eye_item and self._img_refs.get("eye_compact") is not None:
                self.orb_canvas.coords(self._orb_eye_item, 16, 16)
                self.orb_canvas.itemconfigure(self._orb_eye_item, image=self._img_refs["eye_compact"])
            self.header.pack(side="top", fill="both", expand=True, padx=4, pady=0)
            self.shutdown_btn.configure(text="⏻", padx=6, pady=2)
            self.shutdown_btn.pack(side="right", padx=(3, 0))
            self.compact_voice_btn.pack(side="left", padx=(0, 4), before=self.capsule_mic_btn)
            if keep_drawer and not self.drawer.winfo_manager():
                self.drawer.pack(fill="both", expand=True)

        self._update_header()
        self._sync_mic_buttons("listening" if self.mic_enabled else "off")

    def _on_escape(self, _e: tk.Event[Any] | None = None) -> None:
        if getattr(self, "_model_popover_visible", False):
            self._close_model_popover()
            return
        if getattr(self, "_apps_popover_visible", False):
            self._close_apps_popover()
            return
        if getattr(self, "_tools_popover_visible", False):
            self._close_tools_popover()
            return
        self.collapse()

    def _on_root_focus_in(self, _e: tk.Event[Any] | None = None) -> None:
        self._had_focus = True

    def _on_root_focus_out(self, _e: tk.Event[Any] | None = None) -> None:
        if (
            not self.expanded
            or self._closing
            or self._popup_open
            or getattr(self, "_model_popover_visible", False)
            or getattr(self, "_apps_popover_visible", False)
            or getattr(self, "_tools_popover_visible", False)
        ):
            return
        try:
            self.root.after(140, self._check_focus_collapse)
        except Exception:
            pass

    def _check_focus_collapse(self) -> None:
        if (
            not self.expanded
            or self._closing
            or self._popup_open
            or getattr(self, "_model_popover_visible", False)
            or getattr(self, "_apps_popover_visible", False)
            or getattr(self, "_tools_popover_visible", False)
        ):
            return
        if not self._had_focus or time.monotonic() - self._last_expand_time < 0.35:
            return
        try:
            alpha = float(self.root.attributes("-alpha") or 1.0)
            if alpha <= 0.05:
                return
            if self.root.focus_displayof() is not None:
                return
            if sys.platform == "win32":
                fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                if fg_hwnd in (self.root.winfo_id(), _get_top_hwnd(self.root.winfo_id())):
                    return
            self.collapse()
        except Exception:
            pass

    def check_outside_click(self, cursor_x: int, cursor_y: int) -> bool:
        """Collapse the expanded Dynamic Island automatically when clicking outside its window bounds."""
        if not self.expanded or self._closing or self._popup_open:
            return False
        wx = self.root.winfo_rootx()
        wy = self.root.winfo_rooty()
        ww = self.root.winfo_width()
        wh = self.root.winfo_height()
        if ww <= 1 or wh <= 1:
            return False
        if wx <= cursor_x <= wx + ww and wy <= cursor_y <= wy + wh:
            return False
        self._close_model_popover()
        self._close_apps_popover()
        self._close_tools_popover()
        self.collapse()
        return True

    def _poll_outside_click(self) -> None:
        if sys.platform != "win32" or not self.expanded or self._closing or self._popup_open:
            return
        try:
            alpha = float(self.root.attributes("-alpha") or 1.0)
            if alpha <= 0.05 or time.monotonic() - self._last_expand_time < 0.35:
                return
            user32 = ctypes.windll.user32
            down = bool((user32.GetAsyncKeyState(0x01) & 0x8000) or (user32.GetAsyncKeyState(0x02) & 0x8000))
            was_down = self._prev_mouse_down
            self._prev_mouse_down = down
            if not down or was_down:
                return
            pt = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return
            if getattr(self, "_model_popover_visible", False):
                px = self.model_popover.winfo_rootx()
                py = self.model_popover.winfo_rooty()
                pw = self.model_popover.winfo_width()
                ph = self.model_popover.winfo_height()
                bx = self.model_btn.winfo_rootx()
                by = self.model_btn.winfo_rooty()
                bw = self.model_btn.winfo_width()
                bh = self.model_btn.winfo_height()
                in_pop = px <= pt.x <= px + pw and py <= pt.y <= py + ph
                in_btn = bx <= pt.x <= bx + bw and by <= pt.y <= by + bh
                if not in_pop and not in_btn:
                    self._close_model_popover()
            if getattr(self, "_apps_popover_visible", False):
                ax = self.apps_popover.winfo_rootx()
                ay = self.apps_popover.winfo_rooty()
                aw = self.apps_popover.winfo_width()
                ah = self.apps_popover.winfo_height()
                abx = self.apps_btn.winfo_rootx()
                aby = self.apps_btn.winfo_rooty()
                abw = self.apps_btn.winfo_width()
                abh = self.apps_btn.winfo_height()
                in_apop = ax <= pt.x <= ax + aw and ay <= pt.y <= ay + ah
                in_abtn = abx <= pt.x <= abx + abw and aby <= pt.y <= aby + abh
                if not in_apop and not in_abtn:
                    self._close_apps_popover()
            if getattr(self, "_tools_popover_visible", False):
                tx = self.tools_popover.winfo_rootx()
                ty = self.tools_popover.winfo_rooty()
                tw = self.tools_popover.winfo_width()
                th = self.tools_popover.winfo_height()
                tbx = self.tools_btn.winfo_rootx()
                tby = self.tools_btn.winfo_rooty()
                tbw = self.tools_btn.winfo_width()
                tbh = self.tools_btn.winfo_height()
                in_tpop = tx <= pt.x <= tx + tw and ty <= pt.y <= ty + th
                in_tbtn = tbx <= pt.x <= tbx + tbw and tby <= pt.y <= tby + tbh
                if not in_tpop and not in_tbtn:
                    self._close_tools_popover()
            self.check_outside_click(int(pt.x), int(pt.y))
        except Exception:
            pass

    def _on_pill_click(self, event: tk.Event[Any]) -> None:
        self._drag_offset = (event.x, event.y)
        if self.shuttered:
            self.wake_from_shutter()
        elif not self.expanded:
            self.expand()
        else:
            if event.widget is self.orb_canvas:
                self.collapse()

    def _on_pill_drag(self, event: tk.Event[Any]) -> None:
        dx = event.x - self._drag_offset[0]
        dy = event.y - self._drag_offset[1]
        self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")

    def enter_shuttered(self) -> None:
        """Transition to State 02 · Shuttered standby (eye slit, voice & agents asleep)."""
        self._close_model_popover()
        self._close_apps_popover()
        close_tools = getattr(self, "_close_tools_popover", None)
        if close_tools is not None:
            close_tools()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        prev_geo = self._current_geo or compute_island_geometry(sw, sh, expanded=self.expanded, shuttered=self.shuttered)
        if self.mic_enabled:
            self.mic_enabled = False
            try:
                self.mic_listener.stop()
            except Exception:
                pass
            self._sync_mic_buttons("off")
        if self.busy or self.voice_speaking:
            self.stop_active()
        self.expanded = False
        self.shuttered = True
        self._apply_state_layout()
        self._apply_geometry(animate_from=prev_geo)
        self._ensure_pinned_visible()

    def wake_from_shutter(self) -> None:
        """Wake from State 02 · Shuttered standby back into State 01 · Compact island."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        prev_geo = self._current_geo or compute_island_geometry(sw, sh, expanded=self.expanded, shuttered=self.shuttered)
        self.shuttered = False
        self.expanded = False
        self._apply_state_layout()
        self._apply_geometry(animate_from=prev_geo)
        self._ensure_pinned_visible()

    def toggle_shutter(self) -> None:
        if self.shuttered:
            self.wake_from_shutter()
        else:
            self.enter_shuttered()

    def _ensure_pinned_visible(self) -> None:
        """Keep the Dynamic Island un-minimized and topmost across Edge/desktop minimizes."""
        if self._closing:
            return
        try:
            if self.root.state() in ("iconic", "withdrawn"):
                self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
            if sys.platform == "win32" and not os.environ.get("PYTEST_CURRENT_TEST"):
                u32 = ctypes.WinDLL("user32", use_last_error=True)
                u32.IsIconic.argtypes = [wintypes.HWND]
                u32.IsIconic.restype = wintypes.BOOL
                u32.IsWindowVisible.argtypes = [wintypes.HWND]
                u32.IsWindowVisible.restype = wintypes.BOOL
                u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                u32.ShowWindow.restype = wintypes.BOOL
                u32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, wintypes.UINT,
                ]
                u32.SetWindowPos.restype = wintypes.BOOL
                top = _get_top_hwnd(self.root.winfo_id())
                if top:
                    hwnd = wintypes.HWND(top)
                    if u32.IsIconic(hwnd):
                        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    elif not u32.IsWindowVisible(hwnd):
                        u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                    # HWND_TOPMOST=-1, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_SHOWWINDOW=0x0053
                    u32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053)
        except Exception:
            pass

    def wake_and_expand(self, *, from_event: bool = False) -> None:
        """Bring the running Dynamic Island to the front and expand its UI."""
        if from_event:
            self._event_woken_at = time.monotonic()
        if self.shuttered:
            self.wake_from_shutter()
        self.expand()
        self._ensure_pinned_visible()

    def expand(self) -> None:
        """Transition into State 03 · Fully opened Ultron Dynamic Island with spring motion."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        prev_geo = self._current_geo or compute_island_geometry(sw, sh, expanded=self.expanded, shuttered=self.shuttered)
        self.shuttered = False
        self._last_expand_time = time.monotonic()
        self._prev_mouse_down = False
        if not self.expanded:
            no_anim = bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AURA_NO_ANIM") == "1")
            defer = (not no_anim) and prev_geo.height < 180
            self._pending_collapse_drawer = False
            self.expanded = True
            self._apply_state_layout(defer_drawer=defer)
            self._apply_geometry(animate_from=prev_geo)
        self._ensure_pinned_visible()
        self._focus_entry()
        self._refresh_dashboard_async()

    def collapse(self) -> None:
        """Transition back to State 01 · Compact Dynamic Island with spring motion."""
        self._close_model_popover()
        self._close_apps_popover()
        close_tools = getattr(self, "_close_tools_popover", None)
        if close_tools is not None:
            close_tools()
        if self.expanded:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            prev_geo = self._current_geo or compute_island_geometry(sw, sh, expanded=True, shuttered=False)
            no_anim = bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AURA_NO_ANIM") == "1")
            keep = (not no_anim) and prev_geo.height > 200
            self._pending_collapse_drawer = keep
            self.expanded = False
            self.shuttered = False
            self._apply_state_layout(keep_drawer=keep)
            self._apply_geometry(animate_from=prev_geo)
        self._ensure_pinned_visible()

    def toggle_expand(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def on_hotkey_press(self) -> None:
        """Ctrl+Alt+Space handler: expand + start mic push-to-talk, or stop + collapse."""
        if os.environ.get("AURA_HOTKEY_NO_AUTO_MIC") == "1":
            self.toggle_expand()
            return
        if not self.expanded or not self.mic_enabled:
            if not self.expanded:
                self.expand()
            if not self.mic_enabled:
                self.toggle_mic()
            return
        self.toggle_mic()
        self.collapse()

    def shutdown_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_req_seq += 1
        self.busy = False
        self.voice_speaking = False
        self.mic_enabled = False
        self.state_label = "Shutting down & freeing VRAM..."
        if getattr(self, "_anim_after_id", None):
            try:
                self.root.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None
        if getattr(self, "_tools_hover_close_id", None):
            try:
                self.root.after_cancel(self._tools_hover_close_id)
            except Exception:
                pass
            self._tools_hover_close_id = None
        self._hotkey_stop.set()
        try:
            self.mic_listener.stop()
        except Exception:
            pass
        try:
            self.pill_label.configure(text="SHUTTING DOWN · FREEING VRAM...")
            self.root.update_idletasks()
        except Exception:
            pass

        def _stop() -> None:
            self.bridge.shutdown()
            try:
                self.root.after(0, self._destroy_root)
            except Exception:
                pass

        threading.Thread(target=_stop, daemon=True).start()

    def _destroy_root(self) -> None:
        if getattr(self, "_wake_event_handle", 0) and sys.platform == "win32":
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle.restype = wintypes.BOOL
                k32.CloseHandle(wintypes.HANDLE(self._wake_event_handle))
            except Exception:
                pass
            self._wake_event_handle = 0
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _update_header(self) -> None:
        disp_m = (
            _format_cloud_model_label(self.active_model_name)
            if str(self.active_model_name).startswith("cloud:")
            else self.active_model_name
        )
        short_m = disp_m if len(disp_m) <= 18 else f"{disp_m[:17]}…"
        self.model_btn.configure(text=f"◈ {short_m} ▾")

        status_txt = self.state_label
        if status_txt == "AURA Jarvis":
            status_txt = "ULTRON VOICE · STANDBY"
        self.pill_label.configure(text=status_txt.upper())
        self.mode_btn.configure(
            text="RO" if self.read_only else "RW",
            fg=_PALETTE["blue"] if self.read_only else _PALETTE["amber"],
        )
        self.voice_btn.configure(text=_PERSONA_LABELS.get(self.persona, "Voice: Off"))
        if hasattr(self, "compact_voice_btn"):
            vshort = {"jarvis": "🔊 J", "ultron": "🔊 U", "megatron": "🔊 M"}.get(self.persona, "🔇")
            self.compact_voice_btn.configure(
                text=vshort,
                fg=_PALETTE["accent"] if self.persona != "off" else _PALETTE["muted"],
            )
        self.ollama_btn.configure(
            text="● Ollama" if self.ollama_up else "● Start Ollama",
            fg=_PALETTE["accent"] if self.ollama_up else _PALETTE["amber"],
        )

    def _append_rich(self, segments: list[tuple[str, str]]) -> None:
        self.output.configure(state="normal")
        for text, tag in segments:
            self.output.insert("end", text, tag)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _run_quick(self, prompt: str) -> None:
        if not self.expanded:
            self.expand()
        if self.active_tab != "chat":
            self.switch_tab("chat")
        self._clear_placeholder()
        self.entry.delete(0, "end")
        self.entry.insert(0, prompt)
        if prompt.endswith(" ") or prompt.endswith(": "):
            self.entry.focus_force()
            return
        self._submit()

    def _refresh_status_async(self) -> None:
        def _job() -> None:
            try:
                st = self.bridge.get_status()
                self._schedule_ui(lambda: self._apply_status(st))
            except Exception:
                pass
            try:
                snap = self.bridge.get_dashboard_data()
                self._schedule_ui(lambda: self._apply_dashboard(snap))
            except Exception:
                pass

        threading.Thread(target=_job, daemon=True).start()

    def _refresh_dashboard_async(self) -> None:
        def _job() -> None:
            try:
                snap = self.bridge.get_dashboard_data()
                self._schedule_ui(lambda: self._apply_dashboard(snap))
            except Exception:
                pass

        threading.Thread(target=_job, daemon=True).start()

    def _apply_status(self, st: dict[str, Any]) -> None:
        self.read_only = bool(st.get("read_only", True))
        if "ollama_up" in st:
            self.ollama_up = bool(st["ollama_up"])
        if st.get("available_models"):
            self.available_models = [str(m) for m in st["available_models"]]
        if isinstance(st.get("available_cloud_models"), list):
            self.available_cloud_models = [str(m) for m in st["available_cloud_models"]]
        if isinstance(st.get("available_tools"), list):
            parsed_tools = [
                {
                    "name": str(item.get("name") or ""),
                    "description": str(item.get("description") or ""),
                }
                for item in st["available_tools"]
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            if parsed_tools:
                self.available_tools = parsed_tools
                if getattr(self, "_tools_popover_visible", False) and hasattr(self, "_populate_tools_popover"):
                    self._populate_tools_popover()
        if st.get("model_override"):
            self.active_model_name = str(st["model_override"])
        else:
            roles = st.get("roles") or {}
            if roles.get("lightweight"):
                self.active_model_name = str(roles["lightweight"])
        if st.get("agent_role"):
            norm = normalize_agent_role(st["agent_role"])
            if norm != self.selected_agent:
                self.select_agent(norm)
        v = st.get("voice") or {}
        if v.get("persona") in _PERSONA_CYCLE:
            self.persona = v["persona"]
        if ("speaking" in v or "actively_playing" in v) and not self.busy:
            if time.monotonic() >= getattr(self, "_voice_bargein_until", 0.0):
                self.voice_speaking = bool(v.get("actively_playing", v.get("speaking")))
        if self.busy:
            latest_step = str(st.get("latest_step") or "").strip()
            if latest_step and latest_step != getattr(self, "_last_latest_step", ""):
                self._last_latest_step = latest_step
                self._last_reasoning_text = latest_step
                if hasattr(self, "reasoning_detail_lbl"):
                    self.reasoning_detail_lbl.configure(text=latest_step)
            pending_steers = int(st.get("pending_steers") or 0)
            if pending_steers > 0:
                self._refresh_agent_progress(max(30, self._agent_progress_pct), f"Steer x{pending_steers}")
        wake_seq = int(st.get("overlay_wake_seq") or 0)
        if wake_seq > self._last_wake_seq:
            prev_seq = self._last_wake_seq
            self._last_wake_seq = wake_seq
            recently_event_woken = (
                self._event_woken_at is not None and (time.monotonic() - self._event_woken_at) < 3.0
            )
            if not recently_event_woken and (prev_seq > 0 or bool(st.get("overlay_expand"))):
                self.wake_and_expand()
        self._sync_stop_controls()
        self._update_header()

    def _toggle_mode(self) -> None:
        if self._mode_switching:
            return
        self._mode_switching = True
        next_mode = "rw" if self.read_only else "ro"
        self.mode_btn.configure(text=f"{next_mode.upper()}...")

        def _job() -> None:
            try:
                st = self.bridge.set_mode(next_mode)
                self._schedule_ui(lambda: self._apply_status(st))
            except Exception as exc:
                self._schedule_ui(lambda: self._append_rich([(f"Mode error: {exc}\n", "err_body")]))
            finally:
                def _done() -> None:
                    self._mode_switching = False
                    self._update_header()

                self._schedule_ui(_done)

        threading.Thread(target=_job, daemon=True).start()

    def _cycle_persona(self) -> None:
        target = next_persona(self.persona)
        self.persona = target
        self._update_header()

        def _job() -> None:
            try:
                res = self.bridge.set_voice(target)
                st = res.get("status")
                if st:
                    self._schedule_ui(lambda: self._apply_status(st))
            except Exception as exc:
                self._schedule_ui(lambda: self._append_rich([(f"Voice error: {exc}\n", "err_body")]))

        threading.Thread(target=_job, daemon=True).start()

    def _wake_ollama(self) -> None:
        self.state_label = "STARTING OLLAMA..."
        self._update_header()

        def _job() -> None:
            try:
                res = self.bridge.start_ollama()
                ok = bool(res.get("started"))
                st = res.get("status")
                self._schedule_ui(lambda: self._after_ollama_wake(ok, st))
            except Exception as exc:
                self._schedule_ui(lambda: self._on_error(str(exc)))

        threading.Thread(target=_job, daemon=True).start()

    def _after_ollama_wake(self, ok: bool, st: dict[str, Any] | None) -> None:
        self.ollama_up = ok
        self.state_label = "ULTRON VOICE · STANDBY" if ok else "OLLAMA OFFLINE"
        if st:
            self._apply_status(st)
        else:
            self._update_header()
        msg = "Ollama local daemon is online and ready.\n" if ok else "Could not wake Ollama automatically.\n"
        self._append_rich([(msg, "meta_note" if ok else "err_body")])

    def _unload_vram(self) -> None:
        self.state_label = "FREEING VRAM..."
        self._update_header()

        def _job() -> None:
            try:
                res = self.bridge.unload_vram()
                names = ", ".join(res.get("unloaded") or []) or "VRAM free"
                self._schedule_ui(lambda: self._after_vram_unload(names))
            except Exception as exc:
                self._schedule_ui(lambda: self._append_rich([(f"VRAM unload error: {exc}\n", "err_body")]))

        threading.Thread(target=_job, daemon=True).start()

    def _after_vram_unload(self, msg: str) -> None:
        self.state_label = "VRAM IDLE"
        self._update_header()
        self._append_rich([(f"VRAM unloaded: {msg}\n", "meta_note")])

    def _sync_stop_controls(self) -> None:
        active = bool(self.busy or self.voice_speaking)
        self.mic_listener.set_barge_in(active)
        self._refresh_placeholder_text()
        try:
            if active:
                if not self.stop_canvas.winfo_manager():
                    anchor_widget = self.apps_btn if self.expanded else self.compact_voice_btn
                    self.stop_canvas.pack(side="left", padx=(0, 6), before=anchor_widget)
                if self.busy:
                    self.send_btn.configure(text="➤ Push", bg="#1D4ED8", fg="#F2F8FF")
                else:
                    self.send_btn.configure(text="■ Stop", bg=_PALETTE["danger_bg"], fg=_PALETTE["danger_fg"])
                self.composer_stop_btn.configure(
                    bg=_PALETTE["danger_bg"], fg=_PALETTE["danger_fg"], highlightbackground="#E11D48"
                )
                self.hdr_stop_btn.configure(bg=_PALETTE["danger_bg"], fg=_PALETTE["danger_fg"])
            else:
                if self.stop_canvas.winfo_manager():
                    self.stop_canvas.pack_forget()
                self.send_btn.configure(text="Send", bg=_PALETTE["cta_bg"], fg="#F2F8FF")
                self.composer_stop_btn.configure(
                    bg=_PALETTE["gunmetal"], fg=_PALETTE["steel"], highlightbackground="#5A667A"
                )
                self.hdr_stop_btn.configure(bg=_PALETTE["card_hi"], fg=_PALETTE["text"])
        except Exception:
            pass

    def _on_send_or_stop(self) -> None:
        if self.busy:
            prompt = "" if self._has_placeholder else self.entry.get().strip()
            if prompt:
                self._submit()
            else:
                self._focus_entry()
            return
        if self.voice_speaking:
            self.stop_active()
            return
        self._submit()

    def stop_active(self) -> None:
        self._stop_req_seq += 1
        was_active = self.busy or self.voice_speaking
        self.busy = False
        self.voice_speaking = False
        self.state_label = "ULTRON VOICE · LISTENING" if self.mic_enabled else "ULTRON VOICE · STOPPED"
        self._refresh_agent_progress(0, "Stopped")
        self._sync_stop_controls()
        self._update_header()
        if was_active:
            self._append_rich([("Stopped by user.\n", "meta_note")])

        def _job() -> None:
            self.bridge.stop()

        threading.Thread(target=_job, daemon=True).start()

    def _set_voice_speaking(self, speaking: bool) -> None:
        if self._closing:
            return
        self.voice_speaking = bool(speaking)
        if not self.voice_speaking and not self.busy:
            self.state_label = "ULTRON VOICE · LISTENING" if self.mic_enabled else "ULTRON VOICE · STANDBY"
        self._sync_stop_controls()
        self._update_header()

    def _push_steer(self, prompt: str, *, source: str = "text") -> None:
        clean = (prompt or "").strip()
        if not clean or self._closing:
            return
        if self.voice_speaking:
            self._voice_bargein_until = time.monotonic() + 2.0
            self.voice_speaking = False
        self.state_label = "ULTRON · REDIRECTING"
        self._refresh_agent_progress(max(30, self._agent_progress_pct), "Pushing")
        self._sync_stop_controls()
        self._update_header()
        self._append_rich([("\n⚡ PUSH  ", "steer_tag"), (clean + "\n", "you_body")])
        req_seq = self._stop_req_seq
        agent_role = self.selected_agent

        def _job() -> None:
            try:
                data = self.bridge.steer_chat(
                    clean,
                    agent_role=None if agent_role == "auto" else agent_role,
                    source=source,
                    keep_output=True,
                )
                if self._closing or self._stop_req_seq != req_seq:
                    return
                steer_seq = int(data.get("steer_seq") or 0)
                st = data.get("status")
                self._schedule_ui(
                    lambda: self._on_steer_ack(steer_seq, st if isinstance(st, dict) else None)
                )
            except Exception as exc:
                if not self._closing and self._stop_req_seq == req_seq:
                    self._schedule_ui(
                        lambda: self._append_rich([(f"Push steer error: {exc}\n", "err_body")])
                    )

        threading.Thread(target=_job, daemon=True).start()

    def _on_steer_ack(self, steer_seq: int, st: dict[str, Any] | None) -> None:
        if steer_seq > self._steer_seq_seen:
            self._steer_seq_seen = steer_seq
        if st:
            self._apply_status(st)

    def _submit(self) -> None:
        if self._has_placeholder:
            return
        prompt = self.entry.get().strip()
        if not prompt:
            return
        if self.busy:
            self.entry.delete(0, "end")
            self._push_steer(prompt, source="text")
            return
        was_speaking = self.voice_speaking
        self.entry.delete(0, "end")
        self._stop_req_seq += 1
        req_seq = self._stop_req_seq
        self.busy = True
        self.voice_speaking = False
        self._last_latest_step = ""
        self.state_label = "ULTRON · THINKING"
        self._refresh_agent_progress(18, "Running")
        self._sync_stop_controls()
        self._update_header()
        self._append_rich([("\nYOU  ", "you_tag"), (prompt + "\n", "you_body")])

        agent_role = self.selected_agent

        def _job() -> None:
            try:
                if was_speaking:
                    self.bridge.stop_voice()
                data = (
                    self.bridge.send_chat(prompt)
                    if agent_role == "auto"
                    else self.bridge.send_chat(prompt, agent_role=agent_role)
                )
                if self._closing or self._stop_req_seq != req_seq or data.get("stopped"):
                    return
                ans = data.get("answer", "")
                metrics = data.get("metrics", "")
                st = data.get("status")
                spoke = False
                if self.persona in ("jarvis", "ultron", "megatron") and ans:
                    cleaned, _ = clean_reply_and_metrics(ans, metrics)
                    if cleaned and not cleaned.startswith("Error:"):
                        self.bridge.speak_local(cleaned, self.persona)
                        if self._closing or self._stop_req_seq != req_seq:
                            self.bridge.stop()
                            return
                        spoke = True
                self._schedule_ui(lambda: self._on_reply(ans, metrics, st, req_seq, spoke))
                if spoke:
                    deadline = time.monotonic() + 25.0
                    time.sleep(0.35)
                    while (
                        time.monotonic() < deadline
                        and not self._closing
                        and self._stop_req_seq == req_seq
                    ):
                        try:
                            cur = self.bridge.get_status()
                            if not bool((cur.get("voice") or {}).get("speaking")):
                                break
                        except Exception:
                            break
                        time.sleep(0.25)
                    if not self._closing and self._stop_req_seq == req_seq:
                        self._schedule_ui(lambda: self._set_voice_speaking(False))
            except Exception as exc:
                if not self._closing and self._stop_req_seq == req_seq:
                    self._schedule_ui(lambda: self._on_error(str(exc), req_seq))

        threading.Thread(target=_job, daemon=True).start()

    def _on_reply(
        self,
        ans: str,
        metrics: str,
        st: dict[str, Any] | None,
        req_seq: int | None = None,
        spoke: bool = False,
    ) -> None:
        if req_seq is not None and req_seq != self._stop_req_seq:
            return
        self.busy = False
        self.state_label = "ULTRON VOICE · LISTENING" if self.mic_enabled else "ULTRON VOICE · STANDBY"
        if st:
            self._apply_status(st)
        if spoke:
            self.voice_speaking = True
        self._refresh_agent_progress(100, "Done")
        self._sync_stop_controls()
        self._update_header()

        body, meta = clean_reply_and_metrics(ans, metrics)
        self._tools_called_count += 1
        self._last_reasoning_text = meta or f"Agent: {self.selected_agent} · Completed"
        self.tools_called_badge.configure(
            text=f"{self._tools_called_count} turn{'s' if self._tools_called_count != 1 else ''}"
        )
        self.reasoning_detail_lbl.configure(text=self._last_reasoning_text)

        is_err = body.lower().startswith("error:") or "cannot connect to ollama" in body.lower()
        if "cannot connect to ollama" in body.lower():
            self.ollama_up = False
            self._update_header()
            body = format_friendly_error(body)

        tag_name = "MEGATRON" if self.persona == "megatron" else ("◈ ULTRON" if self.persona == "ultron" else "◈ AURA")
        tag_style = "megatron_tag" if self.persona == "megatron" else ("ultron_tag" if self.persona == "ultron" else "jarvis_tag")
        segs: list[tuple[str, str]] = [(f"{tag_name}  ", tag_style)]

        # Render inline fenced code blocks cleanly inside the Chat Well
        parts = re.split(r"```[a-zA-Z0-9_-]*\n?|```", body or "(done)")
        for idx, chunk in enumerate(parts):
            if not chunk:
                continue
            if idx % 2 == 1:
                segs.append((f"\n{chunk.strip()}\n", "code_blk"))
            else:
                segs.append((chunk, "err_body" if is_err else "ans_body"))
        segs.append(("\n", "ans_body"))
        if meta:
            segs.append((f"{meta}\n", "meta_note"))
        self._append_rich(segs)
        self._refresh_dashboard_async()

    def _on_error(self, err: str, req_seq: int | None = None) -> None:
        if req_seq is not None and req_seq != self._stop_req_seq:
            return
        self.busy = False
        self.voice_speaking = False
        self.state_label = "ULTRON · OFFLINE"
        self._refresh_agent_progress(0, "Error")
        self._sync_stop_controls()
        self._update_header()
        self._append_rich([(f"{format_friendly_error(err)}\n", "err_body")])

    def _animate_orb(self) -> None:
        if self._closing:
            return
        if self._wake_event_handle and sys.platform == "win32":
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                k32.WaitForSingleObject.restype = wintypes.DWORD
                if k32.WaitForSingleObject(wintypes.HANDLE(self._wake_event_handle), 0) == 0:
                    self.wake_and_expand(from_event=True)
            except Exception:
                pass
        self._poll_outside_click()
        active = bool(self.busy or self.voice_speaking)
        hover = bool(getattr(self, "_orb_hover", False))
        self._phase += 0.24 if active else (0.15 if (self.mic_enabled or hover) else 0.10)
        target_amp = 15.0 if active else (12.0 if self.mic_enabled else (10.6 if hover else 8.8))
        cur_amp = getattr(self, "_wave_amp", 8.8)
        self._wave_amp = cur_amp + (target_amp - cur_amp) * 0.25
        pulse = 0.5 + 0.5 * math.sin(self._phase)
        if active:
            self._spin_angle = (self._spin_angle + 18.0) % 360.0
            try:
                self.stop_canvas.itemconfigure(self.stop_ring_arc, start=self._spin_angle)
            except Exception:
                pass
            if self.busy and self._agent_progress_pct < 88:
                self._refresh_agent_progress(self._agent_progress_pct + 2, "Running")

        if self.persona == "megatron":
            color, ring = "#F87171", "#EF4444"
            interval = 33 if (active or hover) else 45
        elif self.persona == "ultron":
            color, ring = "#93C5FD", "#38BDF8"
            interval = 33 if (active or hover) else 45
        elif active:
            color, ring = "#EAF8FF", "#5CC3F2"
            interval = 33
        elif self.mic_enabled or hover:
            color, ring = "#B4E1F8", "#5CC3F2"
            interval = 36
        elif not self.ollama_up:
            color, ring = _PALETTE["amber"], "#F59E0B"
            interval = 55
        else:
            color, ring = "#5CC3F2", "#1F6FA8"
            interval = 45

        sz = int(self.orb_canvas.cget("width")) or (54 if self.expanded else 32)
        pad = (6.0 + (2.5 * (1.0 - pulse)) if active else (8.0 - 1.4 * pulse)) * (sz / 54.0)
        self.orb_canvas.coords(self.orb_outer, pad, pad, sz - pad, sz - pad)
        c_off = max(2.5, 4.0 * (sz / 54.0))
        mid = sz / 2.0
        self.orb_canvas.coords(self.orb_core, mid - c_off, mid - c_off, mid + c_off, mid + c_off)
        self.orb_canvas.itemconfigure(self.orb_outer, outline=ring)
        self.orb_canvas.itemconfigure(self.orb_core, fill=color)

        # Render 2x-supersampled wavy multi-harmonic Ultron arc-blue filament curve
        w = int(self.wave_canvas.cget("width")) or 172
        h = int(self.wave_canvas.cget("height")) or 36
        wave_bg = _PALETTE["hdr_bar"] if self.expanded else _PALETTE["gunmetal"]
        wave_photo = _render_wave_photo(
            w,
            h,
            self._phase,
            active=active,
            mic_enabled=self.mic_enabled,
            persona=self.persona,
            bg_hex=wave_bg,
            amp_override=self._wave_amp,
        )
        mid_y = h / 2.0
        amp = self._wave_amp
        pts_main: list[float] = []
        pts_dim: list[float] = []
        samples = 42
        for idx in range(samples + 1):
            u = idx / samples
            x = 6 + u * (w - 12)
            env = math.sin(u * math.pi) ** 1.05
            y1 = mid_y - amp * env * (
                0.62 * math.sin((u * 9.0 - self._phase * 1.15) * math.pi)
                + 0.28 * math.cos((u * 17.0 + self._phase * 1.85) * math.pi)
                + 0.16 * math.sin((u * 25.0 - self._phase * 2.4) * math.pi)
            )
            y2 = mid_y + (amp * 0.72) * env * math.sin((u * 6.5 + self._phase * 0.9) * math.pi)
            pts_main.extend((x, y1))
            pts_dim.extend((x, y2))
        self.wave_canvas.coords(self._fil_line_dim, *pts_dim)
        self.wave_canvas.coords(self._fil_line_main, *pts_main)
        cx, cy = w / 2.0, mid_y
        gr = 5.5 + 2.5 * pulse if (active or self.mic_enabled) else 4.5
        self.wave_canvas.coords(self._fil_glow, cx - gr, cy - gr, cx + gr, cy + gr)
        self.wave_canvas.coords(self._fil_dot, cx - 2, cy - 2, cx + 2, cy + 2)
        if wave_photo is not None:
            self._img_refs["wave"] = wave_photo
            self.wave_canvas.coords(self._wave_img_item, w / 2.0, h / 2.0)
            self.wave_canvas.itemconfigure(self._wave_img_item, image=wave_photo, state="normal")
            for cid in (self._fil_line_dim, self._fil_line_main, self._fil_glow, self._fil_dot):
                self.wave_canvas.itemconfigure(cid, state="hidden")
        else:
            self.wave_canvas.itemconfigure(self._wave_img_item, state="hidden")
            for cid in (self._fil_line_dim, self._fil_line_main, self._fil_glow, self._fil_dot):
                self.wave_canvas.itemconfigure(cid, state="normal")
            self.wave_canvas.itemconfigure(self._fil_line_main, fill="#D2EEFC" if (active or self.mic_enabled) else "#5CC3F2")
        self.root.after(interval, self._animate_orb)

    def _start_hotkey_listener(self) -> None:
        if sys.platform != "win32":
            return

        def _pump() -> None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_SPACE):
                    return
                msg = wintypes.MSG()
                while not self._hotkey_stop.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY and msg.wParam == 1:
                        self._schedule_ui(self.on_hotkey_press)
                user32.UnregisterHotKey(None, 1)
            except Exception:
                pass

        threading.Thread(target=_pump, name="aura-hotkey-pump", daemon=True).start()

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._hotkey_stop.set()
            try:
                self.mic_listener.stop()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AURA V4 · Ultron Dynamic Island Overlay")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000", help="AURA web server URL")
    args = parser.parse_args(argv)
    mutex_handle = None
    if sys.platform == "win32" and not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateMutexW.restype = wintypes.HANDLE
            mutex_handle = k32.CreateMutexW(None, False, _OVERLAY_MUTEX_NAME)
            if mutex_handle and ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                signal_overlay_wake_event()
                JarvisBridgeClient(args.server_url).show_overlay(expand=True)
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle.restype = wintypes.BOOL
                k32.CloseHandle(mutex_handle)
                return 0
        except Exception:
            pass
    try:
        DynamicIslandPill(server_url=args.server_url).run()
    finally:
        if mutex_handle and sys.platform == "win32":
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle.restype = wintypes.BOOL
                k32.CloseHandle(mutex_handle)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
