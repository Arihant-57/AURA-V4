"""
Tier-0 Zero-AI Fast Router for deterministic Windows desktop commands.

Executes instant OS commands (app launches, window placement/snapping,
monitor/window listing, OCR screen read, PC file search, and user-defined
shortcuts) without touching Ollama or Cloud LLMs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import time
from urllib.parse import quote
import webbrowser

from ..mcp.desktop_server import (
    clipboard_impl,
    close_window_impl,
    find_files_impl,
    get_monitors,
    get_windows,
    launch_app_impl,
    media_control_impl,
    open_path_impl,
    place_multiple_windows_impl,
    place_window_impl,
    read_screen_ocr_impl,
    system_info_impl,
    todo_note_impl,
)
from .personal_memory import PersonalMemoryStore, TRUST_TRUSTED


def open_website_impl(target: str, read_only: bool | None = None, browser: str = "") -> str:
    """Lazy-imported wrapper so MCP subprocess startup never hits a circular import."""
    from ..mcp.jarvis_server import open_website_impl as _impl

    if browser:
        return _impl(target, read_only=read_only, browser=browser)
    return _impl(target, read_only=read_only)


def play_youtube_impl(query: str, read_only: bool | None = None, browser: str = "") -> str:
    """Lazy-imported YouTube video resolver/player wrapper."""
    from ..mcp.jarvis_server import play_youtube_impl as _impl

    if browser:
        return _impl(query, browser=browser, read_only=read_only)
    return _impl(query, read_only=read_only)


def resolve_site_target(target: str) -> str | None:
    """Lazy-imported URL resolver."""
    from ..mcp.jarvis_server import resolve_site_target as _impl

    return _impl(target)


def resolve_multiple_site_targets(target: str) -> list[str]:
    """Lazy-imported multi-site URL resolver."""
    from ..mcp.jarvis_server import resolve_multiple_site_targets as _impl

    return _impl(target)


def _calendar_manage(action: str, read_only: bool = True, storage_dir: Path | None = None) -> str:
    """Lazy-imported calendar manager wrapper."""
    from .workspace_connector import calendar_manage_impl

    return calendar_manage_impl(action=action, read_only=read_only, storage_dir=storage_dir)


def _briefing(read_only: bool = True, storage_dir: Path | None = None) -> str:
    """Lazy-imported daily briefing wrapper."""
    from .workspace_connector import briefing_impl

    return briefing_impl(read_only=read_only, storage_dir=storage_dir)


_WAKE_PREFIX_STRIP_RE = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay)[\s,]+)?(?:jarvis|aura|ultron|megatron)[\s,:;-]+(.+)$",
    re.IGNORECASE,
)


_CODE_FILE_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|json|md|txt|yaml|yml|toml|ini|cfg|env|html|css|sql|sh|bat|ps1)$",
    re.IGNORECASE,
)

_POS_REGEX = (
    r"top-left|top-right|bottom-left|bottom-right|top left|top right|bottom left|bottom right"
    r"|left|right|top|bottom|center|max|maximize|min|minimize|restore|focus|front"
)

_SNAP_RE = re.compile(
    rf"^(?:snap|move|place|put)\s+(?P<target>.+?)\s+(?:to|on)?\s*(?P<pos>{_POS_REGEX})(?:\s+(?:on\s+)?monitor\s+(?P<mon>\d+))?$",
    re.IGNORECASE,
)
_QUICK_WIN_RE = re.compile(
    r"^(?P<pos>minimize|maximize|focus)\s+(?P<target>[A-Za-z0-9 _.-]{1,60})$",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"^(?:open|launch|start)\s+(?P<target>[A-Za-z0-9 _.-]{1,40})$",
    re.IGNORECASE,
)
_FIND_FILE_RE = re.compile(
    r"^(?:find file|locate file|search my pc for|search pc for)\s+(?P<query>.+)$",
    re.IGNORECASE,
)
_OCR_RE = re.compile(
    r"^(?:ocr|ocr screen|read screen raw)(?:\s+(?P<target>.+))?$",
    re.IGNORECASE,
)
_BIND_SHORTCUT_RE = re.compile(
    r"^(?:(?:from now on,?\s*)?when(?:ever)? i say|shortcut add|bind shortcut)\s+"
    r"[\"']?(?P<trigger>[^\"'=>,:;\-\n][^\"'=>,:;\n]{1,49}?)[\"']?\s*"
    r"(?:->|=|[:,;]\s*(?:(?:do|run)\s+)?|\s+(?:do|run)\s+)\s*"
    r"[\"']?(?P<action>.+?)[\"']?$",
    re.IGNORECASE,
)
_UNBIND_SHORTCUT_RE = re.compile(
    r"^(?:forget shortcut|unlearn(?: shortcut)?|remove shortcut|delete shortcut)\s+[\"']?(?P<trigger>.+?)[\"']?$",
    re.IGNORECASE,
)
_CLOSE_RE = re.compile(
    r"^(?:close|quit|exit)\s+(?P<target>[A-Za-z0-9 _.,+&'-]{1,80})$",
    re.IGNORECASE,
)
_QUICK_SEARCH_RE = re.compile(
    r"^(?:(?:search(?: up)?|look up|google)(?:\s+(?:(?:on|in)\s+)?(?P<site1>youtube|google|spotify))?"
    r"|(?P<site0>youtube|google|spotify)\s+search)"
    r"(?:\s+for)?\s+(?P<query>.+?)(?:\s+(?:on|in)\s+(?P<site2>youtube|google|spotify))?$",
    re.IGNORECASE,
)
_TODO_ADD_RE = re.compile(
    r"^(?:note(?:\s+down)?|take\s+a?\s*notes?|to-?do(?:\s+add)?)\s*[:,-]?\s+(?P<item>.+)$",
    re.IGNORECASE,
)
_TODO_LIST_ADD_RE = re.compile(
    r"^add\s+(?P<item>.+?)\s+to\s+(?:my\s+)?(?:to-?do(?:\s+list)?|notes?)$",
    re.IGNORECASE,
)
_TODO_DONE_RE = re.compile(
    r"^(?:complete\s+to-?do\s+(?P<item1>.+)|mark\s+(?P<item2>.+?)\s+(?:done|complete|completed)|check\s+off\s+(?P<item3>.+))$",
    re.IGNORECASE,
)
_MULTI_SNAP_CLAUSE_RE = re.compile(
    rf"^(?:(?:snap|move|place|put)\s+)?(?P<target>.+?)\s+(?:(?:to|on|in|at)\s+)?(?P<pos>{_POS_REGEX})"
    rf"(?:\s+(?:on\s+)?monitor\s+(?P<mon>\d+))?$",
    re.IGNORECASE,
)

# Adapted from Atticus (`atticus.py:FILLER` & `DEICTIC`): ignore filler words when matching learned shortcuts.
_DEICTIC_RE = re.compile(r"\b(?:this|that|these|those|it|here|there|them|him|her)\b", re.IGNORECASE)
_GIST_STOP_WORDS = frozenset({
    "a", "an", "the", "um", "uh", "umm", "hmm", "like", "so", "bro", "yeah", "just",
    "actually", "okay", "ok", "hey", "wait", "also", "now", "then", "quick", "quickly",
    "please", "can", "could", "would", "you", "me", "my", "to", "for", "on", "in",
})
_NON_APP_CLOSE_TARGETS = frozenset({
    "this", "that", "it", "issue", "issues", "pr", "pull request", "ticket", "file",
    "folder", "directory", "workspace", "repo", "repository", "connection", "loop",
})
_FOCUS_MODE_TRIGGERS = frozenset({
    "focus mode", "start focus mode", "enter focus mode", "activate focus mode",
    "enable focus mode", "work mode", "start work mode",
})
_DISTRACTION_KEYWORDS = (
    "discord", "spotify", "steam", "youtube", "netflix", "twitch",
    "whatsapp", "telegram", "reddit", "instagram", "tiktok", "epicgameslauncher", "vlc",
)
_MEDIA_PHRASE_MAP: dict[str, str] = {
    "pause music": "playpause",
    "pause media": "playpause",
    "pause playback": "playpause",
    "play music": "playpause",
    "resume music": "playpause",
    "play pause": "playpause",
    "toggle music": "playpause",
    "toggle playback": "playpause",
    "next song": "next",
    "next track": "next",
    "skip song": "next",
    "skip track": "next",
    "previous song": "previous",
    "previous track": "previous",
    "prev song": "previous",
    "prev track": "previous",
    "mute volume": "mute",
    "mute audio": "mute",
    "mute sound": "mute",
    "unmute volume": "mute",
    "unmute audio": "mute",
    "volume up": "volumeup",
    "turn up volume": "volumeup",
    "turn volume up": "volumeup",
    "increase volume": "volumeup",
    "volume down": "volumedown",
    "turn down volume": "volumedown",
    "turn volume down": "volumedown",
    "decrease volume": "volumedown",
}


def normalize_spoken_command(text: str) -> str:
    """Normalize filler/wake phrases from speech into core command text (Atticus `plain()`)."""
    norm = text.strip().rstrip(".!?").strip()
    m_wake = _WAKE_PREFIX_STRIP_RE.match(norm)
    if m_wake:
        remainder = m_wake.group(1).strip()
        if remainder:
            norm = remainder
    norm = re.sub(r"^(?:please|can you|could you|would you)\s+", "", norm, flags=re.IGNORECASE)
    norm = re.sub(r"\s+please$", "", norm, flags=re.IGNORECASE)
    return norm.strip()


def command_gist(text: str) -> list[str]:
    """Extract content-bearing tokens in order for resilient shortcut matching (Atticus `gist()`)."""
    low = normalize_spoken_command(text).lower()
    tokens = re.findall(r"[a-z0-9'.-]+", low)
    return [tok for tok in tokens if tok not in _GIST_STOP_WORDS]


def _clean_local_target(raw: str) -> str:
    cleaned = raw.strip().strip("\"'")
    cleaned = re.sub(r"^(?:the|my|this|that|a|an)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    if re.search(r"\s+(?:file|folder|directory|dir)$", cleaned, flags=re.IGNORECASE):
        stem = re.sub(r"\s+(?:file|folder|directory|dir)$", "", cleaned, flags=re.IGNORECASE).strip()
        if stem:
            cleaned = stem
    return cleaned


_NON_LOCAL_OPEN_WORDS = frozenset({
    "workspace", "repo", "repository", "codebase", "server", "route", "routes",
    "endpoint", "api", "git", "pr", "diff", "bug", "fix", "edit", "inspect",
    "debug", "refactor", "implement", "test", "tests", "youtube", "gmail",
    "calendar", "browser", "tab", "tabs", "chrome", "edge", "firefox", "settings",
    "read", "research", "explain", "summarize", "summary", "analyze", "describe",
    "understand", "inside",
})
_COMPOUND_REASONING_RE = re.compile(
    r"\b(?:read|research|explain|summarize|analyze|describe|understand|inspect|"
    r"what\s+(?:it\s+is|is\s+it|is\s+inside|are\s+inside|does\s+it\s+do)|tell\s+me)\b",
    re.IGNORECASE,
)
_NON_CODE_LOCAL_EXT_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|csv|pptx?|zip|rar|7z|png|jpe?g|gif|webp|mp4|mkv|avi|mp3|wav|lnk)$",
    re.IGNORECASE,
)


def _has_compound_reasoning_clause(norm: str) -> bool:
    """Return True when a multi-step prompt requires LLM reading, research, or explanation."""
    low = norm.lower()
    if any(sep in low for sep in (",", ";", " and ", " then ")) and _COMPOUND_REASONING_RE.search(low):
        return True
    if re.search(r"\bwhat\s+(?:it\s+is|is\s+inside|is\s+in)\b", low):
        return True
    return False


def _parse_local_open_request(norm: str) -> tuple[str, str, bool] | None:
    """Parse natural local file/folder opening commands without hijacking code or web prompts."""
    low = norm.lower()
    if "http://" in low or "https://" in low or " and then " in low or ";" in low:
        return None
    if _COMPOUND_REASONING_RE.search(low):
        return None
    prompt_tokens = set(re.findall(r"[a-z0-9]+", low))
    if prompt_tokens.intersection(_NON_LOCAL_OPEN_WORDS):
        return None

    m_explicit = re.match(
        r"^(?:please\s+)?(?:open|launch|start|show|reveal)\s+(?P<path>(?:[A-Za-z]:[\\/]|~[\\/]|%USERPROFILE%[\\/])[^|&;><]+?)(?:\s+(?:in|inside|with)\s+(?:the\s+)?(?:file\s+)?explorer)?$",
        norm,
        re.IGNORECASE,
    )
    if m_explicit:
        p = m_explicit.group("path").strip().strip("\"'")
        if p and not _CODE_FILE_EXT_RE.search(p):
            in_exp = bool(re.search(r"\bin\s+(?:the\s+)?(?:file\s+)?explorer$", norm, re.IGNORECASE))
            return p, "", in_exp

    m_exp_and = re.match(
        r"^(?:please\s+)?(?:open|launch|start)\s+(?:the\s+|my\s+)?(?:file\s+)?explorer\s*(?:,|then|and(?:\s+then)?)\s+(?:open|find|locate|show|reveal|select)\s+(?P<target>.+?)(?:\s+(?:on|in|from)\s+(?:my\s+|the\s+)?(?P<loc>desktop|downloads|documents))?$",
        norm,
        re.IGNORECASE,
    )
    if m_exp_and:
        target = _clean_local_target(m_exp_and.group("target"))
        loc = (m_exp_and.group("loc") or "").lower()
        if target and target.lower() not in {"file", "folder", "explorer", "it"} and not _CODE_FILE_EXT_RE.search(target):
            return target, loc, True

    m_in_exp = re.match(
        r"^(?:please\s+)?(?:open|show|reveal|find|locate)\s+(?P<target>.+?)(?:\s+(?:on|in|from)\s+(?:my\s+|the\s+)?(?P<loc>desktop|downloads|documents))?\s+(?:in|inside|with)\s+(?:the\s+)?(?:file\s+)?explorer$",
        norm,
        re.IGNORECASE,
    )
    if m_in_exp:
        target = _clean_local_target(m_in_exp.group("target"))
        loc = (m_in_exp.group("loc") or "").lower()
        if target and target.lower() not in {"file", "folder", "explorer", "it"} and not _CODE_FILE_EXT_RE.search(target):
            return target, loc, True

    m_loc = re.match(
        r"^(?:please\s+)?(?:open|show|launch|start)\s+(?P<target>.+?)\s+(?:on|in|from)\s+(?:my\s+|the\s+)?(?P<loc>desktop|downloads|documents)$",
        norm,
        re.IGNORECASE,
    )
    if m_loc:
        target = _clean_local_target(m_loc.group("target"))
        loc = m_loc.group("loc").lower()
        if target and target.lower() not in {"file", "folder", "it"} and not _CODE_FILE_EXT_RE.search(target):
            return target, loc, False

    m_kind = re.match(
        r"^(?:please\s+)?(?:open|show|launch|start)\s+(?P<target>.+?)\s+(?:file|folder|directory|dir)$",
        norm,
        re.IGNORECASE,
    )
    if m_kind:
        target = _clean_local_target(m_kind.group("target"))
        if (
            target
            and target.lower() not in {"file", "folder", "explorer", "it", "the", "a", "my"}
            and not _CODE_FILE_EXT_RE.search(target)
        ):
            return target, "", False

    m_ext = re.match(r"^(?:please\s+)?(?:open|show)\s+(?P<target>.+)$", norm, re.IGNORECASE)
    if m_ext:
        cand = _clean_local_target(m_ext.group("target"))
        if _NON_CODE_LOCAL_EXT_RE.search(cand) and not _CODE_FILE_EXT_RE.search(cand):
            return cand, "", False

    return None


@dataclass
class FastResult:
    """Result returned when Tier-0 FastRouter handles a command deterministically."""

    handled: bool
    answer: str = ""
    action: str = ""
    latency_ms: float = 0.0


class FastRouter:
    """Zero-LLM deterministic router with SSD-backed custom shortcuts."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = storage_dir
        self.shortcuts_path = (storage_dir / "shortcuts.json") if storage_dir else None

    def load_shortcuts(self) -> dict[str, str]:
        if self.shortcuts_path is None or not self.shortcuts_path.is_file():
            return {}
        try:
            raw = json.loads(self.shortcuts_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
        except Exception:
            return {}
        return {}

    def save_shortcut(self, trigger: str, action: str) -> None:
        if self.shortcuts_path is None:
            return
        self.shortcuts_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.load_shortcuts()
        data[trigger.strip().lower()] = action.strip()
        self.shortcuts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def delete_shortcut(self, trigger: str) -> bool:
        if self.shortcuts_path is None or not self.shortcuts_path.is_file():
            return False
        data = self.load_shortcuts()
        key = trigger.strip().lower()
        if key not in data:
            return False
        data.pop(key, None)
        self.shortcuts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True

    def _match_shortcut(self, low: str, shortcuts: dict[str, str]) -> tuple[str, str] | None:
        if low in shortcuts:
            return low, shortcuts[low]
        if _DEICTIC_RE.search(low):
            return None
        g = command_gist(low)
        if not g:
            return None
        g_sorted = sorted(g)
        for key, mapped in shortcuts.items():
            kg = command_gist(key)
            if kg and (kg == g or sorted(kg) == g_sorted):
                return key, mapped
        return None

    def _run_shortcut_step(self, step: str, read_only: bool, visited: frozenset[str]) -> FastResult:
        raw_step = re.sub(
            r"^(?:open|launch|go\s+to|visit)\s+(?=https?://)",
            "",
            step.strip(),
            flags=re.IGNORECASE,
        )
        low_step = raw_step.lower()
        if low_step.startswith(("http://", "https://")):
            if read_only:
                return FastResult(True, "READ-ONLY BLOCKED: Switch to RW mode to open URL shortcuts.", "shortcut_blocked_ro")
            webbrowser.open(raw_step)
            return FastResult(True, f"Opened shortcut URL: {raw_step}", "shortcut_url")
        sub = self._try_handle_internal(step, read_only=read_only, visited=visited)
        if sub.handled:
            return sub
        res = launch_app_impl(step.strip(), read_only=read_only)
        return FastResult(True, res, "shortcut_app")

    def _execute_shortcut(
        self,
        key: str,
        mapped: str,
        read_only: bool,
        visited: frozenset[str],
        t0: float,
    ) -> FastResult:
        if key in visited:
            return FastResult(handled=False)
        next_visited = visited | {key}

        def _with_ms(res: FastResult) -> FastResult:
            return FastResult(
                handled=res.handled,
                answer=res.answer,
                action=res.action,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        # Single-step or multi-window placement executes directly as one unit.
        single_try = (
            FastResult(handled=False)
            if (";" in mapped or "&&" in mapped or " and then " in mapped.lower() or " then " in mapped.lower())
            else self._run_shortcut_step(mapped, read_only, next_visited)
        )
        if single_try.handled and single_try.action != "launch_app":
            return _with_ms(single_try)
        if single_try.handled and " and " not in mapped.lower():
            return _with_ms(single_try)

        # Multi-step chain adapted from Atticus `shortcut_commands()`.
        parts = [p.strip() for p in re.split(r"\s*(?:;|&&|\band\s+then\b|\bthen\b|\band\b)\s*", mapped, flags=re.IGNORECASE) if p.strip()]
        if len(parts) <= 1:
            return _with_ms(self._run_shortcut_step(mapped, read_only, next_visited))

        outputs: list[str] = []
        for idx, part in enumerate(parts, 1):
            step_res = self._run_shortcut_step(part, read_only, next_visited)
            outputs.append(f"{idx}. {step_res.answer}")
            if step_res.action.endswith("_blocked_ro"):
                return _with_ms(FastResult(True, step_res.answer, "shortcut_blocked_ro"))
        return _with_ms(FastResult(True, "\n".join(outputs), "shortcut_chain"))

    def _run_focus_mode(self, read_only: bool) -> tuple[str, str]:
        if read_only:
            return "READ-ONLY BLOCKED: Switch to RW mode to run Focus Mode.", "focus_mode_blocked_ro"
        wins = get_windows(max_items=60)
        targets: list[str] = []
        seen: set[str] = set()
        for w in wins:
            proc = (w.get("process") or "").lower()
            title = (w.get("title") or "").lower()
            if "aura" in title or "visual studio code" in title:
                continue
            for kw in _DISTRACTION_KEYWORDS:
                if kw in proc or kw in title:
                    label = w.get("process") or kw
                    if label.lower() not in seen:
                        seen.add(label.lower())
                        targets.append(label)
                    break

        if targets:
            close_msg = close_window_impl(", ".join(targets), read_only=False)
        else:
            close_msg = "No distraction windows open."

        todo_msg = todo_note_impl("open", storage_dir=self.storage_dir, read_only=False)
        return f"Focus Mode active.\n{close_msg}\n{todo_msg}", "focus_mode"

    def try_handle(self, prompt: str, read_only: bool = True) -> FastResult:
        """Inspect prompt; return FastResult(handled=True) when matched deterministically."""
        return self._try_handle_internal(prompt, read_only=read_only, visited=frozenset())

    def _try_handle_internal(
        self,
        prompt: str,
        *,
        read_only: bool = True,
        visited: frozenset[str] = frozenset(),
    ) -> FastResult:
        t0 = time.perf_counter()
        clean = prompt.strip()
        if not clean:
            return FastResult(handled=False)

        norm = normalize_spoken_command(clean)
        low = norm.lower()

        def _done(answer: str, action: str) -> FastResult:
            return FastResult(
                handled=True,
                answer=answer,
                action=action,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        # 0. Teach a new shortcut BEFORE '/' check so URL actions like 'https://...' save cleanly.
        m_bind = _BIND_SHORTCUT_RE.match(norm)
        if m_bind:
            if read_only:
                return _done("READ-ONLY BLOCKED: Switch to RW mode to save custom shortcuts.", "shortcut_blocked_ro")
            trig = m_bind.group("trigger").strip().lower()
            act = m_bind.group("action").strip()
            self.save_shortcut(trig, act)
            return _done(f"Saved shortcut: '{trig}' -> '{act}'.", "shortcut_save")

        if ("/" in clean or "\\" in clean) and "http://" not in low and "https://" not in low:
            explicit_open = _parse_local_open_request(norm)
            if explicit_open is not None:
                local_target, loc, in_exp = explicit_open
                return _done(
                    open_path_impl(local_target, in_explorer=in_exp, location=loc, read_only=read_only),
                    "open_path",
                )
            return FastResult(handled=False)

        m_unbind = _UNBIND_SHORTCUT_RE.match(norm)
        if m_unbind:
            if read_only:
                return _done("READ-ONLY BLOCKED: Switch to RW mode to delete custom shortcuts.", "shortcut_blocked_ro")
            trig = m_unbind.group("trigger").strip().lower()
            removed = self.delete_shortcut(trig)
            msg = f"Removed shortcut '{trig}'." if removed else f"No saved shortcut named '{trig}'."
            return _done(msg, "shortcut_delete")

        if low in {"list shortcuts", "show shortcuts", "my shortcuts", "what shortcuts do you know"}:
            shortcuts = self.load_shortcuts()
            if not shortcuts:
                return _done("(No custom shortcuts saved yet. Teach one with: 'when I say <name>, <action>'.)", "shortcut_list")
            lines = [f"- '{k}' -> {v}" for k, v in sorted(shortcuts.items())]
            return _done("Saved shortcuts:\n" + "\n".join(lines), "shortcut_list")

        # 1. User-learned shortcuts (exact or Atticus gist match; customizes even 'focus mode')
        shortcuts = self.load_shortcuts()
        matched_shortcut = self._match_shortcut(low, shortcuts)
        if matched_shortcut is not None:
            key, mapped = matched_shortcut
            res = self._execute_shortcut(key, mapped, read_only, visited, t0)
            if res.handled:
                return res

        # 1b. Built-in Atticus Focus Mode (closes distraction apps + opens To-Do list)
        if low in _FOCUS_MODE_TRIGGERS:
            ans, act = self._run_focus_mode(read_only=read_only)
            return _done(ans, act)

        # 2. Read-only info queries & clipboard/personal memory/to-do shortcuts
        if low in {"what time is it", "current time", "time"}:
            now = datetime.now()
            return _done(now.strftime("It is %I:%M %p on %A, %B %d, %Y."), "local_time")

        if low in {"what is today's date", "whats today's date", "today's date", "date"}:
            return _done(datetime.now().strftime("Today is %A, %B %d, %Y."), "local_date")

        if low in {"read clipboard", "clipboard", "what is in my clipboard", "show clipboard"}:
            return _done(clipboard_impl("read"), "read_clipboard")

        if low in {
            "read my to-do list", "read my todo list", "show my to-do list", "show my todo list",
            "what is on my to-do list", "what's on my to-do list", "whats on my to-do list",
            "what is on my todo list", "what's on my todo list", "list todos", "list to-dos",
            "read my notes", "show my notes",
        }:
            return _done(todo_note_impl("read", storage_dir=self.storage_dir, read_only=read_only), "todo_read")

        if low in {"open my to-do list", "open my todo list", "open to-do list", "open todo list", "open my notes"}:
            return _done(todo_note_impl("open", storage_dir=self.storage_dir, read_only=read_only), "todo_open")

        m_todo_list_add = _TODO_LIST_ADD_RE.match(norm)
        if m_todo_list_add:
            return _done(
                todo_note_impl("add", text=m_todo_list_add.group("item"), storage_dir=self.storage_dir, read_only=read_only),
                "todo_add",
            )

        m_todo_add = _TODO_ADD_RE.match(norm)
        if m_todo_add:
            return _done(
                todo_note_impl("add", text=m_todo_add.group("item"), storage_dir=self.storage_dir, read_only=read_only),
                "todo_add",
            )

        m_todo_done = _TODO_DONE_RE.match(norm)
        if m_todo_done:
            item = m_todo_done.group("item1") or m_todo_done.group("item2") or m_todo_done.group("item3") or ""
            return _done(todo_note_impl("done", text=item, storage_dir=self.storage_dir, read_only=read_only), "todo_done")

        if low in {
            "check my calendar",
            "check calendar",
            "check calendar today",
            "check my calendar today",
            "what is on my calendar",
            "what's on my calendar",
            "whats on my calendar",
            "what is on my calendar today",
            "what's on my calendar today",
            "whats on my calendar today",
            "my calendar today",
            "agenda today",
            "what is my agenda today",
            "what's my agenda today",
            "today's schedule",
            "todays schedule",
            "my schedule today",
            "show my calendar",
            "show calendar",
        }:
            return _done(_calendar_manage("today", read_only=read_only, storage_dir=self.storage_dir), "jarvis_calendar")

        if low in {
            "morning briefing",
            "daily briefing",
            "jarvis briefing",
            "briefing",
            "give me my briefing",
            "give me a briefing",
            "morning report",
            "status report",
        }:
            return _done(_briefing(read_only=read_only, storage_dir=self.storage_dir), "jarvis_briefing")

        if low in {"what do you remember", "what do you remember about me", "list memory", "personal memory", "show memory"}:
            mem = PersonalMemoryStore(self.storage_dir)
            facts = mem.list_facts()
            txt = "\n".join(f"- [{f.category}] {f.text}" for f in facts) if facts else "(personal memory is empty)"
            return _done(txt, "memory_read")

        if low.startswith(("remember that ", "remember ")) and len(norm) > 10:
            if read_only:
                return _done("READ-ONLY BLOCKED: Switch to RW mode to save personal memory facts.", "memory_blocked_ro")
            body = norm.split("remember that ", 1)[1] if low.startswith("remember that ") else norm.split(" ", 1)[1]
            saved = PersonalMemoryStore(self.storage_dir).add_fact(body, category="note", trust=TRUST_TRUSTED)
            if saved:
                return _done(f"Remembered: {saved.text}", "memory_remember")
            return _done("Ignored invalid or unsafe memory entry.", "memory_ignored")

        if low.startswith(("forget ", "forget about ")) and len(norm) > 8:
            if read_only:
                return _done("READ-ONLY BLOCKED: Switch to RW mode to remove personal memory facts.", "memory_blocked_ro")
            needle = norm.split("forget about ", 1)[1] if low.startswith("forget about ") else norm.split(" ", 1)[1]
            removed = PersonalMemoryStore(self.storage_dir).remove_fact(needle)
            return _done(f"Forgot {removed} matching fact(s).", "memory_forget")

        if low in {"list monitors", "show monitors", "monitors"}:
            mons = get_monitors()
            txt = json.dumps(mons, indent=2) if mons else "No monitors detected."
            return _done(txt, "list_monitors")

        if low in {"list windows", "show windows", "open windows", "what windows are open"}:
            wins = get_windows(max_items=40)
            if not wins:
                return _done("No visible top-level windows found.", "list_windows")
            lines = [f"- [{w['process'] or 'app'}] {w['title']}" for w in wins]
            return _done("\n".join(lines), "list_windows")

        if low in {
            "system info", "system status", "pc health", "system health",
            "check system status", "check pc health", "cpu and ram usage",
            "ram and cpu usage", "hardware status",
        }:
            return _done(system_info_impl(), "system_info")

        if low in _MEDIA_PHRASE_MAP:
            return _done(media_control_impl(_MEDIA_PHRASE_MAP[low], read_only=read_only), "media_control")

        if _has_compound_reasoning_clause(norm):
            return FastResult(handled=False)

        m_find = _FIND_FILE_RE.match(norm)
        if m_find:
            return _done(find_files_impl(m_find.group("query")), "find_files")

        m_find_loc = re.match(
            r"^(?:find|locate)\s+(?:the\s+|my\s+)?(?P<query>.+?)(?:\s+(?:file|folder))?\s+(?:on|in|from)\s+(?:my\s+|the\s+)?(?P<loc>desktop|downloads|documents)$",
            norm,
            re.IGNORECASE,
        )
        if m_find_loc:
            q_find = _clean_local_target(m_find_loc.group("query"))
            if q_find and not _CODE_FILE_EXT_RE.search(q_find):
                return _done(find_files_impl(q_find, location=m_find_loc.group("loc").lower()), "find_files")

        m_ocr = _OCR_RE.match(norm)
        if m_ocr:
            target = (m_ocr.group("target") or "").strip()
            return _done(read_screen_ocr_impl(target=target), "ocr")

        # 3. Window close, placement, multi-window tiling, focus/minimize/maximize
        m_close = _CLOSE_RE.match(norm)
        if m_close:
            raw_close = m_close.group("target").strip()
            low_close = raw_close.lower()
            first_word = low_close.split()[0] if low_close else ""
            if (
                not _CODE_FILE_EXT_RE.search(raw_close)
                and low_close not in _NON_APP_CLOSE_TARGETS
                and first_word not in {"this", "that", "the", "a", "my"}
                and not any(tok in low_close for tok in ("issue", "pr", "file", "folder", "workspace", "repo", "connection"))
            ):
                return _done(close_window_impl(raw_close, read_only=read_only), "close_window")

        # Multi-window placement ("put chrome left and code right" / "snap chrome left, notepad right")
        if any(sep in low for sep in (",", ";", " and ", " + ")):
            multi_parts = [p.strip() for p in re.split(r"\s*(?:[,;+]|\band\b)\s*", norm, flags=re.IGNORECASE) if p.strip()]
            if len(multi_parts) > 1:
                specs: list[tuple[str, str, int]] = []
                all_matched = True
                for mp in multi_parts:
                    mm = _MULTI_SNAP_CLAUSE_RE.match(mp)
                    if not mm:
                        all_matched = False
                        break
                    specs.append((
                        mm.group("target").strip(),
                        mm.group("pos").lower().replace(" ", "-"),
                        int(mm.group("mon") or 1),
                    ))
                if all_matched and specs:
                    return _done(place_multiple_windows_impl(specs, read_only=read_only), "place_window")

        m_snap = _SNAP_RE.match(norm)
        if m_snap:
            pos = m_snap.group("pos").lower().replace(" ", "-")
            mon = int(m_snap.group("mon") or 1)
            ans = place_window_impl(m_snap.group("target").strip(), position=pos, monitor=mon, read_only=read_only)
            return _done(ans, "place_window")

        m_qwin = _QUICK_WIN_RE.match(norm)
        if m_qwin:
            ans = place_window_impl(m_qwin.group("target").strip(), position=m_qwin.group("pos"), read_only=read_only)
            return _done(ans, "place_window")

        # 3b. Direct YouTube video playback, compound Chrome+YouTube search, and quick search
        _non_browser_action_words = frozenset({
            "scan", "repo", "repository", "codebase", "implement", "fix",
            "edit", "write", "debug", "test", "audit", "summarize", "summary",
        })
        for yt_play_pat in (
            r"^(?:(?:please\s+)?(?:open|launch|start)\s+(?:my\s+)?(?:the\s+)?(?:google\s+)?(?P<browser>chrome|edge|msedge)(?:\s+which\s+is\s+running(?:\s+on\s+my\s+tab)?)?\s+and\s+)?(?:(?:open|go\s+to)\s+youtube(?:\s+in\s+it)?\s+and\s+)?play\s+(?:any\s+video\s+(?:from\s+that\s+url\s+)?(?:of|for)\s+|a\s+video\s+(?:of|for)\s+|the\s+video\s+(?:of|for)\s+|video\s+)?(?P<query>.+?)(?:\s+(?:on|in|from)\s+(?:the\s+)?youtube(?:\s+page)?)?$",
            r"^on\s+(?:the\s+)?youtube(?:\s+page)?[, ]+(?:you\s+can\s+see\s+)?(?:an?\s+)?(?P<query>.+?)[, ]+(?:can\s+you\s+)?play\s+it$",
        ):
            m_yt_play = re.match(yt_play_pat, norm, re.IGNORECASE)
            if m_yt_play:
                yt_browser = (m_yt_play.groupdict().get("browser") or "").strip().lower()
                yt_query = m_yt_play.group("query").strip()
                yt_q_low = yt_query.lower()
                yt_tokens = set(re.findall(r"[a-z0-9]+", yt_q_low))
                if (
                    yt_query
                    and yt_q_low not in {"it", "this", "that", "music", "media", "playback", "pause", "song", "next", "previous"}
                    and not _CODE_FILE_EXT_RE.search(yt_query)
                    and not yt_tokens.intersection(_non_browser_action_words)
                ):
                    return _done(
                        play_youtube_impl(yt_query, read_only=read_only, browser=yt_browser or "chrome"),
                        "play_youtube",
                    )

        for chrome_yt_pat in (
            r"^(?:please\s+)?(?:open|launch|start)\s+(?:my\s+)?(?:the\s+)?(?:google\s+)?(?P<browser>chrome|edge|msedge)(?:\s+which\s+is\s+running(?:\s+on\s+my\s+tab)?)?\s+and\s+(?:open|go\s+to)\s+youtube(?:\s+in\s+it)?[, ]*(?:and\s+)?search(?:\s+youtube)?(?:\s+for)?\s+(?P<query>.+?)(?:\s+(?:on|in)\s+youtube)?$",
            r"^(?:please\s+)?(?:open|launch|start)\s+(?:my\s+)?(?:the\s+)?(?:google\s+)?(?P<browser>chrome|edge|msedge)(?:\s+which\s+is\s+running(?:\s+on\s+my\s+tab)?)?\s+and\s+search\s+youtube(?:\s+in\s+youtube)?[, ]*(?:and\s+)?(?:search(?:\s+for)?\s+)?(?P<query>.+?)$",
            r"^(?:please\s+)?(?:open|launch|start)\s+(?:my\s+)?(?:the\s+)?(?:google\s+)?(?P<browser>chrome|edge|msedge)(?:\s+which\s+is\s+running(?:\s+on\s+my\s+tab)?)?\s+and\s+search(?:\s+for)?\s+(?P<query>.+?)\s+(?:on|in)\s+youtube$",
        ):
            m_cyt = re.match(chrome_yt_pat, norm, re.IGNORECASE)
            if m_cyt:
                yt_browser = m_cyt.group("browser").strip().lower()
                yt_query = m_cyt.group("query").strip()
                yt_tokens = set(re.findall(r"[a-z0-9]+", yt_query.lower()))
                if (
                    yt_query
                    and not _DEICTIC_RE.search(yt_query.lower())
                    and not _CODE_FILE_EXT_RE.search(yt_query)
                    and not yt_tokens.intersection(_non_browser_action_words)
                ):
                    yt_url = f"https://www.youtube.com/results?search_query={quote(yt_query)}"
                    return _done(
                        open_website_impl(yt_url, read_only=read_only, browser=yt_browser),
                        "quick_search",
                    )

        m_qsearch = _QUICK_SEARCH_RE.match(norm)
        if m_qsearch:
            site = (m_qsearch.group("site0") or m_qsearch.group("site1") or m_qsearch.group("site2") or "").lower()
            query = m_qsearch.group("query").strip()
            q_low = query.lower()
            if (
                site in {"youtube", "google", "spotify"}
                or norm.lower().startswith("google ")
            ) and query and not _DEICTIC_RE.search(q_low) and not _CODE_FILE_EXT_RE.search(query) and not any(
                tok in q_low for tok in ("workspace", "codebase", "repo", "file", "files", "my pc")
            ):
                if read_only:
                    return _done("READ-ONLY BLOCKED: Switch to RW mode to open browser searches.", "search_blocked_ro")
                effective_site = site or "google"
                enc = quote(query)
                if effective_site == "youtube":
                    url = f"https://www.youtube.com/results?search_query={enc}"
                elif effective_site == "spotify":
                    url = f"spotify:search:{enc}"
                else:
                    url = f"https://www.google.com/search?q={enc}"
                webbrowser.open(url)
                return _done(f"Searched '{query}' on {effective_site.title()}.", "quick_search")

        local_open = _parse_local_open_request(norm)
        if local_open is not None:
            local_target, loc, in_exp = local_open
            return _done(
                open_path_impl(local_target, in_explorer=in_exp, location=loc, read_only=read_only),
                "open_path",
            )

        # 4. Explicit Chrome/Edge + sites, browser + sites, or known website/app open
        _b_art = r"(?:(?:a|the|my)\s+)?(?:(?:new|fresh)\s+)?(?:google\s+)?"
        _t_spec = r"(?:(?:a|the|new|a\s+new)\s+tab\s+(?:in|on|using)\s+" + _b_art + r"|" + _b_art + r")(?P<browser>chrome|edge|msedge)(?:\s+tab)?"
        for chrome_pat in (
            r"^(?:please\s+)?(?:open|launch|start)\s+" + _t_spec + r"\s+(?:(?:and\s+)?(?:open|go\s+to|visit|navigate\s+to|load|launch)\s+|and\s+)(?:like\s+|the\s+)?(?P<target>.+?)[.?!]*$",
            r"^(?:please\s+)?(?:open|launch|go\s+to|visit|load)\s+(?:like\s+|the\s+)?(?P<target>.+?)\s+(?:in|on|using|with)\s+" + _t_spec + r"[.?!]*$",
            r"^(?:please\s+)?(?:open|launch|start)\s+" + _t_spec + r"\s+(?:with|to|for)\s+(?:like\s+|the\s+)?(?P<target>.+?)[.?!]*$",
        ):
            m_chrome = re.match(chrome_pat, norm, re.IGNORECASE)
            if m_chrome:
                site = m_chrome.group("target").strip()
                browser_name = m_chrome.group("browser").strip().lower()
                site_tokens = set(re.findall(r"[a-z0-9]+", site.lower()))
                if (
                    not _CODE_FILE_EXT_RE.search(site)
                    and not site_tokens.intersection(_non_browser_action_words)
                    and resolve_multiple_site_targets(site)
                ):
                    return _done(open_website_impl(site, read_only=read_only, browser=browser_name), "open_website")

        m_browser_open = re.match(
            r"^(?:please\s+)?(?:open|launch|start)\s+(?:the\s+)?(?:web\s+)?browser\s+and\s+(?:open|go\s+to|visit|load)\s+(?:like\s+|the\s+)?(?P<target>.+?)[.?!]*$",
            norm,
            re.IGNORECASE,
        )
        if m_browser_open:
            site = m_browser_open.group("target").strip()
            if not _CODE_FILE_EXT_RE.search(site) and resolve_site_target(site) is not None:
                return _done(open_website_impl(site, read_only=read_only), "open_website")

        m_any_open = re.match(
            r"^(?:please\s+)?(?:open|launch|start|go\s+to|visit)\s+(?P<target>.+?)[.?!]*$",
            norm,
            re.IGNORECASE,
        )
        if m_any_open:
            raw_site = m_any_open.group("target").strip()
            candidate_site = re.sub(r"^(?:like|the)\s+", "", raw_site, flags=re.IGNORECASE).strip()
            site_tokens = set(re.findall(r"[a-z0-9]+", candidate_site.lower()))
            if (
                not _CODE_FILE_EXT_RE.search(candidate_site)
                and not raw_site.lower().startswith(("this ", "file ", "folder ", "workspace ", "repo "))
                and not site_tokens.intersection(_non_browser_action_words)
                and resolve_multiple_site_targets(candidate_site)
            ):
                return _done(open_website_impl(candidate_site, read_only=read_only), "open_website")

        m_open = _OPEN_RE.match(norm)
        if m_open:
            raw_target = m_open.group("target").strip()
            target = re.sub(r"^(?:like|the|my|a|an)\s+", "", raw_target, flags=re.IGNORECASE).strip()
            lower_raw = raw_target.lower()
            lower_target = target.lower()
            if (
                not _CODE_FILE_EXT_RE.search(target)
                and " and " not in lower_target
                and not lower_raw.startswith(("this ", "file ", "folder ", "workspace ", "repo "))
            ):
                if resolve_site_target(target) is not None:
                    return _done(open_website_impl(target, read_only=read_only), "open_website")
                _known_folder_map = {
                    "desktop": Path.home() / "Desktop",
                    "downloads": Path.home() / "Downloads",
                    "download": Path.home() / "Downloads",
                    "documents": Path.home() / "Documents",
                    "document": Path.home() / "Documents",
                }
                if lower_target in _known_folder_map:
                    folder_p = _known_folder_map[lower_target]
                    return _done(open_path_impl(str(folder_p), read_only=read_only), "open_path")
                _known_app_tokens = {
                    "chrome", "google chrome", "chrome browser",
                    "edge", "microsoft edge", "msedge", "edge browser",
                    "firefox", "brave", "notepad", "calc", "calculator",
                    "vs code", "vscode", "code", "terminal", "paint",
                    "spotify", "discord", "explorer", "file explorer",
                    "task manager", "settings", "whatsapp", "telegram",
                    "powershell", "cmd",
                }
                app_cand = re.sub(
                    r"\s+(?:browser|app|tab)$",
                    "",
                    re.sub(r"^(?:(?:a|the|new)\s+)+", "", lower_target),
                ).strip()
                if lower_target in _known_app_tokens or app_cand in _known_app_tokens:
                    return _done(launch_app_impl(app_cand if app_cand in _known_app_tokens else target, read_only=read_only), "launch_app")

                target_words = lower_target.split()
                _web_or_media_tokens = {
                    "on", "in", "from", "page", "youtube", "video", "videos",
                    "shot", "shots", "short", "shorts", "trailer", "tab", "url",
                    "link", "website", "site", "result", "results",
                }
                if (
                    not lower_raw.startswith("the ")
                    and len(target_words) <= 3
                    and not set(target_words).intersection(_web_or_media_tokens)
                ):
                    if not any(w in target_words for w in ("workspace", "repo", "repository", "readme", "codebase")):
                        path_ans = open_path_impl(target, read_only=read_only)
                        if not path_ans.startswith("NOT FOUND:"):
                            return _done(path_ans, "open_path")
                    ans = launch_app_impl(target, read_only=read_only)
                    return _done(ans, "launch_app")

        return FastResult(handled=False)
