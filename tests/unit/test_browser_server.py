"""Unit and live headless Chromium tests for the Phase-1 Browser MCP server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from mcp import Client, StdioServerParameters

from src.aura.core.scope import TaskScope, select_active_tools
from src.aura.mcp.browser_server import (
    BrowserManager,
    back_impl,
    click_impl,
    close_impl,
    navigate_impl,
    scroll_impl,
    snapshot_impl,
    type_impl,
)


def test_browser_ssrf_and_ro_guards():
    assert "SSRF BLOCKED" in navigate_impl("http://127.0.0.1:8000", read_only=False)
    assert "SSRF BLOCKED" in navigate_impl("file:///C:/Windows/win.ini", read_only=False)
    assert "READ-ONLY BLOCKED" in navigate_impl("https://example.com", read_only=True)
    assert "READ-ONLY BLOCKED" in click_impl("Submit", read_only=True)
    assert "READ-ONLY BLOCKED" in type_impl("Email", "a@b.com", read_only=True)
    assert "READ-ONLY BLOCKED" in scroll_impl("down", read_only=True)
    assert "READ-ONLY BLOCKED" in back_impl(read_only=True)


def test_browser_scope_gating():
    web_scope = TaskScope("open browser and fill the search form")
    assert web_scope.browser is True
    code_scope = TaskScope("fix bug in server.py")
    assert code_scope.browser is False

    fake_tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="browser_navigate"),
        SimpleNamespace(name="browser_click"),
    ]
    assert [t.name for t in select_active_tools(fake_tools, web_scope)] == [
        "read_file",
        "browser_navigate",
        "browser_click",
    ]
    assert [t.name for t in select_active_tools(fake_tools, code_scope)] == ["read_file"]


def test_browser_live_headless_dom_workflow():
    mgr = BrowserManager()
    html = (
        "data:text/html,"
        "<title>AURA Test Form</title>"
        "<div style='height:1800px'>"
        "<input id='q' placeholder='Search topic'/>"
        "<button id='go' onclick=\"document.getElementById('out').innerText='Typed: '+document.getElementById('q').value\">Run</button>"
        "<p id='out'>Idle</p>"
        "</div>"
    )
    try:
        snap1 = navigate_impl(html, headless=True, read_only=False, manager=mgr, allow_local=True)
        assert "Title: AURA Test Form" in snap1
        assert "<input> Search topic" in snap1
        assert "<button> Run" in snap1

        snap2 = type_impl("Search topic", "AURA Phase 1", read_only=False, manager=mgr)
        assert "Title: AURA Test Form" in snap2

        snap3 = click_impl("2", read_only=False, manager=mgr)
        assert "Typed: AURA Phase 1" in snap3

        snap4 = scroll_impl("down", amount=400, read_only=False, manager=mgr)
        assert "Title: AURA Test Form" in snap4

        ro_snap = snapshot_impl(manager=mgr)
        assert "Typed: AURA Phase 1" in ro_snap
    finally:
        assert "closed" in close_impl(manager=mgr).lower()


def test_browser_mcp_server_stdio_discovery():
    script = str(Path("src/aura/mcp/browser_server.py").resolve())
    env = os.environ.copy()
    env["AURA_READ_ONLY"] = "1"

    async def _discover() -> list[str]:
        async with Client(
            StdioServerParameters(command=sys.executable, args=[script], env=env)
        ) as client:
            res = await client.list_tools()
            return [t.name for t in res.tools]

    names = asyncio.run(_discover())
    for expected in (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_close",
    ):
        assert expected in names


def test_browser_refs_password_masking_stale_recovery_and_link_form_ssrf():
    from src.aura.mcp.browser_server import _format_snapshot

    formatted = _format_snapshot(
        {
            "title": "Login",
            "url": "https://example.com/login",
            "items": [
                {"idx": 1, "ref": "e1", "tag": "input", "label": "Secret", "role": "textbox", "val": "***"},
                {"idx": 2, "ref": "e2", "tag": "button", "label": "Sign In", "role": "button", "disabled": False, "expanded": "false"},
            ],
            "text": "Welcome",
        }
    )
    assert '[e1] <input> Secret [textbox, value="***"]' in formatted
    assert "[e2] <button> Sign In [expanded=false]" in formatted

    mgr = BrowserManager()
    html = (
        "data:text/html,"
        "<title>Refs & SSRF Test</title>"
        "<form action='http://127.0.0.1:8000/pwn' method='post'>"
        "<input id='pw' type='password' aria-label='Passcode'/>"
        "<button id='sub' type='submit'>Submit Local</button>"
        "</form>"
        "<a id='ssrf-link' href='http://169.254.169.254/latest/meta-data/'>Metadata Link</a>"
        "<button id='btn-dyn' aria-label='Dynamic Save' onclick=\"this.innerText='Saved OK'\">Dynamic Save</button>"
    )
    try:
        snap = navigate_impl(html, headless=True, read_only=False, manager=mgr, allow_local=True)
        assert "[e1]" in snap and "Passcode" in snap
        assert "[e2]" in snap and "Submit Local" in snap
        assert "[e3]" in snap and "Metadata Link" in snap

        typed = type_impl("e1", "supersecret123", read_only=False, manager=mgr)
        assert 'value="***"' in typed
        assert "supersecret123" not in typed

        # Enable strict SSRF enforcement for click/submit target checks
        mgr.set_allow_local(False)
        blocked_click = click_impl("e3", read_only=False, manager=mgr)
        assert "SSRF BLOCKED" in blocked_click
        blocked_submit = type_impl("e1", "secret", submit=True, read_only=False, manager=mgr)
        assert "SSRF BLOCKED" in blocked_submit

        # Re-allow data/local page to test stale-ref recovery when data-aura-* attrs are wiped
        mgr.set_allow_local(True)
        snap_again = snapshot_impl(manager=mgr)
        assert "Dynamic Save" in snap_again
        drv = mgr.get_driver(create=False)
        drv.execute_script(
            "document.querySelectorAll('[data-aura-ref]').forEach(el => {"
            "  el.removeAttribute('data-aura-ref'); el.removeAttribute('data-aura-idx');"
            "});"
        )
        recovered = click_impl("e4", read_only=False, manager=mgr)
        assert "Saved OK" in recovered
    finally:
        close_impl(manager=mgr)


def test_browser_tab_switch_on_blank_target_and_closed_tab_recovery():
    mgr = BrowserManager()
    html = (
        "data:text/html,"
        "<title>Main Tab</title>"
        "<button id='open-tab' onclick=\"const w = window.open('about:blank','_blank'); "
        "w.document.write('<title>Child Tab</title><h1>Child Loaded</h1>'); w.document.close();\">Open Child</button>"
    )
    try:
        snap1 = navigate_impl(html, headless=True, read_only=False, manager=mgr, allow_local=True)
        assert "[e1] <button> Open Child" in snap1
        snap2 = click_impl("e1", read_only=False, manager=mgr)
        assert "Child Tab" in snap2 or "Child Loaded" in snap2
        assert "Tabs: 2" in snap2

        drv = mgr.get_driver(create=False)
        drv.close()
        # Session auto-recovers live remaining window on next snapshot/close
        snap3 = snapshot_impl(manager=mgr)
        assert "Main Tab" in snap3
    finally:
        close_impl(manager=mgr)
