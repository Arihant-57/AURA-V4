"""
AURA Browser MCP Server (DOM-aware Web Automation via Chrome/Edge).

Phase 1 Upgrade: Eliminates reliance on screen OCR for web tasks by exposing
structured DOM snapshots, indexed interactive element clicking, form typing,
and scrolling over Selenium Chrome (with Edge fallback).

Security & safety:
- Reuses fail-closed `check_ssrf()` from `jarvis_server` before navigating.
- Mutating actions (`browser_navigate`, `browser_click`, `browser_type`,
  `browser_scroll`, `browser_back`) strictly refuse when `AURA_READ_ONLY=1`.
- Uses an isolated persistent automation profile (`~/.aura/browser_profile`)
  so user logins stay clean without locking the primary browser profile.
- Suppresses console flashes on Windows (`creation_flags = 0x08000000`).
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

_SRC_DIR = str(Path(__file__).resolve().parents[2])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from mcp.server import MCPServer

from aura.mcp.jarvis_server import check_ssrf, resolve_site_target


mcp = MCPServer("AURA Browser Server")

CREATE_NO_WINDOW = 0x08000000

_SNAPSHOT_JS = r"""
const maxItems = arguments[0] || 40;
const maxChars = arguments[1] || 3000;

for (const old of document.querySelectorAll('[data-aura-idx], [data-aura-ref]')) {
  old.removeAttribute('data-aura-idx');
  old.removeAttribute('data-aura-ref');
}

const cleanText = (s, maxLen = 72) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, maxLen);

const cssEsc = (v) => {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(v);
  return String(v).replace(/["\\]/g, '\\$&');
};

const isVisible = (el) => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
  if (el.closest('[hidden], [aria-hidden="true"], template, noscript')) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return false;
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) return false;
  return true;
};

const getAriaLabelledBy = (el) => {
  const ids = (el.getAttribute('aria-labelledby') || '').trim();
  if (!ids) return '';
  return ids.split(/\s+/).map((id) => {
    const t = document.getElementById(id);
    return t ? (t.innerText || t.textContent || '') : '';
  }).join(' ');
};

const getAssociatedLabel = (el) => {
  if (el.id) {
    const lbl = document.querySelector(`label[for="${cssEsc(el.id)}"]`);
    if (lbl) return lbl.innerText || lbl.textContent || '';
  }
  const wrap = el.closest('label');
  if (wrap) return wrap.innerText || wrap.textContent || '';
  return '';
};

const getRole = (el, tag, inputType) => {
  const explicit = cleanText(el.getAttribute('role') || '', 24).toLowerCase();
  if (explicit) return explicit;
  if (tag === 'a') return 'link';
  if (tag === 'button' || tag === 'summary') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'input') {
    if (inputType === 'search') return 'searchbox';
    if (inputType === 'checkbox') return 'checkbox';
    if (inputType === 'radio') return 'radio';
    if (inputType === 'submit' || inputType === 'button' || inputType === 'reset') return 'button';
    return 'textbox';
  }
  if (el.isContentEditable) return 'textbox';
  return '';
};

const getLabel = (el, tag, inputType) => {
  const aria = getAriaLabelledBy(el) || el.getAttribute('aria-label') || '';
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    if (tag === 'input' && (inputType === 'submit' || inputType === 'button' || inputType === 'reset')) {
      return cleanText(aria || el.value || el.getAttribute('title') || el.getAttribute('name') || el.id || inputType);
    }
    return cleanText(
      aria ||
      getAssociatedLabel(el) ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      el.getAttribute('name') ||
      el.id ||
      (tag === 'input' ? (inputType || 'input') : tag)
    );
  }
  const imgAlt = el.querySelector('img[alt]') ? el.querySelector('img[alt]').getAttribute('alt') : '';
  const childAria = el.querySelector('[aria-label]') ? el.querySelector('[aria-label]').getAttribute('aria-label') : '';
  return cleanText(
    aria ||
    el.innerText ||
    el.textContent ||
    el.getAttribute('title') ||
    imgAlt ||
    childAria ||
    el.getAttribute('placeholder') ||
    el.getAttribute('name') ||
    el.id ||
    ''
  );
};

const cleanHref = (el, tag) => {
  if (tag !== 'a') return '';
  const raw = (el.getAttribute('href') || '').trim();
  if (!raw || raw === '#' || raw.toLowerCase().startsWith('javascript:')) return '';
  try {
    const u = new URL(raw, window.location.href);
    for (const k of Array.from(u.searchParams.keys())) {
      if (/^(utm_|ved$|ei$|opi$|sca_esv$|sa$|usg$|gclid$|fbclid$)/i.test(k)) {
        u.searchParams.delete(k);
      }
    }
    const out = (u.origin === window.location.origin) ? (u.pathname + u.search) : u.href;
    return out.slice(0, 64);
  } catch (e) {
    return raw.slice(0, 64);
  }
};

const selector = [
  'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
  '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="searchbox"]',
  '[role="combobox"]', '[role="checkbox"]', '[role="radio"]', '[role="switch"]',
  '[role="tab"]', '[role="menuitem"]', '[role="option"]',
  '[contenteditable="true"]', '[tabindex]:not([tabindex="-1"])'
].join(', ');

const rawNodes = Array.from(document.querySelectorAll(selector));
const keptSet = new Set();
const seenLinks = new Set();
const candidates = [];
const viewH = window.innerHeight || document.documentElement.clientHeight || 900;

for (let domIdx = 0; domIdx < rawNodes.length; domIdx++) {
  const el = rawNodes[domIdx];
  if (!isVisible(el)) continue;
  const tag = el.tagName.toLowerCase();
  const inputType = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  const label = getLabel(el, tag, inputType);
  const isFormField = (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable);
  if (!label && !isFormField) continue;

  const parentClickable = el.parentElement ? el.parentElement.closest('a[href], button, [role="button"], [role="link"]') : null;
  if (parentClickable && keptSet.has(parentClickable)) {
    const pLabel = cleanText(parentClickable.innerText || parentClickable.getAttribute('aria-label') || '');
    if (pLabel && pLabel === label) continue;
  }

  const href = cleanHref(el, tag);
  if (tag === 'a' && href) {
    const linkKey = `${label}|${href}`;
    if (seenLinks.has(linkKey)) continue;
    seenLinks.add(linkKey);
  }

  const rect = el.getBoundingClientRect();
  const inView = rect.bottom >= 0 && rect.top <= viewH;
  const inChrome = Boolean(el.closest('nav, footer, [role="navigation"], [role="contentinfo"]'));
  const inMain = Boolean(el.closest('main, [role="main"], article, form, dialog, [role="dialog"]'));
  const isPrimary = isFormField || (!inChrome && (inView || inMain));

  keptSet.add(el);
  candidates.push({ el, domIdx, tag, inputType, label, href, isPrimary });
}

let chosen = candidates;
if (candidates.length > maxItems) {
  const primary = candidates.filter((c) => c.isPrimary);
  const secondary = candidates.filter((c) => !c.isPrimary);
  chosen = [...primary, ...secondary].slice(0, maxItems);
  chosen.sort((a, b) => a.domIdx - b.domIdx);
}

const items = [];
let idx = 1;
for (const c of chosen) {
  const { el, tag, inputType, label, href } = c;
  const ref = 'e' + idx;
  el.setAttribute('data-aura-idx', String(idx));
  el.setAttribute('data-aura-ref', ref);

  const role = getRole(el, tag, inputType);
  let val = '';
  if (tag === 'input' && inputType === 'password') {
    val = el.value ? '***' : '';
  } else if (tag === 'input' && !['submit', 'button', 'reset', 'checkbox', 'radio', 'hidden', 'file'].includes(inputType)) {
    val = cleanText(el.value || '', 40);
  } else if (tag === 'textarea') {
    val = cleanText(el.value || '', 40);
  } else if (tag === 'select' && el.selectedIndex >= 0 && el.options[el.selectedIndex]) {
    val = cleanText(el.options[el.selectedIndex].text || '', 40);
  } else if (el.isContentEditable) {
    val = cleanText(el.innerText || '', 40);
  }

  let checked = null;
  if (role === 'checkbox' || role === 'radio' || role === 'switch') {
    checked = Boolean(el.checked || el.getAttribute('aria-checked') === 'true');
  }
  const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
  const expanded = el.getAttribute('aria-expanded');

  items.push({
    idx,
    ref,
    tag,
    role,
    inputType,
    label,
    val,
    checked,
    disabled,
    expanded: (expanded === 'true' || expanded === 'false') ? expanded : '',
    href,
    elId: el.id || '',
    elName: el.getAttribute('name') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    placeholder: el.getAttribute('placeholder') || '',
  });
  idx += 1;
}

const mainEl = document.querySelector('main, [role="main"], article');
let rawText = '';
if (mainEl && mainEl.innerText && mainEl.innerText.trim().length >= 120) {
  rawText = mainEl.innerText;
} else {
  rawText = (document.body && document.body.innerText) ? document.body.innerText : '';
}
rawText = rawText
  .replace(/[ \t]+\n/g, '\n')
  .replace(/\n{3,}/g, '\n\n')
  .trim()
  .slice(0, maxChars);

const docEl = document.documentElement || document.body;
const scrollY = Math.round(window.scrollY || (docEl ? docEl.scrollTop : 0) || 0);
const scrollH = Math.round(Math.max(docEl ? docEl.scrollHeight : 0, document.body ? document.body.scrollHeight : 0));

return {
  title: document.title || '(untitled)',
  url: window.location.href || '',
  items,
  text: rawText,
  scrollY,
  viewH: Math.round(viewH),
  scrollH,
  totalControls: candidates.length,
};
"""


def _is_read_only(read_only: bool | None = None) -> bool:
    if read_only is not None:
        return read_only
    return os.environ.get("AURA_READ_ONLY", "") == "1"


def _default_profile_dir() -> Path:
    custom = os.environ.get("AURA_BROWSER_PROFILE", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path.home() / ".aura" / "browser_profile").resolve()


def _configure_driver(drv: Any) -> Any:
    for fn, val in (("set_page_load_timeout", 15), ("set_script_timeout", 10)):
        try:
            getattr(drv, fn)(val)
        except Exception:
            pass
    return drv


def _create_webdriver(headless: bool = False) -> Any:
    """Create a quiet Chrome (or Edge fallback) WebDriver instance."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService

    profile_dir = _default_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    use_headless = headless or (os.environ.get("AURA_BROWSER_HEADLESS", "0") == "1")
    opts = ChromeOptions()
    opts.page_load_strategy = "eager"
    try:
        try:
            from .desktop_server import _lookup_app_path
        except ImportError:
            from aura.mcp.desktop_server import _lookup_app_path
        chrome_bin = _lookup_app_path("chrome.exe")
        if chrome_bin and Path(chrome_bin).is_file():
            opts.binary_location = chrome_bin
    except Exception:
        pass
    if use_headless:
        opts.add_argument("--headless=new")
    else:
        opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    svc = ChromeService()
    if sys.platform == "win32":
        svc.creation_flags = CREATE_NO_WINDOW
    try:
        return _configure_driver(webdriver.Chrome(options=opts, service=svc))
    except Exception:
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.edge.service import Service as EdgeService

        e_opts = EdgeOptions()
        e_opts.page_load_strategy = "eager"
        if use_headless:
            e_opts.add_argument("--headless=new")
        else:
            e_opts.add_argument(f"--user-data-dir={profile_dir}")
        e_opts.add_argument("--disable-gpu")
        e_svc = EdgeService()
        if sys.platform == "win32":
            e_svc.creation_flags = CREATE_NO_WINDOW
        return _configure_driver(webdriver.Edge(options=e_opts, service=e_svc))


class BrowserManager:
    """Thread-safe lazy singleton manager for the active browser session."""

    def __init__(self, driver_factory: Callable[[bool], Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._driver: Any = None
        self._driver_factory = driver_factory or _create_webdriver
        self._allow_local: bool = False
        self._last_items: dict[str, dict[str, Any]] = {}

    @property
    def allow_local(self) -> bool:
        return self._allow_local

    def set_allow_local(self, allow: bool) -> None:
        self._allow_local = bool(allow)

    def record_snapshot(self, snap: dict[str, Any]) -> None:
        mapping: dict[str, dict[str, Any]] = {}
        for it in snap.get("items") or ():
            idx_str = str(it.get("idx", ""))
            ref_str = str(it.get("ref") or f"e{idx_str}").lower()
            if idx_str:
                mapping[idx_str] = it
                mapping[ref_str] = it
        self._last_items = mapping

    def get_ref_meta(self, idx: str) -> dict[str, Any] | None:
        return self._last_items.get(str(idx).lower())

    @staticmethod
    def ensure_live_window(drv: Any) -> bool:
        try:
            handles = list(drv.window_handles or [])
        except Exception:
            return False
        if not handles:
            return False
        try:
            cur = drv.current_window_handle
            if cur in handles:
                _ = drv.title
                return True
        except Exception:
            pass
        for h in reversed(handles):
            try:
                drv.switch_to.window(h)
                _ = drv.title
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def tab_count(drv: Any) -> int:
        try:
            return len(drv.window_handles or [])
        except Exception:
            return 1

    def get_driver(self, headless: bool = False, create: bool = True) -> Any:
        with self._lock:
            if self._driver is not None:
                if self.ensure_live_window(self._driver):
                    return self._driver
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
                self._last_items = {}
            if not create:
                return None
            self._driver = self._driver_factory(headless)
            return self._driver

    def close(self) -> bool:
        with self._lock:
            drv = self._driver
            self._driver = None
            self._allow_local = False
            self._last_items = {}
        if drv is None:
            return False
        try:
            drv.quit()
        except Exception:
            pass
        return True


_MANAGER = BrowserManager()
atexit.register(_MANAGER.close)


def _format_snapshot(snap: dict[str, Any], max_chars: int = 3000, tab_count: int = 1) -> str:
    budget = max(400, min(int(max_chars), 8000))
    if budget == 3000:
        budget = 2900
    title = snap.get("title") or "(untitled)"
    url = snap.get("url") or ""
    items = snap.get("items") or []
    text = snap.get("text") or "(empty page)"

    header_lines = [f"Title: {title}", f"URL: {url}"]
    meta_parts: list[str] = []
    scroll_y = int(snap.get("scrollY") or 0)
    view_h = int(snap.get("viewH") or 0)
    scroll_h = int(snap.get("scrollH") or 0)
    if scroll_h > view_h > 0:
        pct = min(100, max(0, round((scroll_y / max(1, scroll_h - view_h)) * 100)))
        meta_parts.append(f"Scroll: {pct}% ({scroll_y}/{scroll_h}px)")
    if tab_count > 1:
        meta_parts.append(f"Tabs: {tab_count}")
    if meta_parts:
        header_lines.append(" | ".join(meta_parts))

    header = "\n".join(header_lines)
    reserved_text = min(650, max(250, budget // 3))
    elem_budget = max(200, budget - len(header) - reserved_text - 32)

    elem_lines: list[str] = []
    used_elem_chars = 0
    omitted = max(0, int(snap.get("totalControls") or len(items)) - len(items))
    if items:
        elem_lines.append("Interactive Elements:")
        for i, it in enumerate(items):
            ref = it.get("ref") or f"e{it['idx']}"
            tag = it.get("tag") or "el"
            label = it.get("label") or ""
            role = it.get("role") or ""

            attrs: list[str] = []
            redundant_role = (
                (tag == "button" and role == "button")
                or (tag == "a" and role == "link")
                or (tag == "select" and role == "combobox")
                or (tag == "textarea" and role == "textbox")
            )
            if role and not redundant_role:
                attrs.append(role)
            if it.get("checked") is True:
                attrs.append("checked")
            elif it.get("checked") is False:
                attrs.append("unchecked")
            if it.get("disabled"):
                attrs.append("disabled")
            if it.get("expanded") in ("true", "false"):
                attrs.append(f"expanded={it['expanded']}")
            if it.get("val"):
                attrs.append(f'value="{it["val"]}"')

            attr_part = f" [{', '.join(attrs)}]" if attrs else ""
            href_part = f" -> {it['href']}" if it.get("href") else ""
            line = f"  [{ref}] <{tag}> {label}{attr_part}{href_part}".rstrip()
            if elem_lines and (used_elem_chars + len(line) + 1) > elem_budget and len(elem_lines) > 1:
                omitted += len(items) - i
                break
            elem_lines.append(line)
            used_elem_chars += len(line) + 1
        if omitted > 0:
            elem_lines.append(f"  ... (+{omitted} more controls)")

    body_parts = [header]
    if elem_lines:
        body_parts.append("\n".join(elem_lines))
    prefix = "\n".join(body_parts)
    text_budget = max(180, budget - len(prefix) - len("\n\nVisible Text:\n") - 20)
    clipped_text = text
    if len(clipped_text) > text_budget:
        clipped_text = clipped_text[:text_budget].rstrip() + "..."
    return f"{prefix}\n\nVisible Text:\n{clipped_text}"


def _xpath_literal(val: str) -> str:
    if "'" not in val:
        return f"'{val}'"
    if '"' not in val:
        return f'"{val}"'
    parts = val.split("'")
    joined = ", \"'\", ".join(f"'{p}'" for p in parts)
    return f"concat({joined})"


def _first_preferred(matches: list[Any]) -> Any | None:
    if not matches:
        return None
    for el in matches:
        try:
            if el.is_displayed():
                return el
        except Exception:
            continue
    return matches[0]


def _recover_stale_ref(drv: Any, meta: dict[str, Any]) -> Any | None:
    from selenium.webdriver.common.by import By

    tag = meta.get("tag") or "*"
    el_id = meta.get("elId") or ""
    if el_id:
        hit = _first_preferred(drv.find_elements(By.ID, el_id))
        if hit is not None:
            return hit
    for attr_name, key in (("name", "elName"), ("aria-label", "ariaLabel"), ("placeholder", "placeholder")):
        val = meta.get(key) or ""
        if val:
            lit = _xpath_literal(val)
            hit = _first_preferred(drv.find_elements(By.XPATH, f"//{tag}[@{attr_name}={lit}]"))
            if hit is not None:
                return hit
    href = meta.get("href") or ""
    if tag == "a" and href:
        lit = _xpath_literal(href)
        hit = _first_preferred(drv.find_elements(By.XPATH, f"//a[contains(@href, {lit})]"))
        if hit is not None:
            return hit
    label = meta.get("label") or ""
    if label:
        low = _xpath_literal(label.lower())
        hit = _first_preferred(
            drv.find_elements(
                By.XPATH,
                f"//{tag}[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {low})]",
            )
        )
        if hit is not None:
            return hit
    return None


def _find_element(drv: Any, target: str, mgr: BrowserManager | None = None) -> Any:
    """Resolve an interactive DOM element by `[e1]` / `[1]` ref, CSS selector, label/placeholder, or text."""
    from selenium.webdriver.common.by import By

    clean = (target or "").strip()
    if not clean:
        raise ValueError("Target element selector or ref cannot be empty.")

    m_idx = re.match(r"^\[?[eE]?(\d+)\]?$", clean)
    if m_idx:
        idx = m_idx.group(1)
        matches = drv.find_elements(By.CSS_SELECTOR, f'[data-aura-ref="e{idx}"], [data-aura-idx="{idx}"]')
        hit = _first_preferred(matches)
        if hit is not None:
            return hit
        if mgr is not None:
            meta = mgr.get_ref_meta(idx)
            if meta is not None:
                recovered = _recover_stale_ref(drv, meta)
                if recovered is not None:
                    return recovered
        raise LookupError(f"Element [e{idx}] not found. Run browser_snapshot first.")

    if clean.startswith(("#", ".", "[")) or ("=" in clean and " " not in clean):
        hit = _first_preferred(drv.find_elements(By.CSS_SELECTOR, clean))
        if hit is not None:
            return hit

    low_lit = _xpath_literal(clean.lower())
    xpath = (
        f'//*[self::a or self::button or self::input or self::textarea or self::select or self::summary '
        f'or @role="button" or @role="link" or @role="textbox" or @role="searchbox" '
        f'or @role="combobox" or @role="tab" or @role="menuitem"]'
        f'[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), {low_lit}) '
        f'or contains(translate(@aria-label, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), {low_lit}) '
        f'or contains(translate(@placeholder, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), {low_lit}) '
        f'or contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), {low_lit}) '
        f'or contains(translate(@name, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), {low_lit})]'
    )
    hit = _first_preferred(drv.find_elements(By.XPATH, xpath))
    if hit is not None:
        return hit

    hit = _first_preferred(drv.find_elements(By.CSS_SELECTOR, clean))
    if hit is not None:
        return hit
    raise LookupError(f"Could not find interactive element matching '{clean}'.")


def _wait_for_dom_settle(
    drv: Any,
    *,
    timeout: float = 2.0,
    settle_gap: float = 0.12,
    min_wait: float = 0.0,
    required_stable: int = 1,
) -> None:
    """Wait concisely for page readyState and DOM/SPA mutations to settle."""
    if min_wait > 0:
        time.sleep(min_wait)
    deadline = time.monotonic() + timeout
    prev_sig: tuple[Any, ...] | None = None
    stable_hits = 0
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            BrowserManager.ensure_live_window(drv)
            state = drv.execute_script(
                "return [document.readyState || '', window.location.href || '', "
                "(document.body && document.body.innerText ? document.body.innerText.length : 0), "
                "document.querySelectorAll('a,button,input,textarea,select').length];"
            )
        except Exception:
            time.sleep(0.1)
            prev_sig = None
            stable_hits = 0
            continue
        if not isinstance(state, list) or len(state) < 4:
            break
        ready = str(state[0])
        sig = (str(state[1]), int(state[2] or 0), int(state[3] or 0))
        elapsed = time.monotonic() - started
        ready_ok = (ready == "complete") or (ready == "interactive" and elapsed >= 1.0)
        if ready_ok and sig == prev_sig:
            stable_hits += 1
            if stable_hits >= required_stable:
                return
        else:
            stable_hits = 0
        prev_sig = sig
        time.sleep(settle_gap)


def _check_target_url_ssrf(drv: Any, raw_target: str, mgr: BrowserManager) -> str | None:
    if mgr.allow_local:
        return None
    raw = (raw_target or "").strip()
    if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
        return None
    cur_url = ""
    try:
        cur_url = drv.current_url or ""
    except Exception:
        pass
    resolved = urljoin(cur_url, raw) if cur_url.startswith(("http://", "https://")) else raw
    parsed = urlparse(resolved)
    if parsed.scheme.lower() not in {"http", "https"}:
        return f"SSRF BLOCKED: Unsupported URL scheme '{parsed.scheme}'."
    cur_host = (urlparse(cur_url).hostname or "").strip().rstrip(".").lower()
    tgt_host = (parsed.hostname or "").strip().rstrip(".").lower()
    if tgt_host != cur_host:
        err = check_ssrf(resolved)
        if err:
            return f"SSRF BLOCKED: {err}"
    return None


def _check_element_target_ssrf(drv: Any, el: Any, mgr: BrowserManager, *, is_submit: bool = False) -> str | None:
    if mgr.allow_local:
        return None
    try:
        urls = drv.execute_script(
            "const el = arguments[0], isSub = arguments[1];"
            "const out = [];"
            "const a = el.closest ? el.closest('a[href]') : null;"
            "if (el.getAttribute && el.getAttribute('href')) out.push(el.getAttribute('href'));"
            "else if (a && a.getAttribute('href')) out.push(a.getAttribute('href'));"
            "if (el.getAttribute && el.getAttribute('formaction')) out.push(el.getAttribute('formaction'));"
            "const tag = (el.tagName || '').toLowerCase();"
            "const typ = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();"
            "const triggersForm = isSub || (tag === 'button' && (!typ || typ === 'submit')) || (tag === 'input' && (typ === 'submit' || typ === 'image'));"
            "if (triggersForm && el.form && el.form.getAttribute('action')) out.push(el.form.getAttribute('action'));"
            "return out;",
            el,
            is_submit,
        ) or []
    except Exception:
        return None
    for candidate in urls:
        blocked = _check_target_url_ssrf(drv, str(candidate), mgr)
        if blocked:
            return blocked
    return None


def _enforce_current_url_ssrf(
    drv: Any,
    mgr: BrowserManager,
    before_handles: list[str] | None = None,
) -> str | None:
    if mgr.allow_local:
        return None
    try:
        cur_url = (drv.current_url or "").strip()
    except Exception:
        return None
    if not cur_url or cur_url.startswith("about:blank"):
        return None
    err = check_ssrf(cur_url)
    if err:
        try:
            cur_handle = drv.current_window_handle
            if before_handles is not None and cur_handle not in before_handles and len(drv.window_handles) > 1:
                drv.close()
                drv.switch_to.window(before_handles[-1])
            else:
                drv.get("about:blank")
        except Exception:
            pass
        return f"SSRF BLOCKED: {err}"
    return None


def _switch_to_new_or_live_tab(drv: Any, before_handles: list[str]) -> bool:
    try:
        after_handles = list(drv.window_handles or [])
    except Exception:
        return False
    new_handles = [h for h in after_handles if h not in before_handles]
    if new_handles:
        try:
            drv.switch_to.window(new_handles[-1])
            return True
        except Exception:
            pass
    return BrowserManager.ensure_live_window(drv)


def snapshot_impl(
    max_chars: int = 3000,
    manager: BrowserManager | None = None,
) -> str:
    mgr = manager or _MANAGER
    drv = mgr.get_driver(create=False)
    if drv is None:
        return "No active browser session. Call browser_navigate first."
    ssrf_err = _enforce_current_url_ssrf(drv, mgr)
    if ssrf_err:
        return ssrf_err
    cap = max(400, min(int(max_chars), 8000))
    snap = drv.execute_script(_SNAPSHOT_JS, 40, cap)
    mgr.record_snapshot(snap)
    return _format_snapshot(snap, max_chars=cap, tab_count=mgr.tab_count(drv))


def navigate_impl(
    url: str,
    headless: bool = False,
    read_only: bool | None = None,
    manager: BrowserManager | None = None,
    allow_local: bool = False,
) -> str:
    ro = _is_read_only(read_only)
    resolved = resolve_site_target(url) or url.strip()
    if not resolved:
        return "ERROR: URL or site name cannot be empty."
    if not allow_local:
        err = check_ssrf(resolved)
        if err:
            return f"SSRF BLOCKED: {err}"
    if ro:
        return f"READ-ONLY BLOCKED: Switch to RW mode to navigate browser to {resolved}."

    from selenium.common.exceptions import TimeoutException, WebDriverException

    mgr = manager or _MANAGER
    mgr.set_allow_local(allow_local)
    drv = mgr.get_driver(headless=headless, create=True)
    timed_out = False
    try:
        drv.get(resolved)
    except TimeoutException:
        timed_out = True
        try:
            drv.execute_script("window.stop();")
        except Exception:
            pass
    except WebDriverException as exc:
        short = str(exc).splitlines()[0][:180]
        return f"ERROR: Failed to navigate to {resolved}: {short}"

    ssrf_err = _enforce_current_url_ssrf(drv, mgr)
    if ssrf_err:
        return ssrf_err
    _wait_for_dom_settle(drv, timeout=2.2 if not timed_out else 0.5, settle_gap=0.10)
    ssrf_err = _enforce_current_url_ssrf(drv, mgr)
    if ssrf_err:
        return ssrf_err
    out = snapshot_impl(manager=mgr)
    if timed_out:
        return f"[Note: Page load timed out; showing partial DOM]\n{out}"
    return out


def click_impl(
    target: str,
    read_only: bool | None = None,
    manager: BrowserManager | None = None,
) -> str:
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to click web elements."
    mgr = manager or _MANAGER
    drv = mgr.get_driver(create=False)
    if drv is None:
        return "No active browser session. Call browser_navigate first."

    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        ElementNotInteractableException,
        StaleElementReferenceException,
        WebDriverException,
    )

    before_handles = list(drv.window_handles or [])
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            el = _find_element(drv, target, mgr=mgr)
            blocked = _check_element_target_ssrf(drv, el, mgr, is_submit=False)
            if blocked:
                return blocked
            try:
                drv.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", el)
            except Exception:
                pass
            try:
                el.click()
            except (ElementClickInterceptedException, ElementNotInteractableException):
                drv.execute_script("arguments[0].click();", el)
            last_exc = None
            break
        except StaleElementReferenceException as exc:
            last_exc = exc
            time.sleep(0.12)
        except (LookupError, ValueError) as exc:
            return f"ERROR: {exc}"
        except WebDriverException as exc:
            if attempt < 2 and "stale" in str(exc).lower():
                last_exc = exc
                time.sleep(0.12)
                continue
            return f"ERROR: Click failed for '{target}': {str(exc).splitlines()[0][:160]}"

    if last_exc is not None:
        return f"ERROR: Element '{target}' remained stale after retries. Run browser_snapshot again."

    _switch_to_new_or_live_tab(drv, before_handles)
    _wait_for_dom_settle(drv, timeout=2.0, settle_gap=0.14, min_wait=0.14, required_stable=2)
    ssrf_err = _enforce_current_url_ssrf(drv, mgr, before_handles=before_handles)
    if ssrf_err:
        return ssrf_err
    return snapshot_impl(manager=mgr)


def type_impl(
    target: str,
    text: str,
    submit: bool = False,
    read_only: bool | None = None,
    manager: BrowserManager | None = None,
) -> str:
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to type into web fields."
    mgr = manager or _MANAGER
    drv = mgr.get_driver(create=False)
    if drv is None:
        return "No active browser session. Call browser_navigate first."

    from selenium.common.exceptions import (
        ElementNotInteractableException,
        InvalidElementStateException,
        StaleElementReferenceException,
        WebDriverException,
    )
    from selenium.webdriver.common.keys import Keys

    before_handles = list(drv.window_handles or [])
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            el = _find_element(drv, target, mgr=mgr)
            if submit:
                blocked = _check_element_target_ssrf(drv, el, mgr, is_submit=True)
                if blocked:
                    return blocked
            try:
                drv.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", el)
                drv.execute_script("arguments[0].focus();", el)
            except Exception:
                pass
            try:
                el.clear()
            except (InvalidElementStateException, ElementNotInteractableException):
                pass
            except Exception:
                pass
            try:
                rem = el.get_attribute("value") or ""
                if rem:
                    el.send_keys(Keys.CONTROL, "a")
                    el.send_keys(Keys.BACKSPACE)
            except Exception:
                pass
            el.send_keys(text)
            if submit:
                el.send_keys(Keys.RETURN)
            last_exc = None
            break
        except StaleElementReferenceException as exc:
            last_exc = exc
            time.sleep(0.12)
        except (LookupError, ValueError) as exc:
            return f"ERROR: {exc}"
        except WebDriverException as exc:
            if attempt < 2 and "stale" in str(exc).lower():
                last_exc = exc
                time.sleep(0.12)
                continue
            return f"ERROR: Typing into '{target}' failed: {str(exc).splitlines()[0][:160]}"

    if last_exc is not None:
        return f"ERROR: Input '{target}' remained stale after retries. Run browser_snapshot again."

    if submit:
        _switch_to_new_or_live_tab(drv, before_handles)
        _wait_for_dom_settle(drv, timeout=2.0, settle_gap=0.14, min_wait=0.14, required_stable=2)
        ssrf_err = _enforce_current_url_ssrf(drv, mgr, before_handles=before_handles)
        if ssrf_err:
            return ssrf_err
    else:
        _wait_for_dom_settle(drv, timeout=0.35, settle_gap=0.08)
    return snapshot_impl(manager=mgr)


def scroll_impl(
    direction: str = "down",
    amount: int = 600,
    read_only: bool | None = None,
    manager: BrowserManager | None = None,
) -> str:
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to scroll pages."
    mgr = manager or _MANAGER
    drv = mgr.get_driver(create=False)
    if drv is None:
        return "No active browser session. Call browser_navigate first."
    d = (direction or "down").strip().lower()
    px = max(100, min(int(amount), 3000))
    delta = -px if d == "up" else px
    drv.execute_script(
        "const mode = arguments[0], delta = arguments[1];"
        "const doc = document.documentElement || document.body;"
        "const canWin = doc && (doc.scrollHeight > window.innerHeight + 2);"
        "if (mode === 'top') { window.scrollTo(0, 0); return; }"
        "if (mode === 'bottom') { window.scrollTo(0, Math.max(doc ? doc.scrollHeight : 0, document.body ? document.body.scrollHeight : 0)); return; }"
        "if (canWin) { window.scrollBy(0, delta); return; }"
        "for (const el of document.querySelectorAll('main, [role=\"main\"], div, section')) {"
        "  const st = window.getComputedStyle(el);"
        "  if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {"
        "    el.scrollTop += delta; return;"
        "  }"
        "}"
        "window.scrollBy(0, delta);",
        d,
        delta,
    )
    _wait_for_dom_settle(drv, timeout=0.35, settle_gap=0.08)
    return snapshot_impl(manager=mgr)


def back_impl(
    read_only: bool | None = None,
    manager: BrowserManager | None = None,
) -> str:
    if _is_read_only(read_only):
        return "READ-ONLY BLOCKED: Switch to RW mode to navigate back."
    mgr = manager or _MANAGER
    drv = mgr.get_driver(create=False)
    if drv is None:
        return "No active browser session. Call browser_navigate first."
    drv.back()
    _wait_for_dom_settle(drv, timeout=2.0, settle_gap=0.14, min_wait=0.14, required_stable=2)
    ssrf_err = _enforce_current_url_ssrf(drv, mgr)
    if ssrf_err:
        return ssrf_err
    return snapshot_impl(manager=mgr)


def close_impl(manager: BrowserManager | None = None) -> str:
    mgr = manager or _MANAGER
    closed = mgr.close()
    return "Browser session closed." if closed else "No active browser session was running."


# ============================================================
# MCP TOOL EXPORTS
# ============================================================

@mcp.tool()
def browser_navigate(url: str, headless: bool = False) -> str:
    """Open a site alias (gmail, youtube, google), search shortcut ('google search <X>'), or HTTPS URL in automated Chromium and return DOM snapshot (RW mode)."""
    return navigate_impl(url, headless=headless)


@mcp.tool()
def browser_snapshot(max_chars: int = 3000) -> str:
    """Inspect current page title, URL, numbered interactive refs `[e1]`, `[e2]`, roles/values, and clean visible text."""
    return snapshot_impl(max_chars=max_chars)


@mcp.tool()
def browser_click(target: str) -> str:
    """Click an interactive element by ref (`[e1]`, `e1`, `1`), visible label, or CSS selector (RW mode)."""
    return click_impl(target)


@mcp.tool()
def browser_type(target: str, text: str, submit: bool = False) -> str:
    """Type into an input/search field by ref (`[e1]`, `e1`, `1`), placeholder/label, or CSS selector; optionally press Enter (RW mode)."""
    return type_impl(target, text=text, submit=submit)


@mcp.tool()
def browser_scroll(direction: str = "down", amount: int = 600) -> str:
    """Scroll active web page (`down`, `up`, `top`, `bottom`) and return updated DOM snapshot (RW mode)."""
    return scroll_impl(direction=direction, amount=amount)


@mcp.tool()
def browser_back() -> str:
    """Navigate back one page in the automated browser session (RW mode)."""
    return back_impl()


@mcp.tool()
def browser_close() -> str:
    """Close the automated browser session cleanly to free memory."""
    return close_impl()


if __name__ == "__main__":
    mcp.run()
