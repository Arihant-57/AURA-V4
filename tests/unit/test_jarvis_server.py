"""Unit tests for jarvis_server SSRF guard, web parsing, memory tool, and scope gating."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.aura.core.personal_memory import PersonalMemoryStore
from src.aura.core.scope import TaskScope, select_active_tools
from src.aura.mcp import jarvis_server as js


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///C:/Windows/win.ini",
        "http://127.0.0.1:8000/api/status",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.50/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://nonexistent.invalid.aura.local/",
    ],
)
def test_check_ssrf_blocks_private_disguised_and_unresolvable(bad_url: str):
    reason = js.check_ssrf(bad_url)
    assert reason is not None


def test_check_ssrf_allows_public_ip():
    assert js.check_ssrf("https://1.1.1.1/") is None


def test_parse_ddg_html_extracts_results():
    sample = '''
    <div class="result">
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide">AURA Guide</a>
      <a class="result__snippet">Complete guide to AURA V4.</a>
    </div>
    '''
    items = js._parse_ddg_html(sample, max_results=5)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/guide"
    assert "AURA Guide" in items[0]["title"]


def test_memory_manage_tool_respects_ro_mode(tmp_path: Path, monkeypatch):
    store = PersonalMemoryStore(storage_dir=tmp_path)
    monkeypatch.setenv("AURA_READ_ONLY", "1")
    assert "READ-ONLY BLOCKED" in js.memory_manage_impl("remember", "likes dark UI", store=store)
    assert "READ-ONLY BLOCKED" in js.memory_manage_impl("forget", "dark", store=store)

    monkeypatch.setenv("AURA_READ_ONLY", "0")
    assert "Remembered:" in js.memory_manage_impl("remember", "likes dark UI", store=store)
    assert "likes dark UI" in js.memory_manage_impl("read", store=store)


def test_jarvis_tools_scope_gated():
    fake_tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="desktop_read_screen_ocr"),
        SimpleNamespace(name="jarvis_web_search"),
        SimpleNamespace(name="jarvis_memory"),
    ]
    code_names = {t.name for t in select_active_tools(fake_tools, TaskScope("refactor providers.py"))}
    assert code_names == {"read_file"}

    web_names = {t.name for t in select_active_tools(fake_tools, TaskScope("search online for Python 3.13 news"))}
    assert "jarvis_web_search" in web_names


def test_open_website_and_natural_scope():
    from unittest.mock import patch
    from src.aura.core.scope import TaskScope
    from src.aura.mcp.jarvis_server import open_website_impl, resolve_site_target

    assert resolve_site_target("my gmail") == "https://mail.google.com"
    assert resolve_site_target("google calendar") == "https://calendar.google.com"
    assert "SSRF BLOCKED" in open_website_impl("http://127.0.0.1:8000", read_only=False)
    assert "READ-ONLY BLOCKED" in open_website_impl("gmail", read_only=True)

    with patch("webbrowser.open", return_value=True) as mock_wb:
        out = open_website_impl("gmail", read_only=False)
        assert "https://mail.google.com" in out
        mock_wb.assert_called_once_with("https://mail.google.com")

    greet = TaskScope("Hey Jarvis, what are you doing?")
    assert greet.conversational is True

    model_q = TaskScope("which midel uh are")
    assert model_q.conversational is True

    gmail_task = TaskScope("open my gmail and summarize inbox")
    assert gmail_task.jarvis is True
    assert gmail_task.desktop is True


def test_calendar_manage_crud_ro_and_ics(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch
    from src.aura.core.workspace_connector import calendar_manage_impl

    assert "READ-ONLY BLOCKED" in calendar_manage_impl(
        "add", title="Standup", start_time="2026-11-14 09:00", read_only=True, storage_dir=tmp_path
    )
    assert "No scheduled events found" in calendar_manage_impl("today", read_only=True, storage_dir=tmp_path)

    added = calendar_manage_impl(
        "add", title="Design Review", notes="Bring slides", read_only=False, storage_dir=tmp_path
    )
    assert "Calendar event scheduled:" in added
    ev_id = added.split("[", 1)[1].split("]", 1)[0]

    today_out = calendar_manage_impl("today", read_only=True, storage_dir=tmp_path)
    assert "Design Review" in today_out
    assert "Bring slides" in today_out

    assert "READ-ONLY BLOCKED" in calendar_manage_impl("delete", event_id=ev_id, read_only=True, storage_dir=tmp_path)
    assert "Deleted 1" in calendar_manage_impl("delete", event_id=ev_id, read_only=False, storage_dir=tmp_path)

    # SSRF guard blocks localhost iCal URL
    monkeypatch.setenv("GCAL_ICAL_URL", "http://127.0.0.1:9999/private.ics")
    ssrf_out = calendar_manage_impl("list", read_only=True, storage_dir=tmp_path)
    assert "SSRF BLOCKED" in ssrf_out

    # Valid external iCal feed parses VEVENT summary and timestamp
    ics_payload = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-9001\r\n"
        "DTSTART:20261114T153000Z\r\n"
        "SUMMARY:AURA Architecture Sync\r\n"
        "DESCRIPTION:Discuss Phase 2 & 3\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    monkeypatch.setenv("GCAL_ICAL_URL", "https://calendar.google.com/calendar/ical/demo/basic.ics")
    fake_resp = SimpleNamespace(status_code=200, headers={}, text=ics_payload, raise_for_status=lambda: None)
    with patch("src.aura.core.workspace_connector._ssrf_check_url", return_value=None), patch(
        "httpx.get", return_value=fake_resp
    ):
        listed = calendar_manage_impl("list", read_only=True, storage_dir=tmp_path)
        assert "AURA Architecture Sync" in listed
        assert "2026-11-14 15:30" in listed


def test_gmail_manage_ro_guard_fallback_and_mocked_imap_smtp(tmp_path: Path, monkeypatch):
    from email.message import EmailMessage
    from unittest.mock import MagicMock, patch
    from src.aura.core.workspace_connector import gmail_manage_impl

    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN", raising=False)

    assert "READ-ONLY BLOCKED" in gmail_manage_impl(
        "send", to="boss@example.com", subject="Status", body="Done", read_only=True, storage_dir=tmp_path
    )
    assert "No Gmail credentials configured yet" in gmail_manage_impl("unread", read_only=True, storage_dir=tmp_path)

    monkeypatch.setenv("GMAIL_ADDRESS", "jarvis@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret-app-pw")

    sample_email = EmailMessage()
    sample_email["From"] = "Pepper Potts <pepper@stark.com>"
    sample_email["Subject"] = "Quarterly Demo"
    sample_email["Date"] = "Sat, 14 Nov 2026 09:00:00 +0000"
    sample_email.set_content("All systems green for the briefing.")

    mock_imap = MagicMock()
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822)", sample_email.as_bytes())])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        inbox_out = gmail_manage_impl("unread", read_only=True, storage_dir=tmp_path)
        assert "Pepper Potts" in inbox_out
        assert "Quarterly Demo" in inbox_out
        assert "All systems green" in inbox_out
        mock_imap.login.assert_called_once_with("jarvis@example.com", "secret-app-pw")
        mock_imap.logout.assert_called_once()

    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
        sent_out = gmail_manage_impl(
            "send", to="pepper@stark.com", subject="Re: Quarterly Demo", body="On it.", read_only=False, storage_dir=tmp_path
        )
        assert "Email sent to pepper@stark.com" in sent_out
        mock_smtp.login.assert_called_once_with("jarvis@example.com", "secret-app-pw")
        mock_smtp.send_message.assert_called_once()

    # CRLF header injection is rejected before SMTP connect
    assert "cannot contain newline" in gmail_manage_impl(
        "send", to="a@b.com\r\nBcc: evil@x.com", subject="Hi", read_only=False, storage_dir=tmp_path
    )


def test_proactive_briefing_combines_agenda_memory_and_inbox(tmp_path: Path, monkeypatch):
    from src.aura.core.workspace_connector import briefing_impl, calendar_manage_impl

    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN", raising=False)

    calendar_manage_impl("add", title="Evening Workout", read_only=False, storage_dir=tmp_path)
    PersonalMemoryStore(storage_dir=tmp_path).add_fact("prefers concise morning briefings", category="preference")

    brief = briefing_impl(read_only=True, storage_dir=tmp_path)
    assert "## Jarvis Daily Briefing" in brief
    assert "Evening Workout" in brief
    assert "prefers concise morning briefings" in brief
    assert "Direct Gmail credentials not configured" in brief
    assert TaskScope("give me the daily briefing").jarvis is True


def test_open_website_aliases_multi_site_and_chrome_launch(monkeypatch):
    from unittest.mock import patch
    from src.aura.mcp.jarvis_server import (
        open_website_impl,
        resolve_multiple_site_targets,
        resolve_site_target,
    )

    monkeypatch.delenv("AURA_HEADLESS_TEST", raising=False)
    assert resolve_site_target("chatgpt") == "https://chatgpt.com"
    assert resolve_site_target("claude") == "https://claude.ai"
    assert resolve_site_target("stackoverflow") == "https://stackoverflow.com"
    assert resolve_site_target("whatsapp in chrome") == "https://web.whatsapp.com"
    assert resolve_multiple_site_targets("youtube and github") == [
        "https://www.youtube.com",
        "https://github.com",
    ]

    with patch("src.aura.mcp.desktop_server._lookup_app_path", return_value="C:/Chrome/chrome.exe"), patch(
        "src.aura.mcp.jarvis_server.subprocess.Popen"
    ) as mock_popen:
        out = open_website_impl("youtube and github", read_only=False, browser="chrome")
        assert "Google Chrome" in out
        assert "https://www.youtube.com" in out
        assert "https://github.com" in out
        mock_popen.assert_called_once_with(
            ["C:/Chrome/chrome.exe", "https://www.youtube.com", "https://github.com"],
            shell=False,
        )


def test_play_youtube_impl_and_site_search_alias(monkeypatch):
    from unittest.mock import patch
    from src.aura.mcp.jarvis_server import play_youtube_impl, resolve_site_target

    assert resolve_site_target("youtube search Avengers Endgame") == (
        "https://www.youtube.com/results?search_query=Avengers%20Endgame"
    )
    assert "READ-ONLY BLOCKED" in play_youtube_impl("Avengers Endgame", read_only=True)

    monkeypatch.setenv("AURA_HEADLESS_TEST", "1")
    out_headless = play_youtube_impl("Avengers Endgame", browser="chrome", read_only=False)
    assert "Playing 'Avengers Endgame' on YouTube" in out_headless
    assert "https://www.youtube.com/results?search_query=Avengers%20Endgame" in out_headless

    monkeypatch.delenv("AURA_HEADLESS_TEST", raising=False)
    fake_html = '<script>var ytInitialData = {"videoId":"TcMBFSGVi1c"};</script>'
    with patch(
        "src.aura.mcp.jarvis_server._safe_http_get",
        return_value=(200, "https://www.youtube.com/results", fake_html),
    ), patch("src.aura.mcp.desktop_server._lookup_app_path", return_value="C:/Chrome/chrome.exe"), patch(
        "src.aura.mcp.jarvis_server.subprocess.Popen"
    ) as mock_popen:
        out_live = play_youtube_impl("Avengers Endgame trailer", browser="chrome", read_only=False)
        assert "https://www.youtube.com/watch?v=TcMBFSGVi1c" in out_live
        mock_popen.assert_called_once_with(
            ["C:/Chrome/chrome.exe", "https://www.youtube.com/watch?v=TcMBFSGVi1c"],
            shell=False,
        )


def test_safe_http_get_blocks_ssrf_redirects_and_binary_content(monkeypatch):
    import httpx
    from unittest.mock import patch

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://93.184.216.34/start":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/internal"})
        if str(request.url) == "https://93.184.216.34/file.pdf":
            return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4")
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<h1>Example OK</h1>")

    orig_client = httpx.Client
    transport = httpx.MockTransport(_mock_handler)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: orig_client(*a, transport=transport, **k))

    with patch("src.aura.mcp.jarvis_server.check_ssrf", side_effect=lambda u: "Blocked private IP" if "127.0.0.1" in u else None):
        out_redirect = js.fetch_url_impl("https://93.184.216.34/start")
        assert "SSRF BLOCKED" in out_redirect

        out_binary = js.fetch_url_impl("https://93.184.216.34/file.pdf")
        assert "Unsupported non-text content-type" in out_binary

        out_ok = js.fetch_url_impl("https://93.184.216.34/ok")
        assert "Example OK" in out_ok


def test_parse_ddg_lite_and_concurrent_research_topic(monkeypatch):
    lite_html = '''
    <table>
      <tr><td><a class="result-link" href="//127.0.0.1/secret">Blocked Local</a></td></tr>
      <tr><td class="result-snippet">Should be skipped.</td></tr>
      <tr><td><a class="result-link" href="https://example.org/helmet">Smart Helmet Crash Guide</a></td></tr>
      <tr><td class="result-snippet">ESP32 with MPU6050 detects falls and alerts via GPS.</td></tr>
      <tr><td><a class="result-link" href="https://example.org/gps">GPS GSM Alert Spec</a></td></tr>
      <tr><td class="result-snippet">Fallback snippet for second source.</td></tr>
    </table>
    '''
    parsed = js._parse_ddg_html(lite_html, max_results=5)
    assert len(parsed) == 2
    assert parsed[0]["url"] == "https://example.org/helmet"

    monkeypatch.setattr(js, "_search_ddg", lambda q, limit: (parsed, None))

    def _fake_fetch(url: str, max_chars: int = 800, timeout: float = 6.0) -> str:
        if url.endswith("/helmet"):
            return f"[URL: {url}]\nMPU6050 threshold tuning and SIM800L SOS alerts."
        return "FETCH ERROR: simulated timeout"

    monkeypatch.setattr(js, "fetch_url_impl", _fake_fetch)
    summary = js.research_topic_impl("smart helmet esp32", max_sources=2)
    assert "1/2 sources fetched" in summary
    assert "Smart Helmet Crash Guide" in summary
    assert "MPU6050 threshold tuning" in summary
    assert "Fallback snippet for second source." in summary
