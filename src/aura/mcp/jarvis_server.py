"""
AURA Jarvis MCP Server (SSRF-Safe Web Research, Personal Memory, Free Voice).

Inspired by Stanford OpenJarvis:
- Fail-closed SSRF guard before every network fetch (blocks private CIDRs,
  cloud metadata, IPv4-mapped IPv6, decimal/hex disguised IPs, and DNS failures).
- Keyless DuckDuckGo web search & HTML-to-text reader over HTTPX.
- Persistent personal memory management (`personal_facts.jsonl` + `USER_PROFILE.md`).
- Free Jarvis & Megatron DSP speech tool.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_lib
from html.parser import HTMLParser
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
import webbrowser

_SRC_DIR = str(Path(__file__).resolve().parents[2])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from mcp.server import MCPServer

from aura.core.personal_memory import PersonalMemoryStore, TRUST_TRUSTED
from aura.core.voice import VoiceEngine
from aura.core.workspace_connector import briefing_impl, calendar_manage_impl, gmail_manage_impl


mcp = MCPServer("AURA Jarvis Server")

_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.google.com",
    "100.100.100.200",
})

_BLOCKED_CIDRS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def _is_read_only() -> bool:
    return os.environ.get("AURA_READ_ONLY", "") == "1"


def _storage_dir() -> Path:
    from aura.config.settings import get_default_storage_dir

    return get_default_storage_dir()


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    packed = addr.packed
    if packed[:12] == b"\x00" * 12 and addr not in (ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")):
        return ipaddress.IPv4Address(packed[12:])
    return None


def is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        emb = _embedded_ipv4(addr)
        if emb is not None:
            addr = emb
    return any(addr in net for net in _BLOCKED_CIDRS)


def check_ssrf(url: str) -> str | None:
    """Validate an external HTTP(S) URL against SSRF targets; return reason or None."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return f"Unsupported URL scheme '{parsed.scheme}'. Only http and https are allowed."
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return "URL must include a hostname."
    if host in _BLOCKED_HOSTS:
        return f"Blocked internal/metadata host: {host}"

    # 1. Explicit IP literal
    try:
        ip_lit = ipaddress.ip_address(host)
    except ValueError:
        ip_lit = None
    if ip_lit is not None:
        if is_private_ip(str(ip_lit)):
            return f"Blocked private/reserved IP: {host}"
        return None

    # 2. Disguised decimal/hex/octal IPv4 (e.g. 2130706433, 0x7f000001, 127.1)
    try:
        packed = socket.inet_aton(host)
        canon = str(ipaddress.IPv4Address(packed))
        if canon in _BLOCKED_HOSTS or is_private_ip(canon):
            return f"Blocked disguised private IP: {host} -> {canon}"
    except OSError:
        pass

    # 3. DNS resolution fail-closed check
    try:
        resolved = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f"Could not resolve host (fail-closed SSRF guard): {host}"
    for _fam, _st, _proto, _cn, sockaddr in resolved:
        ip = sockaddr[0]
        if ip in _BLOCKED_HOSTS or is_private_ip(ip):
            return f"Blocked host resolving to private IP: {host} -> {ip}"
    return None


_SKIP_CONTENT_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "iframe", "canvas"})
_BLOCK_BREAK_TAGS = frozenset({"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"})
_CHROME_TAGS = frozenset({"nav", "footer", "aside"})
_BINARY_CONTENT_PREFIXES = ("image/", "audio/", "video/")
_BINARY_CONTENT_TYPES = frozenset({
    "application/pdf",
    "application/octet-stream",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/x-gzip",
    "application/vnd.ms-excel",
})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._chrome_depth = 0
        self._main_depth = 0
        self.all_parts: list[str] = []
        self.non_chrome_parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        low = tag.lower()
        if low in _SKIP_CONTENT_TAGS:
            self._skip += 1
            return
        attr_map = dict(attrs or ())
        role = (attr_map.get("role") or "").strip().lower()
        if low in _CHROME_TAGS or role in {"navigation", "contentinfo", "banner"}:
            self._chrome_depth += 1
        if low in {"main", "article"} or role == "main":
            self._main_depth += 1

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in _SKIP_CONTENT_TAGS:
            if self._skip > 0:
                self._skip -= 1
            return
        if low in _CHROME_TAGS and self._chrome_depth > 0:
            self._chrome_depth -= 1
        if low in {"main", "article"} and self._main_depth > 0:
            self._main_depth -= 1
        if low in _BLOCK_BREAK_TAGS and self._skip == 0:
            self.all_parts.append("\n")
            if self._chrome_depth == 0:
                self.non_chrome_parts.append("\n")
            if self._main_depth > 0 and self._chrome_depth == 0:
                self.main_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip != 0:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        chunk = cleaned + " "
        self.all_parts.append(chunk)
        if self._chrome_depth == 0:
            self.non_chrome_parts.append(chunk)
        if self._main_depth > 0 and self._chrome_depth == 0:
            self.main_parts.append(chunk)

    @staticmethod
    def _normalize_parts(parts: list[str]) -> str:
        raw = "".join(parts)
        lines: list[str] = []
        prev = ""
        for raw_line in raw.splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if line == prev and len(line) < 120:
                continue
            lines.append(line)
            prev = line
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    def text(self) -> str:
        main_txt = self._normalize_parts(self.main_parts)
        if len(main_txt) >= 180:
            return main_txt
        clean_txt = self._normalize_parts(self.non_chrome_parts)
        if len(clean_txt) >= 100:
            return clean_txt
        return self._normalize_parts(self.all_parts)


def _safe_http_get(
    url: str,
    timeout: float = 12.0,
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    max_bytes: int = 524288,
) -> tuple[int, str, str]:
    import httpx

    cur = url.strip()
    req_method = method.upper()
    req_data = data
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AURA-Jarvis/4.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    t_cfg = httpx.Timeout(
        connect=min(4.0, timeout),
        read=timeout,
        write=min(4.0, timeout),
        pool=min(4.0, timeout),
    )

    for attempt in range(2):
        try:
            with httpx.Client(follow_redirects=False, timeout=t_cfg) as client:
                hop_url = cur
                hop_method = req_method
                hop_data = req_data
                for _ in range(6):
                    err = check_ssrf(hop_url)
                    if err:
                        raise ValueError(f"SSRF BLOCKED: {err}")
                    with client.stream(hop_method, hop_url, headers=hdrs, data=hop_data) as resp:
                        if resp.status_code in {301, 302, 303, 307, 308} and "location" in resp.headers:
                            hop_url = urljoin(hop_url, resp.headers["location"].strip())
                            if resp.status_code in {301, 302, 303}:
                                hop_method = "GET"
                                hop_data = None
                            continue
                        resp.raise_for_status()
                        ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                        if ctype and (
                            ctype.startswith(_BINARY_CONTENT_PREFIXES) or ctype in _BINARY_CONTENT_TYPES
                        ):
                            raise ValueError(f"Unsupported non-text content-type: {ctype}")
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in resp.iter_bytes(chunk_size=16384):
                            if not chunk:
                                continue
                            remaining = max_bytes - total
                            if remaining <= 0:
                                break
                            if len(chunk) > remaining:
                                chunks.append(chunk[:remaining])
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                        enc = resp.encoding or "utf-8"
                        text = b"".join(chunks).decode(enc, errors="replace")
                        return resp.status_code, str(resp.url), text
            raise ValueError("Too many HTTP redirects.")
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            if attempt == 0:
                time.sleep(0.2)
                continue
            raise
    raise ValueError("HTTP request failed.")


def _normalize_search_href(raw_href: str) -> str:
    href = html_lib.unescape((raw_href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        extracted = qs.get("uddg", [""])[0]
        if extracted:
            href = html_lib.unescape(extracted)
            if href.startswith("//"):
                href = "https:" + href
    parsed = urlparse(href)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or is_private_ip(host):
        return ""
    if "duckduckgo.com/y.js" in href:
        return ""
    return href


def _parse_ddg_html(html: str, max_results: int) -> list[dict[str, str]]:
    """Extract clean result entries from DuckDuckGo HTML or Lite response."""
    anchor_matches = list(
        re.finditer(
            r'<a\b([^>]*\bclass=["\'][^"\']*\bresult__a\b[^"\']*["\'][^>]*)>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
    )
    snippet_pat = r'<(?P<tag>a|div|span)\b[^>]*\bclass=["\'][^"\']*\bresult__snippet\b[^"\']*["\'][^>]*>(?P<body>.*?)</(?P=tag)>'
    if not anchor_matches:
        anchor_matches = list(
            re.finditer(
                r'<a\b([^>]*\bclass=["\'][^"\']*\bresult-link\b[^"\']*["\'][^>]*)>(.*?)</a>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
        )
        snippet_pat = r'<(?P<tag>td|div|span)\b[^>]*\bclass=["\'][^"\']*\bresult-snippet\b[^"\']*["\'][^>]*>(?P<body>.*?)</(?P=tag)>'

    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for i, m in enumerate(anchor_matches):
        attrs = m.group(1)
        m_href = re.search(r'\bhref=(["\'])(.*?)\1', attrs, re.DOTALL | re.IGNORECASE)
        if not m_href:
            continue
        href = _normalize_search_href(m_href.group(2))
        if not href or href in seen_urls:
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        title = " ".join(title.split())
        if not title:
            continue

        block_end = anchor_matches[i + 1].start() if (i + 1) < len(anchor_matches) else min(len(html), m.end() + 2500)
        window = html[m.end() : min(block_end, m.end() + 2500)]
        m_snip = re.search(snippet_pat, window, re.DOTALL | re.IGNORECASE)
        raw_snip = m_snip.group("body") if m_snip else ""
        snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_snip)).strip()
        seen_urls.add(href)
        items.append({"title": title, "url": href, "snippet": " ".join(snippet.split())[:350]})
        if len(items) >= max_results:
            break
    return items


def _search_ddg(clean_q: str, limit: int) -> tuple[list[dict[str, str]], str | None]:
    """Query DuckDuckGo HTML with Lite fallback over SSRF-checked HTTP."""
    endpoints = (
        ("https://html.duckduckgo.com/html/", 7.0),
        ("https://lite.duckduckgo.com/lite/", 6.0),
    )
    last_err: str | None = None
    for endpoint, t_sec in endpoints:
        try:
            status, _final_url, body = _safe_http_get(
                endpoint,
                timeout=t_sec,
                method="POST",
                data={"q": clean_q},
            )
            if status == 202:
                last_err = "Search endpoint returned 202 challenge."
                continue
            results = _parse_ddg_html(body, limit)
            if results:
                return results, None
        except Exception as exc:
            last_err = str(exc)
    return [], last_err


def web_search_impl(query: str, max_results: int = 5) -> str:
    clean_q = query.strip()
    if len(clean_q) < 2:
        return "ERROR: Search query must be at least 2 characters."
    limit = max(1, min(int(max_results), 8))
    results, err = _search_ddg(clean_q, limit)
    if not results:
        if err:
            return f"WEB SEARCH ERROR: {err}"
        return f"No web results found for '{clean_q}'."
    chunks = [
        f"### {r['title']}\nSource: {r['url']}\nSummary: {r['snippet'] or '(no snippet)'}"
        for r in results
    ]
    return "\n\n---\n\n".join(chunks)


def fetch_url_impl(url: str, max_chars: int = 3500, timeout: float = 10.0) -> str:
    limit = max(300, min(int(max_chars), 8000))
    try:
        _, final_url, body = _safe_http_get(url, timeout=timeout)
    except Exception as exc:
        return f"FETCH ERROR: {exc}"

    parser = _TextExtractor()
    parser.feed(body)
    text = parser.text()
    if len(text) > limit:
        text = text[:limit] + "\n... [truncated]"
    return f"[URL: {final_url}]\n{text or '(empty page text)'}"


_SITE_ALIASES: dict[str, str] = {
    "gmail": "https://mail.google.com",
    "google mail": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "email": "https://mail.google.com",
    "inbox": "https://mail.google.com",
    "calendar": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
    "gcal": "https://calendar.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "youtube music": "https://music.youtube.com",
    "github": "https://github.com",
    "gitlab": "https://gitlab.com",
    "google": "https://www.google.com",
    "browser": "https://www.google.com",
    "web browser": "https://www.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "docs": "https://docs.google.com",
    "google docs": "https://docs.google.com",
    "sheets": "https://sheets.google.com",
    "google sheets": "https://sheets.google.com",
    "meet": "https://meet.google.com",
    "google meet": "https://meet.google.com",
    "notion": "https://www.notion.so",
    "chatgpt": "https://chatgpt.com",
    "openai": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "perplexity": "https://www.perplexity.ai",
    "gemini": "https://gemini.google.com",
    "huggingface": "https://huggingface.co",
    "hf": "https://huggingface.co",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "leetcode": "https://leetcode.com",
    "kaggle": "https://www.kaggle.com",
    "linkedin": "https://www.linkedin.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "whatsapp": "https://web.whatsapp.com",
    "telegram": "https://web.telegram.org",
    "discord": "https://discord.com/app",
    "spotify": "https://open.spotify.com",
    "netflix": "https://www.netflix.com",
    "figma": "https://www.figma.com",
    "canva": "https://www.canva.com",
    "slack": "https://app.slack.com",
    "vercel": "https://vercel.com",
    "render": "https://dashboard.render.com",
    "supabase": "https://supabase.com/dashboard",
    "aws": "https://console.aws.amazon.com",
    "azure": "https://portal.azure.com",
    "cloudflare": "https://dash.cloudflare.com",
    "npm": "https://www.npmjs.com",
    "pypi": "https://pypi.org",
    "localhost": "http://127.0.0.1",
}


def resolve_site_target(target: str) -> str | None:
    """Resolve a spoken site alias, search shortcut, or raw URL/domain into a valid HTTPS URL."""
    clean = re.sub(r"^(?:my|the)\s+", "", (target or "").strip(), flags=re.IGNORECASE)
    clean = re.sub(
        r"\s+(?:(?:in|on|using|with)\s+(?:my\s+)?(?:google\s+)?(?:chrome|edge|msedge|firefox|browser)|website|site)$",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip(" .!?\"'")
    if not clean:
        return None
    low = clean.lower()
    if low in _SITE_ALIASES:
        return _SITE_ALIASES[low]

    m_yt = re.match(
        r"^(?:youtube\s+(?:search|play|for)|search\s+youtube\s+for|play\s+on\s+youtube)\s+(.+)$",
        low,
    )
    if m_yt:
        q = clean[m_yt.start(1) : m_yt.end(1)].strip()
        if q:
            return f"https://www.youtube.com/results?search_query={quote(q)}"

    m_site_on = re.match(
        r"^search\s+(?:for\s+)?(.+?)\s+(?:on|in)\s+(google|duckduckgo|ddg|bing|youtube|github|wikipedia)$",
        low,
    )
    if m_site_on:
        q = clean[m_site_on.start(1) : m_site_on.end(1)].strip()
        engine = m_site_on.group(2)
        if q:
            enc = quote(q)
            if engine == "youtube":
                return f"https://www.youtube.com/results?search_query={enc}"
            if engine in {"duckduckgo", "ddg"}:
                return f"https://duckduckgo.com/?q={enc}"
            if engine == "bing":
                return f"https://www.bing.com/search?q={enc}"
            if engine == "github":
                return f"https://github.com/search?q={enc}"
            if engine == "wikipedia":
                return f"https://en.wikipedia.org/w/index.php?search={enc}"
            return f"https://www.google.com/search?q={enc}"

    search_patterns = (
        (r"^(?:google\s+search(?:\s+for)?|search\s+google\s+for)\s+(.+)$", "https://www.google.com/search?q={}"),
        (r"^(?:(?:duckduckgo|ddg)\s+search(?:\s+for)?|search\s+(?:duckduckgo|ddg)\s+for)\s+(.+)$", "https://duckduckgo.com/?q={}"),
        (r"^(?:bing\s+search(?:\s+for)?|search\s+bing\s+for)\s+(.+)$", "https://www.bing.com/search?q={}"),
        (r"^(?:github\s+search(?:\s+for)?|search\s+github\s+for)\s+(.+)$", "https://github.com/search?q={}"),
        (r"^(?:(?:wikipedia|wiki)(?:\s+search)?(?:\s+for)?|search\s+wikipedia\s+for)\s+(.+)$", "https://en.wikipedia.org/w/index.php?search={}"),
        (r"^(?:web\s+search(?:\s+for)?|search\s+(?:the\s+)?(?:web\s+)?for)\s+(.+)$", "https://www.google.com/search?q={}"),
    )
    for pat, tmpl in search_patterns:
        m_s = re.match(pat, low)
        if m_s:
            q = clean[m_s.start(1) : m_s.end(1)].strip()
            if q:
                return tmpl.format(quote(q))

    m_google = re.match(r"^google\s+(.+)$", low)
    if m_google:
        q = clean[m_google.start(1) : m_google.end(1)].strip()
        if q and q.lower() not in {"chrome", "edge", "msedge", "firefox", "browser"}:
            return f"https://www.google.com/search?q={quote(q)}"

    if low.startswith(("http://", "https://")):
        return clean
    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(?:/.*)?$", low):
        return f"https://{clean}"
    return None


def resolve_multiple_site_targets(target: str) -> list[str]:
    """Resolve one or multiple comma/'and'-separated site aliases or URLs into valid URLs."""
    raw = (target or "").strip()
    if not raw:
        return []
    single = resolve_site_target(raw)
    has_sep = ("," in raw) or bool(re.search(r"\s+(?:and|&)\s+", raw, re.IGNORECASE))
    if single and not has_sep:
        return [single]
    if has_sep:
        parts = [p.strip() for p in re.split(r"\s*(?:,|\band\b|&)\s*", raw, flags=re.IGNORECASE) if p.strip()]
        if len(parts) > 1:
            resolved: list[str] = []
            for part in parts:
                u = resolve_site_target(part)
                if not u:
                    resolved = []
                    break
                if u not in resolved:
                    resolved.append(u)
            if resolved:
                return resolved
    return [single] if single else []


def _lookup_browser_exe(browser: str) -> tuple[str | None, str]:
    b_low = (browser or "").strip().lower()
    if not b_low:
        return None, ""
    if "chrome" in b_low:
        exe_name, label = "chrome.exe", "Google Chrome"
    elif "edge" in b_low or "msedge" in b_low:
        exe_name, label = "msedge.exe", "Microsoft Edge"
    elif "firefox" in b_low:
        exe_name, label = "firefox.exe", "Firefox"
    else:
        return None, browser.strip()
    try:
        from .desktop_server import _lookup_app_path
    except ImportError:
        from aura.mcp.desktop_server import _lookup_app_path
    try:
        return _lookup_app_path(exe_name), label
    except Exception:
        return None, label


def _launch_browser_urls(urls: list[str], browser: str = "") -> str:
    """Open already-validated *urls* in the requested or default browser."""
    url_list = ", ".join(urls)
    browser_exe, browser_label = _lookup_browser_exe(browser)
    headless = os.environ.get("AURA_HEADLESS_TEST") == "1"
    if browser.strip():
        if headless:
            return f"Opened {url_list} in {browser_label or browser} (headless test)."
        if browser_exe:
            subprocess.Popen([browser_exe, *urls], shell=False)
            return f"Opened {url_list} in {browser_label}."

    if headless:
        return f"Opened {url_list} in your default browser (headless test)."

    opened_any = False
    for url in urls:
        if webbrowser.open(url):
            opened_any = True
    if opened_any:
        return f"Opened {url_list} in your default browser."
    return f"Triggered browser launch for {url_list}."


def open_website_impl(target: str, read_only: bool | None = None, browser: str = "") -> str:
    """Open one or more known web apps (Gmail, Calendar, YouTube, GitHub, etc.), search URLs, or public URLs safely."""
    ro = _is_read_only() if read_only is None else read_only
    urls = resolve_multiple_site_targets(target)
    if not urls:
        raw = (target or "").strip()
        if "://" in raw:
            err = check_ssrf(raw)
            if err:
                return f"SSRF BLOCKED: {err}"
        return f"ERROR: Unknown website or invalid URL '{target}'."
    for url in urls:
        err = check_ssrf(url)
        if err:
            return f"SSRF BLOCKED: {err}"
    url_list = ", ".join(urls)
    if ro:
        return f"READ-ONLY BLOCKED: Switch to RW mode to open {url_list} in browser."
    return _launch_browser_urls(urls, browser=browser)


def play_youtube_impl(query: str, browser: str = "chrome", read_only: bool | None = None) -> str:
    """Resolve the top playable YouTube video for *query* and open it directly to play."""
    ro = _is_read_only() if read_only is None else read_only
    clean_q = re.sub(
        r"^(?:the\s+video\s+(?:of|for)\s+|a\s+video\s+(?:of|for)\s+|any\s+video\s+(?:of|for)\s+)",
        "",
        (query or "").strip(),
        flags=re.IGNORECASE,
    )
    clean_q = re.sub(
        r"\s+(?:(?:on|in|from)\s+(?:the\s+)?youtube(?:\s+page)?|(?:in|on)\s+(?:google\s+)?(?:chrome|edge|msedge)|video)$",
        "",
        clean_q,
        flags=re.IGNORECASE,
    ).strip(" .!?\"'")
    if not clean_q:
        return "ERROR: YouTube search query cannot be empty."
    if ro:
        return f"READ-ONLY BLOCKED: Switch to RW mode to play '{clean_q}' on YouTube."

    search_url = f"https://www.youtube.com/results?search_query={quote(clean_q)}"
    target_url = search_url
    if os.environ.get("AURA_HEADLESS_TEST") != "1":
        try:
            _, _, html = _safe_http_get(search_url, timeout=5.0)
            m_vid = re.search(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', html) or re.search(
                r'/watch\?v=([A-Za-z0-9_-]{11})', html
            )
            if m_vid:
                target_url = f"https://www.youtube.com/watch?v={m_vid.group(1)}"
        except Exception:
            target_url = search_url

    launch_note = _launch_browser_urls([target_url], browser=browser)
    return f"Playing '{clean_q}' on YouTube ({target_url}). {launch_note}"


def research_topic_impl(query: str, max_sources: int = 3) -> str:
    """Search DuckDuckGo and concurrently SSRF-safe fetch excerpts from top result URLs."""
    clean_q = query.strip()
    if len(clean_q) < 2:
        return "ERROR: Research topic must be at least 2 characters."

    limit = max(1, min(int(max_sources), 4))
    results, search_err = _search_ddg(clean_q, limit + 3)
    if not results:
        if search_err:
            return f"WEB SEARCH ERROR: {search_err}"
        return f"No research results found for '{clean_q}'."

    candidates = results[: min(len(results), limit + 2)]
    per_source_chars = max(450, min(950, 2250 // limit))
    fetched_map: dict[int, str] = {}

    pool = ThreadPoolExecutor(max_workers=min(4, len(candidates)))
    futures = {
        pool.submit(fetch_url_impl, item["url"], per_source_chars, 6.0): idx
        for idx, item in enumerate(candidates)
    }
    try:
        for fut in as_completed(futures, timeout=11.0):
            idx = futures[fut]
            try:
                fetched_map[idx] = fut.result()
            except Exception as exc:
                fetched_map[idx] = f"FETCH ERROR: {exc}"
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    succeeded: list[tuple[int, dict[str, str], str]] = []
    fallbacks: list[tuple[int, dict[str, str], str]] = []
    for idx, item in enumerate(candidates):
        page_txt = fetched_map.get(idx, "")
        if page_txt and not page_txt.startswith(("FETCH ERROR:", "SSRF BLOCKED:")):
            succeeded.append((idx, item, page_txt))
        else:
            fallbacks.append((idx, item, page_txt))

    chosen = (succeeded + fallbacks)[:limit]
    sections: list[str] = [
        f"Research Summary for '{clean_q}' ({len(succeeded[:limit])}/{len(chosen)} sources fetched):"
    ]
    for rank, (_idx, item, page_txt) in enumerate(chosen, start=1):
        lines = [
            f"### [{rank}] {item['title']}",
            f"Source: {item['url']}",
        ]
        if page_txt and not page_txt.startswith(("FETCH ERROR:", "SSRF BLOCKED:")):
            excerpt = page_txt
            if excerpt.startswith("[URL: "):
                first_nl = excerpt.find("\n")
                if first_nl != -1:
                    url_hdr = excerpt[6:first_nl].rstrip("]")
                    body_txt = excerpt[first_nl + 1 :].strip()
                    if url_hdr != item["url"]:
                        lines.append(f"Final URL: {url_hdr}")
                    excerpt = body_txt
            if len(excerpt) < 120 and item.get("snippet"):
                lines.append(f"Snippet: {item['snippet']}")
            lines.append(f"Excerpt:\n{excerpt}")
        elif item.get("snippet"):
            lines.append(f"Snippet: {item['snippet']}")
        sections.append("\n".join(lines))

    joined = "\n\n---\n\n".join(sections)
    if len(joined) > 2850:
        joined = joined[:2830].rstrip() + "\n... [truncated]"
    return joined


def memory_manage_impl(action: str = "read", entry: str = "", store: PersonalMemoryStore | None = None) -> str:
    mem = store or PersonalMemoryStore()
    act = action.strip().lower()
    if act == "read":
        facts = mem.list_facts()
        if not facts:
            return "(personal memory is empty)"
        return "\n".join(f"- [{f.category}] {f.text}" for f in facts)
    if act == "search":
        hits = mem.search_facts(entry)
        if not hits:
            return f"No personal memory facts matching '{entry}'."
        return "\n".join(f"- [{f.category}] {f.text}" for f in hits)
    if act in {"remember", "add"}:
        if _is_read_only():
            return "READ-ONLY BLOCKED: Switch to RW mode for assistant memory writes."
        saved = mem.add_fact(entry, category="note", trust=TRUST_TRUSTED)
        return f"Remembered: {saved.text}" if saved else "ERROR: Fact could not be stored (empty or unsafe content)."
    if act in {"forget", "remove"}:
        if _is_read_only():
            return "READ-ONLY BLOCKED: Switch to RW mode for assistant memory deletion."
        removed = mem.remove_fact(entry)
        return f"Forgot {removed} matching fact(s)."
    return f"ERROR: Unknown action '{action}'. Use read, search, remember, or forget."


# ============================================================
# MCP TOOL EXPORTS
# ============================================================

@mcp.tool()
def jarvis_web_search(query: str, max_results: int = 5) -> str:
    """Search the public web for up-to-date answers and sources (no API key required)."""
    return web_search_impl(query, max_results=max_results)


@mcp.tool()
def jarvis_research(topic: str, max_sources: int = 3) -> str:
    """Research a topic by searching the web and reading top external source pages (SSRF-safe)."""
    return research_topic_impl(topic, max_sources=max_sources)


@mcp.tool()
def jarvis_open_website(target: str, browser: str = "") -> str:
    """Open site alias (gmail, youtube, google, github), search shortcut ('google search <X>'), or public URL(s) in browser (browser='chrome'|'edge'; RW mode)."""
    return open_website_impl(target, browser=browser)


@mcp.tool()
def jarvis_play_youtube(query: str, browser: str = "chrome") -> str:
    """Find and play a specific YouTube video, trailer, or song directly in Chrome/Edge (RW mode)."""
    return play_youtube_impl(query, browser=browser)


@mcp.tool()
def jarvis_fetch_url(url: str, max_chars: int = 3500) -> str:
    """Fetch and read text from an external public HTTP/HTTPS URL (SSRF-protected)."""
    return fetch_url_impl(url, max_chars=max_chars)


@mcp.tool()
def jarvis_memory(action: str = "read", entry: str = "") -> str:
    """Read, search, remember, or forget durable personal facts across all sessions."""
    return memory_manage_impl(action=action, entry=entry)


@mcp.tool()
def jarvis_speak(text: str, persona: str = "jarvis") -> str:
    """Speak a concise response aloud via free Jarvis, Ultron DSP, or Megatron DSP voice."""
    target = persona.strip().lower()
    if target not in ("jarvis", "ultron", "megatron"):
        target = "jarvis"
    VoiceEngine().speak_async(text[:400], persona=target)  # type: ignore[arg-type]
    return f"Speaking ({target}): {text[:80]}"


@mcp.tool()
def jarvis_calendar(
    action: str = "today",
    title: str = "",
    start_time: str = "",
    notes: str = "",
    event_id: str = "",
) -> str:
    """Check today's/upcoming agenda or add/delete calendar events (supports local store, iCal feeds, and Google Calendar)."""
    return calendar_manage_impl(
        action=action,
        title=title,
        start_time=start_time,
        notes=notes,
        event_id=event_id,
        read_only=_is_read_only(),
        storage_dir=_storage_dir(),
    )


@mcp.tool()
def jarvis_gmail(
    action: str = "unread",
    query: str = "",
    to: str = "",
    subject: str = "",
    body: str = "",
    max_results: int = 5,
) -> str:
    """Read/search Gmail inbox directly in the background or send email (sending requires RW mode)."""
    return gmail_manage_impl(
        action=action,
        query=query,
        to=to,
        subject=subject,
        body=body,
        max_results=max_results,
        read_only=_is_read_only(),
        storage_dir=_storage_dir(),
    )


@mcp.tool()
def jarvis_briefing() -> str:
    """Generate a proactive daily briefing combining local time, today's calendar agenda, unread Gmail status, and personal memory notes."""
    return briefing_impl(read_only=_is_read_only(), storage_dir=_storage_dir())


if __name__ == "__main__":
    mcp.run()
