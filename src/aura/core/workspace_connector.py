"""
Direct Google Workspace & Local Agenda Connector (Phase 2).

Provides background Gmail (IMAP/SMTP or REST Bearer token) and Calendar
(local JSON agenda + private .ics iCal feed + Google Calendar REST API) access
without needing to launch a visible browser window.
"""

from __future__ import annotations

from datetime import datetime
import email
from email.header import decode_header
from email.message import EmailMessage
import imaplib
import json
import os
from pathlib import Path
import re
import smtplib
import tempfile
import time
import uuid
from typing import Any


_CONFIG_FILENAME = "google_config.json"
_CALENDAR_FILENAME = "calendar_events.json"


def _resolve_storage_dir(storage_dir: Path | None = None) -> Path:
    if storage_dir is not None:
        return Path(storage_dir)
    from ..config.settings import get_default_storage_dir

    return get_default_storage_dir()


def load_google_config(storage_dir: Path | None = None) -> dict[str, str]:
    """Load Gmail / Calendar settings from <storage_dir>/google_config.json and env vars."""
    base_dir = _resolve_storage_dir(storage_dir)
    cfg_path = base_dir / _CONFIG_FILENAME
    file_data: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                file_data = raw
        except Exception:
            file_data = {}

    def _pick(env_key: str, json_key: str) -> str:
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            return env_val
        return str(file_data.get(json_key, "") or file_data.get(env_key, "") or "").strip()

    gmail_ok = file_data.get("gmail_enabled", True) is not False
    cal_ok = file_data.get("calendar_enabled", True) is not False
    return {
        "gmail_address": _pick("GMAIL_ADDRESS", "gmail_address") if gmail_ok else "",
        "gmail_app_password": _pick("GMAIL_APP_PASSWORD", "gmail_app_password") if gmail_ok else "",
        "google_oauth_token": _pick("GOOGLE_OAUTH_TOKEN", "google_oauth_token") if (gmail_ok or cal_ok) else "",
        "gcal_ical_url": _pick("GCAL_ICAL_URL", "gcal_ical_url") if cal_ok else "",
    }


def _load_local_events(cal_path: Path) -> list[dict[str, str]]:
    if not cal_path.is_file():
        return []
    try:
        raw = json.loads(cal_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _save_local_events(cal_path: Path, events: list[dict[str, str]]) -> None:
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(events, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".aura_cal_", suffix=".tmp", dir=str(cal_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, cal_path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _format_ics_timestamp(raw_val: str) -> str:
    val = raw_val.strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?Z?$", val)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    m_date = re.match(r"^(\d{4})(\d{2})(\d{2})$", val)
    if m_date:
        return f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}"
    return val


def _parse_ics_events(ics_text: str) -> list[dict[str, str]]:
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text or "")
    events: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for raw_line in unfolded.splitlines():
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            cur = {"id": "ics", "title": "", "start_time": "", "notes": "", "source": "iCal"}
        elif line == "END:VEVENT":
            if cur is not None and (cur["title"] or cur["start_time"]):
                cur["title"] = cur["title"] or "(Untitled Event)"
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key_part, val = line.split(":", 1)
            prop = key_part.split(";", 1)[0].upper()
            clean_val = val.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()
            if prop == "SUMMARY":
                cur["title"] = clean_val
            elif prop == "DTSTART":
                cur["start_time"] = _format_ics_timestamp(clean_val)
            elif prop == "DESCRIPTION":
                cur["notes"] = clean_val[:180]
            elif prop == "UID" and clean_val:
                cur["id"] = clean_val[:8]
    return events


def _ssrf_check_url(url: str) -> str | None:
    from ..mcp.jarvis_server import check_ssrf

    return check_ssrf(url)


def _fetch_ical_events(ical_url: str) -> tuple[list[dict[str, str]], str | None]:
    from urllib.parse import urljoin
    import httpx

    cur = ical_url.strip()
    for _ in range(4):
        err = _ssrf_check_url(cur)
        if err:
            return [], f"SSRF BLOCKED (iCal): {err}"
        try:
            resp = httpx.get(cur, timeout=8.0, follow_redirects=False)
            if getattr(resp, "status_code", 200) in {301, 302, 303, 307, 308} and "location" in getattr(resp, "headers", {}):
                cur = urljoin(cur, resp.headers["location"])
                continue
            resp.raise_for_status()
            return _parse_ics_events(resp.text), None
        except Exception as exc:
            return [], f"iCal sync error: {exc}"
    return [], "iCal sync error: Too many redirects."


def _fetch_gcal_rest_events(oauth_token: str) -> tuple[list[dict[str, str]], str | None]:
    import httpx

    try:
        resp = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            params={"singleEvents": "true", "orderBy": "startTime", "maxResults": 10},
            headers={"Authorization": f"Bearer {oauth_token}"},
            timeout=8.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return [], f"Google Calendar API error: {exc}"

    out: list[dict[str, str]] = []
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        start_obj = item.get("start") or {}
        start_raw = str(start_obj.get("dateTime") or start_obj.get("date") or "")
        out.append({
            "id": str(item.get("id", "gcal"))[:8],
            "title": str(item.get("summary") or "(Untitled Event)"),
            "start_time": start_raw.replace("T", " ")[:16],
            "notes": str(item.get("description") or "")[:180],
            "source": "Google Calendar",
        })
    return out, None


def calendar_manage_impl(
    action: str = "today",
    title: str = "",
    start_time: str = "",
    notes: str = "",
    event_id: str = "",
    *,
    read_only: bool = False,
    storage_dir: Path | None = None,
) -> str:
    """Manage local persistent agenda events and optional iCal/Google Calendar sync."""
    base_dir = _resolve_storage_dir(storage_dir)
    cal_path = base_dir / _CALENDAR_FILENAME
    act = (action or "today").strip().lower()

    if act in {"add", "create"}:
        if read_only:
            return "READ-ONLY BLOCKED: Switch to RW mode to add or modify calendar events."
        clean_title = " ".join((title or "").split()).strip()
        if not clean_title:
            return "Error: Event title is required."
        clean_start = (start_time or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M")
        events = _load_local_events(cal_path)
        entry = {
            "id": uuid.uuid4().hex[:8],
            "title": clean_title,
            "start_time": clean_start,
            "notes": " ".join((notes or "").split()).strip(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        events.append(entry)
        _save_local_events(cal_path, events)

        # Best-effort Google Calendar write if OAuth token is present
        cfg = load_google_config(base_dir)
        gcal_note = ""
        if cfg["google_oauth_token"]:
            try:
                import httpx

                iso_start = clean_start.replace(" ", "T") + (":00Z" if len(clean_start) <= 16 and ":" in clean_start else "")
                body = {"summary": clean_title, "description": entry["notes"]}
                if "T" in iso_start:
                    body["start"] = {"dateTime": iso_start}
                    body["end"] = {"dateTime": iso_start}
                else:
                    body["start"] = {"date": clean_start[:10]}
                    body["end"] = {"date": clean_start[:10]}
                resp = httpx.post(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                    headers={"Authorization": f"Bearer {cfg['google_oauth_token']}"},
                    json=body,
                    timeout=8.0,
                )
                if resp.status_code < 300:
                    gcal_note = " (also synced to Google Calendar)"
            except Exception:
                pass
        return f"Calendar event scheduled: [{entry['id']}] '{clean_title}' at {clean_start}{gcal_note}."

    if act in {"delete", "remove"}:
        if read_only:
            return "READ-ONLY BLOCKED: Switch to RW mode to delete calendar events."
        target_id = (event_id or "").strip().lower()
        target_title = (title or "").strip().lower()
        if not target_id and not target_title:
            return "Error: Provide event_id or title to delete a calendar event."
        events = _load_local_events(cal_path)
        kept: list[dict[str, str]] = []
        removed = 0
        for ev in events:
            ev_id = str(ev.get("id", "")).strip().lower()
            ev_title = str(ev.get("title", "")).strip().lower()
            if (target_id and ev_id == target_id) or (not target_id and target_title and target_title in ev_title):
                removed += 1
            else:
                kept.append(ev)
        if removed == 0:
            return "No matching calendar event found."
        _save_local_events(cal_path, kept)
        return f"Deleted {removed} calendar event(s)."

    if act in {"today", "list", "upcoming"}:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        all_events: list[dict[str, str]] = [
            {
                "id": str(ev.get("id", "")),
                "title": str(ev.get("title", "")),
                "start_time": str(ev.get("start_time", "")),
                "notes": str(ev.get("notes", "")),
                "source": "Local",
            }
            for ev in _load_local_events(cal_path)
        ]

        warnings: list[str] = []
        cfg = load_google_config(base_dir)
        if cfg["gcal_ical_url"]:
            ics_events, err = _fetch_ical_events(cfg["gcal_ical_url"])
            if err:
                warnings.append(err)
            all_events.extend(ics_events)
        if cfg["google_oauth_token"]:
            gcal_events, err = _fetch_gcal_rest_events(cfg["google_oauth_token"])
            if err:
                warnings.append(err)
            all_events.extend(gcal_events)

        if not all_events:
            msg = (
                "Calendar: No scheduled events found for today. "
                "Use jarvis_calendar(action='add', title='...', start_time='YYYY-MM-DD HH:MM') in RW mode to schedule one."
            )
            if warnings:
                msg += "\n" + "\n".join(f"Warning: {w}" for w in warnings)
            return msg

        if act == "today":
            today_matches = [ev for ev in all_events if ev.get("start_time", "").startswith(today_prefix)]
            shown = today_matches if today_matches else [
                ev for ev in all_events if not ev.get("start_time") or ev["start_time"] >= today_prefix
            ]
            heading = f"Calendar Events for {today_prefix}" if today_matches else "Upcoming Calendar Events"
        else:
            shown = all_events
            heading = "Scheduled Calendar Events"

        if not shown:
            msg = f"Calendar: No events matching '{act}' ({len(all_events)} past event(s) stored)."
            if warnings:
                msg += "\n" + "\n".join(f"Warning: {w}" for w in warnings)
            return msg

        shown.sort(key=lambda e: e.get("start_time", ""))
        lines = [f"### {heading}"]
        for ev in shown[:25]:
            note_suffix = f" — {ev['notes']}" if ev.get("notes") else ""
            lines.append(
                f"- [{ev.get('id', '?')}] **{ev.get('start_time') or 'TBD'}** | {ev.get('title', '(Untitled)')} "
                f"({ev.get('source', 'Local')}){note_suffix}"
            )
        if warnings:
            lines.extend(f"Warning: {w}" for w in warnings)
        return "\n".join(lines)

    return f"ERROR: Unknown calendar action '{action}'. Use today, list, upcoming, add, or delete."


def _decode_mime_header(raw_header: str | None) -> str:
    if not raw_header:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(raw_header):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def _extract_email_snippet(msg: email.message.Message, max_chars: int = 200) -> str:
    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, (bytes, bytearray)):
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = bytes(payload).decode(charset, errors="replace")
            if ctype == "text/plain" and not text_body:
                text_body = decoded
                break
            if ctype == "text/html" and not html_body:
                html_body = decoded
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            charset = msg.get_content_charset() or "utf-8"
            raw_text = bytes(payload).decode(charset, errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                html_body = raw_text
            else:
                text_body = raw_text

    raw_out = text_body or re.sub(r"<[^>]+>", " ", html_body)
    compact = " ".join(raw_out.split())
    if len(compact) > max_chars:
        return compact[: max_chars - 3].rstrip() + "..."
    return compact or "(empty message body)"


def gmail_manage_impl(
    action: str = "unread",
    query: str = "",
    to: str = "",
    subject: str = "",
    body: str = "",
    max_results: int = 5,
    *,
    read_only: bool = False,
    storage_dir: Path | None = None,
) -> str:
    """Read/search Gmail inbox messages or send email directly without a browser."""
    act = (action or "unread").strip().lower()
    cfg = load_google_config(storage_dir)
    addr = cfg["gmail_address"]
    app_pw = cfg["gmail_app_password"]
    oauth_tok = cfg["google_oauth_token"]

    if act == "send":
        if read_only:
            return "READ-ONLY BLOCKED: Switch to RW mode to send emails."
        clean_to = (to or "").strip()
        clean_subject = (subject or "").strip()
        if any(ch in val for val in (clean_to, clean_subject) for ch in ("\r", "\n")):
            return "Error: Email recipient and subject cannot contain newline characters."
        if not clean_to or "@" not in clean_to:
            return "Error: A valid recipient email address ('to') is required."
        if not clean_subject:
            return "Error: Email subject is required."
        if not (addr and app_pw) and not oauth_tok:
            return (
                "Gmail Direct Connector: No Gmail credentials configured yet. "
                "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your environment (or logs/google_config.json) "
                "to send emails directly, or use browser_navigate('gmail') in RW mode."
            )

        msg = EmailMessage()
        if addr:
            msg["From"] = addr
        msg["To"] = clean_to
        msg["Subject"] = clean_subject
        msg.set_content(body or "")

        if addr and app_pw:
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
                    smtp.login(addr, app_pw)
                    smtp.send_message(msg)
                return f"Email sent to {clean_to} with subject '{clean_subject}'."
            except Exception as exc:
                return f"GMAIL SEND ERROR: {exc}"

        import base64
        import httpx

        raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            resp = httpx.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {oauth_tok}"},
                json={"raw": raw_b64},
                timeout=10.0,
            )
            resp.raise_for_status()
            return f"Email sent to {clean_to} with subject '{clean_subject}'."
        except Exception as exc:
            return f"GMAIL API SEND ERROR: {exc}"

    if act in {"unread", "search", "read", "inbox"}:
        if not (addr and app_pw) and not oauth_tok:
            return (
                "Gmail Direct Connector: No Gmail credentials configured yet. "
                "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your environment (or logs/google_config.json) "
                "for background IMAP/SMTP access, or use browser_navigate('gmail') in RW mode to open Gmail in the browser."
            )

        limit = max(1, min(int(max_results or 5), 10))

        if addr and app_pw:
            mail: imaplib.IMAP4_SSL | None = None
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                mail.login(addr, app_pw)
                mail.select("INBOX", readonly=True)

                if act == "unread":
                    criteria = "UNSEEN"
                elif act == "search" and query.strip():
                    safe_q = query.strip().replace('"', " ").replace("\\", " ")
                    criteria = f'(TEXT "{safe_q}")'
                else:
                    criteria = "ALL"

                typ, data = mail.search(None, criteria)
                if typ != "OK" or not data or not data[0]:
                    return f"Gmail ({act}): No matching messages found."
                msg_ids = [mid for mid in data[0].split() if mid]
                if not msg_ids:
                    return f"Gmail ({act}): No matching messages found."

                target_ids = list(reversed(msg_ids[-limit:]))
                entries: list[str] = [f"### Gmail Inbox ({act}, showing {len(target_ids)} of {len(msg_ids)})"]
                for mid in target_ids:
                    f_typ, f_data = mail.fetch(mid, "(RFC822)")
                    if f_typ != "OK" or not f_data:
                        continue
                    raw_bytes: bytes | None = None
                    for item in f_data:
                        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                            raw_bytes = bytes(item[1])
                            break
                    if not raw_bytes:
                        continue
                    parsed = email.message_from_bytes(raw_bytes)
                    sender = _decode_mime_header(parsed.get("From")) or "(Unknown Sender)"
                    subj = _decode_mime_header(parsed.get("Subject")) or "(No Subject)"
                    date_str = _decode_mime_header(parsed.get("Date"))
                    snippet = _extract_email_snippet(parsed)
                    entries.append(
                        f"- **From**: {sender}\n"
                        f"  **Subject**: {subj}"
                        + (f" ({date_str})" if date_str else "")
                        + f"\n  **Snippet**: {snippet}"
                    )
                return "\n".join(entries) if len(entries) > 1 else f"Gmail ({act}): No readable messages returned."
            except Exception as exc:
                return f"GMAIL IMAP ERROR: {exc}"
            finally:
                if mail is not None:
                    try:
                        mail.logout()
                    except Exception:
                        pass

        # OAuth Bearer REST fallback
        import httpx

        q_param = "is:unread" if act == "unread" else (query.strip() if act == "search" else "")
        try:
            params: dict[str, Any] = {"maxResults": limit}
            if q_param:
                params["q"] = q_param
            list_resp = httpx.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                params=params,
                headers={"Authorization": f"Bearer {oauth_tok}"},
                timeout=10.0,
            )
            list_resp.raise_for_status()
            refs = list_resp.json().get("messages", [])
            if not refs:
                return f"Gmail ({act}): No matching messages found."
            lines = [f"### Gmail Inbox ({act}, showing {min(len(refs), limit)})"]
            for ref in refs[:limit]:
                mid = ref.get("id", "")
                if not mid:
                    continue
                msg_resp = httpx.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                    headers={"Authorization": f"Bearer {oauth_tok}"},
                    timeout=8.0,
                )
                msg_resp.raise_for_status()
                mdata = msg_resp.json()
                hdr_map = {
                    h.get("name", "").lower(): h.get("value", "")
                    for h in mdata.get("payload", {}).get("headers", [])
                    if isinstance(h, dict)
                }
                lines.append(
                    f"- **From**: {hdr_map.get('from', '(Unknown)')}\n"
                    f"  **Subject**: {hdr_map.get('subject', '(No Subject)')}\n"
                    f"  **Snippet**: {mdata.get('snippet', '')}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"GMAIL API ERROR: {exc}"

    return f"ERROR: Unknown Gmail action '{action}'. Use unread, search, read, inbox, or send."


def _format_relative_age(ts: float | int | str | None, *, now: float | None = None) -> str:
    """Format epoch seconds or ISO timestamp into human-readable relative age."""
    if ts is None or ts == "":
        return ""
    try:
        if isinstance(ts, (int, float)):
            epoch = float(ts)
        else:
            epoch = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return ""
    ref = now if now is not None else time.time()
    delta = max(0, int(ref - epoch))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def briefing_impl(
    *,
    read_only: bool = True,
    storage_dir: Path | None = None,
) -> str:
    """Build a proactive daily briefing combining time, calendar agenda, Gmail inbox status, To-Do notes, and personal memory."""
    del read_only  # Briefing is strictly read-only safe.
    base_dir = _resolve_storage_dir(storage_dir)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%I:%M %p on %A, %B %d, %Y")
    now_epoch = now_dt.timestamp()
    cal_summary = calendar_manage_impl("today", read_only=True, storage_dir=base_dir)

    cfg = load_google_config(base_dir)
    if (cfg["gmail_address"] and cfg["gmail_app_password"]) or cfg["google_oauth_token"]:
        inbox_summary = gmail_manage_impl("unread", max_results=3, read_only=True, storage_dir=base_dir)
    else:
        inbox_summary = (
            "Inbox: Direct Gmail credentials not configured "
            "(use browser_navigate('gmail') or set GMAIL_ADDRESS + GMAIL_APP_PASSWORD)."
        )

    from .personal_memory import PersonalMemoryStore

    facts = PersonalMemoryStore(base_dir).list_facts()[:5]
    if facts:
        note_lines: list[str] = []
        for f in facts:
            ts = getattr(f, "created_at", None) or getattr(f, "updated_at", None)
            age = _format_relative_age(ts, now=now_epoch)
            age_suffix = f" ({age})" if age else ""
            note_lines.append(f"- [{f.category}] {f.text}{age_suffix}")
        notes_block = "\n".join(note_lines)
    else:
        notes_block = "(no personal memory notes saved yet)"

    try:
        from ..mcp.desktop_server import todo_note_impl

        todo_summary = todo_note_impl(action="read", storage_dir=base_dir, read_only=True)
    except Exception:
        todo_summary = "To-Do list is empty."

    return (
        f"## Jarvis Daily Briefing\n"
        f"**Current Time:** {now_str}\n\n"
        f"{cal_summary}\n\n"
        f"### Inbox Status\n{inbox_summary}\n\n"
        f"### To-Do List\n{todo_summary}\n\n"
        f"### Active Notes & Preferences\n{notes_block}"
    )


def mask_account_identifier(value: str) -> str:
    """Return a privacy-safe masked email or identifier string."""
    clean = (value or "").strip()
    if not clean:
        return ""
    if "@" in clean:
        local, domain = clean.split("@", 1)
        head = local[:2] if len(local) >= 2 else local[:1]
        return f"{head}***@{domain}"
    return (clean[:2] + "***") if len(clean) > 2 else "***"


def _load_raw_google_json(storage_dir: Path | None = None) -> dict[str, Any]:
    base = _resolve_storage_dir(storage_dir)
    cfg_path = base / _CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def get_apps_status(storage_dir: Path | None = None) -> dict[str, Any]:
    """Return non-secret connection status for Gmail and Calendar apps."""
    raw = _load_raw_google_json(storage_dir)

    def _raw_or_env(env_key: str, json_key: str) -> str:
        return (os.environ.get(env_key, "") or str(raw.get(json_key, "") or raw.get(env_key, "") or "")).strip()

    gmail_enabled = bool(raw.get("gmail_enabled", True) is not False)
    cal_enabled = bool(raw.get("calendar_enabled", True) is not False)
    gmail_addr = _raw_or_env("GMAIL_ADDRESS", "gmail_address")
    gmail_pw = _raw_or_env("GMAIL_APP_PASSWORD", "gmail_app_password")
    oauth_tok = _raw_or_env("GOOGLE_OAUTH_TOKEN", "google_oauth_token")
    ical_url = _raw_or_env("GCAL_ICAL_URL", "gcal_ical_url")
    has_gmail_creds = bool(gmail_addr and (gmail_pw or oauth_tok))
    has_oauth = bool(oauth_tok)
    has_ical = bool(ical_url)
    cal_source = "google" if (has_oauth and cal_enabled) else ("ical" if (has_ical and cal_enabled) else "local")
    return {
        "gmail": {
            "id": "gmail",
            "label": "Gmail",
            "connected": bool(has_gmail_creds and gmail_enabled),
            "configured": has_gmail_creds,
            "enabled": gmail_enabled,
            "account": mask_account_identifier(gmail_addr),
        },
        "calendar": {
            "id": "calendar",
            "label": "Google Calendar",
            "connected": bool((has_oauth or has_ical) and cal_enabled),
            "configured": bool(has_oauth or has_ical),
            "enabled": cal_enabled,
            "source": cal_source,
        },
    }


def save_google_config(updates: dict[str, Any], storage_dir: Path | None = None) -> dict[str, Any]:
    """Safely persist Gmail/Calendar connector fields or enabled toggles to google_config.json."""
    base = _resolve_storage_dir(storage_dir)
    cfg_path = base / _CONFIG_FILENAME
    data = _load_raw_google_json(base)

    for str_key in ("gmail_address", "gmail_app_password", "google_oauth_token", "gcal_ical_url"):
        if str_key in updates and updates[str_key] is not None:
            val = str(updates[str_key]).strip()
            if str_key == "gcal_ical_url" and val:
                err = _ssrf_check_url(val)
                if err:
                    raise ValueError(err)
            if val:
                data[str_key] = val
            else:
                data.pop(str_key, None)

    for bool_key in ("gmail_enabled", "calendar_enabled"):
        if bool_key in updates and updates[bool_key] is not None:
            data[bool_key] = bool(updates[bool_key])

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return get_apps_status(base)


def get_dashboard_snapshot(storage_dir: Path | None = None) -> dict[str, Any]:
    """Return real local To-Dos, Calendar Agenda, Apps status, and honest Briefing lines."""
    from ..mcp.desktop_server import list_todo_items

    base = _resolve_storage_dir(storage_dir)
    todos = [{"text": txt, "done": done} for txt, done in list_todo_items(base)]
    all_events = _load_local_events(base / _CALENDAR_FILENAME)
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    upcoming = [
        ev for ev in all_events
        if not ev.get("start_time") or str(ev.get("start_time", "")).startswith(today_prefix) or str(ev.get("start_time", "")) >= today_prefix
    ]
    upcoming.sort(key=lambda e: str(e.get("start_time", "")))
    agenda = [
        {
            "id": str(ev.get("id", "")),
            "title": str(ev.get("title", "")) or "(Untitled event)",
            "start_time": str(ev.get("start_time", "")) or "Today",
            "notes": str(ev.get("notes", "")),
        }
        for ev in upcoming[:5]
    ]

    apps = get_apps_status(base)
    done_cnt = sum(1 for item in todos if item["done"])
    open_cnt = len(todos) - done_cnt
    briefing: list[str] = []
    if todos:
        briefing.append(f"{open_cnt} open to-do(s), {done_cnt} done in Jarvis Notes")
    else:
        briefing.append("No to-dos added yet — add a task below or say 'add todo ...'")

    if agenda:
        first_ev = agenda[0]
        briefing.append(f"{len(agenda)} upcoming event(s) — next: {first_ev['title']} ({first_ev['start_time']})")
    elif apps["calendar"]["connected"]:
        briefing.append("Calendar linked — ask AURA 'what is on my calendar today?'")
    else:
        briefing.append("No calendar events saved — connect Google Calendar in Apps")

    if apps["gmail"]["connected"]:
        briefing.append(f"Gmail connected ({apps['gmail']['account'] or 'ready'}) — click Inbox to check")
    else:
        briefing.append("Gmail not connected — open Apps to link Gmail")

    gmail_cards: list[dict[str, Any]] = []
    if apps["gmail"]["connected"]:
        gmail_cards.append({
            "sender": apps["gmail"]["account"] or "Gmail",
            "time": "Connected",
            "subject": "Click Inbox or ask AURA to fetch unread mail",
        })

    return {
        "todos": todos,
        "agenda": agenda,
        "gmail": gmail_cards,
        "briefing": briefing,
        "apps": apps,
    }
