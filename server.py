#!/usr/bin/env python3
"""Enhanced Gmail MCP server with SMTP, templates, calendar invites, and IMAP tools."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import imaplib
import mimetypes
import os
import smtplib
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from pathlib import Path
import re
from string import Template
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

ENV_FILE_DEFAULT = Path.home() / ".config" / "mcp" / "gmail_smtp.env"
TEMPLATE_DIR = Path(__file__).with_name("templates")
SIGNATURE_DIR = Path(__file__).with_name("signatures")
ATTACHMENTS_DIR = Path(__file__).with_name("attachments")
ASSETS_DIR = Path(__file__).with_name("assets")
SPOOL_ROOT = Path(__file__).with_name("spool")
SPOOL_PENDING_DIR = SPOOL_ROOT / "pending"
SPOOL_SENT_DIR = SPOOL_ROOT / "sent"
SPOOL_FAILED_DIR = SPOOL_ROOT / "failed"

_SAMPLE_INLINE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)

_SAMPLE_ATTACHMENTS: Dict[str, str] = {
    "sample-report.txt": (
        "Inside Sales Report\n"
        "====================\n"
        "- Opportunities reviewed.\n"
        "- Pipeline metrics attached.\n"
        "- Prepared automatically for Codex spool testing.\n"
    ),
    "implementation-notes.md": (
        "# Implementation Notes\n"
        "- Calendar invite uses ICS with METHOD:REQUEST.\n"
        "- Inline assets embed via CID references.\n"
        "- Attachments are small text placeholders for tests.\n"
    ),
}

DEFAULT_TEMPLATE_TEXT = (
    "Hi ${recipient_name},\n\n${intro}\n\nRegards,\n${sender_name}\n"
)

DEFAULT_TEMPLATE_HTML = (
    "<html>\n  <body style=\"font-family: Arial, sans-serif; color: #1f2933;\">\n    <p>Hi ${recipient_name},</p>\n    <p>${intro}</p>\n    <p>Regards,<br />${sender_name}</p>\n  </body>\n</html>\n"
)

DEFAULT_SIGNATURE_TEXT = "${name}\n${role} · ${company}\n${email}\n"

DEFAULT_SIGNATURE_HTML = (
    "<p style=\"font-family: Arial, sans-serif; font-size: 13px; color: #1f2933;\">\n      <strong>${name}</strong><br/>${role} · ${company}<br/>${email}\n    </p>\n"
)

server = FastMCP(
    name="gmail-smtp-mcp",
    instructions="""
Tools:
- gmail_prepare_sample_assets: stages sample attachments (in attachments/) and an inline PNG (assets/codex-inline.png) and returns their absolute paths.
- gmail_list_sample_assets: return the current sample attachment and inline image paths without regenerating them.
- gmail_send_email_with_attachments: send a message immediately. Provide JSON fields such as "to", "subject", optional "body" or "body_template", "template_variables", "attachments", "inline_images", "calendar_event", and "signature_template".
- gmail_queue_email_with_attachments: queue the same payload format for the background spooler to deliver.
- gmail_list_templates / gmail_list_signatures: discover existing snippets (with previews).
- gmail_create_template / gmail_create_signature: add or overwrite snippets (defaults supplied when bodies are omitted).

Example snippet:
{
  "to": ["recipient@example.com"],
  "subject": "Launch preview",
  "body_template": "modern_launch",
  "template_variables": {...},
  "attachments": ["/path/to/attachments/sample-report.txt"],
  "inline_images": [{"path": "/path/to/assets/codex-inline.png", "cid": "codex-inline"}],
  "calendar_event": {"summary": "Sprint sync", "start": "2025-10-25T18:00:00+02:00", "end": "2025-10-25T19:00:00+02:00", "location": "Online"},
  "signature_template": "work"
}

Paths should be absolute (the prepare tools return ready-to-use values). Calendar datetimes must be ISO 8601 with timezone offsets. Inline images require CID references in the HTML template.
Recommended workflow: prepare assets, list/create templates and signatures, then send or queue the email payload.
""",
)


class InlineImageSpec(BaseModel):
    path: str = Field(..., description="Path to the image file to embed inline.")
    cid: Optional[str] = Field(
        default=None,
        description="Optional Content-ID to reference inside HTML. Generated when omitted.",
    )


class InlineImageResult(BaseModel):
    cid: str = Field(..., description="Content-ID assigned to the inline image.")
    filename: str = Field(..., description="Filename used in the MIME part.")


class CalendarAttendee(BaseModel):
    email: str = Field(..., description="Email address of the attendee.")
    name: Optional[str] = Field(default=None, description="Attendee display name.")


class CalendarEventInput(BaseModel):
    summary: str = Field(..., description="Short summary/title for the event.")
    start: datetime = Field(..., description="Event start (ISO-8601).")
    end: datetime = Field(..., description="Event end (ISO-8601).")
    description: Optional[str] = Field(default=None, description="Longer event description.")
    location: Optional[str] = Field(default=None, description="Event location or meeting link.")
    organizer_email: Optional[str] = Field(
        default=None,
        description="Override the organizer email; defaults to the authenticated sender.",
    )
    attendees: List[CalendarAttendee] = Field(default_factory=list)
    timezone: Optional[str] = Field(default=None, description="IANA timezone for naive datetimes.")
    reminder_minutes: Optional[int] = Field(default=15, description="Reminder offset in minutes.")
    uid: Optional[str] = Field(default=None, description="Explicit UID for the calendar invite.")


class EmailSendResult(BaseModel):
    accepted: List[str] = Field(..., description="Recipients accepted by Gmail.")
    refused: Dict[str, str] = Field(default_factory=dict, description="Recipients refused by Gmail.")
    attachments: List[str] = Field(default_factory=list, description="Attachment filenames.")
    inline_images: List[InlineImageResult] = Field(default_factory=list)
    template_used: Optional[str] = None
    calendar_invite: Optional[str] = None
    smtp_diagnostics: Optional[Dict[str, Any]] = None
    signature_used: Optional[str] = Field(
        default=None, description="Signature template applied to the outgoing message."
    )


class EmailPreview(BaseModel):
    uid: str
    subject: str
    sender: str
    date: str
    snippet: Optional[str] = None


class EmailPreviewList(BaseModel):
    messages: List[EmailPreview] = Field(default_factory=list)
    criteria: str


class EmailPreviewResult(BaseModel):
    message: Optional[EmailPreview] = None


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _collect_credentials() -> Tuple[str, str, str, int, str]:
    env_path = Path(os.environ.get("GMAIL_SMTP_ENV_FILE", ENV_FILE_DEFAULT))
    _load_env_file(env_path)

    username = os.environ.get("GMAIL_SMTP_USERNAME")
    password = os.environ.get("GMAIL_SMTP_APP_PASSWORD")
    server_host = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
    port = int(os.environ.get("GMAIL_SMTP_PORT", "465"))
    from_address = os.environ.get("GMAIL_FROM_ADDRESS", username or "")

    if not username:
        raise ValueError("Set GMAIL_SMTP_USERNAME in the environment or env file.")
    if not password:
        raise ValueError("Set GMAIL_SMTP_APP_PASSWORD in the environment or env file.")
    if not from_address:
        raise ValueError("Set GMAIL_FROM_ADDRESS when username is unavailable.")

    return username, password, server_host, port, from_address


def _collect_imap_settings() -> Tuple[str, int]:
    env_path = Path(os.environ.get("GMAIL_SMTP_ENV_FILE", ENV_FILE_DEFAULT))
    _load_env_file(env_path)
    host = os.environ.get("GMAIL_IMAP_SERVER", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", "993"))
    return host, port


def _safe_template(text: str, variables: Dict[str, str]) -> str:
    if not variables:
        return text
    return Template(text).safe_substitute(variables)


def _render_template_pair(
    template_name: str, variables: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    text_path = TEMPLATE_DIR / f"{template_name}.txt"
    html_path = TEMPLATE_DIR / f"{template_name}.html"

    rendered_text = None
    rendered_html = None

    if text_path.exists():
        rendered_text = _safe_template(text_path.read_text(encoding="utf-8"), variables)
    if html_path.exists():
        rendered_html = _safe_template(html_path.read_text(encoding="utf-8"), variables)

    if rendered_text is None and rendered_html is None:
        raise FileNotFoundError(
            f"No template found for '{template_name}'. Expected {text_path.name} or {html_path.name}."
        )

    return rendered_text, rendered_html


def _render_signature_pair(
    signature_name: str, variables: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    text_path = SIGNATURE_DIR / f"{signature_name}.txt"
    html_path = SIGNATURE_DIR / f"{signature_name}.html"

    rendered_text = None
    rendered_html = None

    if text_path.exists():
        rendered_text = _safe_template(text_path.read_text(encoding="utf-8"), variables)
    if html_path.exists():
        rendered_html = _safe_template(html_path.read_text(encoding="utf-8"), variables)

    if rendered_text is None and rendered_html is None:
        raise FileNotFoundError(
            f"No signature found for '{signature_name}'. Expected {text_path.name} or {html_path.name}."
        )

    return rendered_text, rendered_html


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_text_if_missing(path: Path, text: str) -> None:
    if not path.exists():
        _ensure_parent(path)
        path.write_text(text, encoding="utf-8")


def _write_binary_if_missing(path: Path, data: bytes) -> None:
    if not path.exists():
        _ensure_parent(path)
        path.write_bytes(data)


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_slug(name: str, kind: str) -> str:
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{kind} name must contain only letters, numbers, hyphens, or underscores.")
    return name


def _prepare_sample_assets() -> Dict[str, Any]:
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    attachment_paths: List[str] = []
    for name, content in _SAMPLE_ATTACHMENTS.items():
        path = ATTACHMENTS_DIR / name
        _write_text_if_missing(path, content.rstrip() + "\n")
        attachment_paths.append(str(path))

    inline_path = ASSETS_DIR / "codex-inline.png"
    _write_binary_if_missing(inline_path, base64.b64decode(_SAMPLE_INLINE_PNG_BASE64))

    return {
        "attachments": attachment_paths,
        "inline_image": str(inline_path),
    }


def _gather_entries(base: Path) -> List[Dict[str, Any]]:
    entries: Dict[str, Dict[str, Any]] = {}
    if not base.exists():
        return []
    for path in base.glob('*.txt'):
        entries.setdefault(path.stem, {})['text_path'] = str(path)
    for path in base.glob('*.html'):
        entries.setdefault(path.stem, {})['html_path'] = str(path)
    result = []
    for name in sorted(entries):
        entry = entries[name]
        result.append({
            'name': name,
            'text_path': entry.get('text_path'),
            'html_path': entry.get('html_path'),
        })
    return result


def _write_content(path: Path, content: str, overwrite: bool) -> str:
    _ensure_parent(path)
    if path.exists() and not overwrite:
        raise ValueError(f"File {path} already exists. Set overwrite=True to replace it.")
    path.write_text(content, encoding='utf-8')
    return str(path)



def _ensure_spool_dirs() -> None:
    for directory in (SPOOL_PENDING_DIR, SPOOL_SENT_DIR, SPOOL_FAILED_DIR):
        directory.mkdir(parents=True, exist_ok=True)



def _queue_payload_file(payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> str:
    _ensure_spool_dirs()
    entry = {
        'schema_version': 1,
        'queued_at': datetime.now(timezone.utc).isoformat(),
        'metadata': metadata or {},
        'payload': payload,
    }
    filename = f"{int(time.time())}-{uuid.uuid4().hex}.json"
    target = SPOOL_PENDING_DIR / filename
    target.write_text(json.dumps(entry, indent=2), encoding='utf-8')
    return str(target)


def _preview_snippet(path: Optional[str], length: int = 160) -> Optional[str]:
    if not path:
        return None
    try:
        snippet = Path(path).read_text(encoding='utf-8')[:length].strip()
    except Exception:
        return None
    return snippet or None


@server.tool(
    name="gmail_list_templates",
    description="List available template names with the text/HTML files and short previews.",
)
def gmail_list_templates() -> Dict[str, Any]:
    entries = []
    for entry in _gather_entries(TEMPLATE_DIR):
        entries.append({
            'name': entry['name'],
            'text_path': entry.get('text_path'),
            'html_path': entry.get('html_path'),
            'text_preview': _preview_snippet(entry.get('text_path')),
            'html_preview': _preview_snippet(entry.get('html_path')),
        })
    return {'templates': entries}


@server.tool(
    name="gmail_create_template",
    description="Create or overwrite a template. Provide name plus optional text_body/html_body content. Defaults supplied when omitted.",
)
def gmail_create_template(
    name: str,
    text_body: Optional[str] = None,
    html_body: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    slug = _validate_slug(name, 'Template')
    if text_body is None and html_body is None:
        text_body = DEFAULT_TEMPLATE_TEXT
        html_body = DEFAULT_TEMPLATE_HTML
    result: Dict[str, Any] = {'name': slug}
    if text_body is not None:
        result['text_path'] = _write_content(TEMPLATE_DIR / f"{slug}.txt", text_body, overwrite)
    if html_body is not None:
        result['html_path'] = _write_content(TEMPLATE_DIR / f"{slug}.html", html_body, overwrite)
    return result


@server.tool(
    name="gmail_list_signatures",
    description="List available signature snippets with file paths and previews.",
)
def gmail_list_signatures() -> Dict[str, Any]:
    entries = []
    for entry in _gather_entries(SIGNATURE_DIR):
        entries.append({
            'name': entry['name'],
            'text_path': entry.get('text_path'),
            'html_path': entry.get('html_path'),
            'text_preview': _preview_snippet(entry.get('text_path')),
            'html_preview': _preview_snippet(entry.get('html_path')),
        })
    return {'signatures': entries}


@server.tool(
    name="gmail_create_signature",
    description="Create or overwrite a signature snippet. Provide name plus optional text/html content. Defaults supplied when omitted.",
)
def gmail_create_signature(
    name: str,
    text_body: Optional[str] = None,
    html_body: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    slug = _validate_slug(name, 'Signature')
    if text_body is None and html_body is None:
        text_body = DEFAULT_SIGNATURE_TEXT
        html_body = DEFAULT_SIGNATURE_HTML
    result: Dict[str, Any] = {'name': slug}
    if text_body is not None:
        result['text_path'] = _write_content(SIGNATURE_DIR / f"{slug}.txt", text_body, overwrite)
    if html_body is not None:
        result['html_path'] = _write_content(SIGNATURE_DIR / f"{slug}.html", html_body, overwrite)
    return result


@server.tool(
    name="gmail_list_sample_assets",
    description="Return the current sample attachments and inline image paths without regenerating them.",
)
def gmail_list_sample_assets() -> Dict[str, Any]:
    assets = _prepare_sample_assets()
    return {
        'attachments': assets['attachments'],
        'inline_image': assets['inline_image'],
    }


def _apply_timezone(dt: datetime, tz_name: Optional[str]) -> datetime:
    if dt.tzinfo is None:
        zone = ZoneInfo(tz_name) if tz_name else timezone.utc
        dt = dt.replace(tzinfo=zone)
    return dt


@server.tool(
    name="gmail_prepare_sample_assets",
    description=(
        "Create sample attachments and inline-image assets inside the repository. Returns the absolute paths "
        "you can pass to gmail_send_email_with_attachments."
    ),
)
def gmail_prepare_sample_assets() -> Dict[str, Any]:
    """Expose helper for Codex/GPT sessions to stage local files before sending."""

    assets = _prepare_sample_assets()
    return {
        "attachments": assets["attachments"],
        "inline_image": assets["inline_image"],
        "notes": (
            "Attachments live under the repository's 'attachments/' directory. "
            "The inline image is stored at 'assets/codex-inline.png' and can be referenced via CID 'codex-inline'."
        ),
    }


def _format_ics_datetime(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_calendar_invite(
    event: CalendarEventInput,
    sender: str,
    recipients: List[str],
) -> Tuple[str, bytes]:
    tz_name = event.timezone
    start = _apply_timezone(event.start, tz_name)
    end = _apply_timezone(event.end, tz_name)
    if end <= start:
        raise ValueError("Calendar event 'end' must be after 'start'.")

    dtstamp = datetime.now(timezone.utc)
    organizer = event.organizer_email or sender
    uid = event.uid or f"{uuid.uuid4()}@gmail-smtp-mcp"

    attendees = event.attendees or [CalendarAttendee(email=email.strip()) for email in recipients]

    attendee_lines: List[str] = []
    seen_attendees = set()
    for attendee in attendees:
        email = attendee.email.strip()
        if not email or email.lower() in seen_attendees:
            continue
        seen_attendees.add(email.lower())
        params = ["ROLE=REQ-PARTICIPANT", "PARTSTAT=NEEDS-ACTION", "RSVP=TRUE"]
        if attendee.name:
            params.insert(0, f"CN={attendee.name}")
        attendee_lines.append(f"ATTENDEE;{';'.join(params)}:mailto:{email}")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gmail-smtp-mcp//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{event.summary}",
        f"DTSTAMP:{_format_ics_datetime(dtstamp)}",
        f"DTSTART:{_format_ics_datetime(start)}",
        f"DTEND:{_format_ics_datetime(end)}",
        f"ORGANIZER;EMAIL={organizer}:mailto:{organizer}",
    ]

    if event.location:
        lines.append(f"LOCATION:{event.location}")
    if event.description:
        description = event.description.replace("\n", "\\n")
        lines.append(f"DESCRIPTION:{description}")

    lines.extend(attendee_lines)

    reminder_minutes = event.reminder_minutes if event.reminder_minutes is not None else 15
    if reminder_minutes >= 0:
        lines.extend(
            [
                "BEGIN:VALARM",
                f"TRIGGER:-PT{int(reminder_minutes)}M",
                "ACTION:DISPLAY",
                "DESCRIPTION:Reminder",
                "END:VALARM",
            ]
        )

    lines.extend(["END:VEVENT", "END:VCALENDAR"])

    ics_payload = "\r\n".join(lines) + "\r\n"
    filename = f"{uid}.ics"
    return filename, ics_payload.encode("utf-8")


def _attach_files(message: EmailMessage, attachment_paths: List[str]) -> List[str]:
    filenames: List[str] = []
    for raw_path in attachment_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
        filenames.append(path.name)
    return filenames


def _attach_inline_images(
    message: EmailMessage,
    inline_specs: List[InlineImageSpec],
) -> List[InlineImageResult]:
    if not inline_specs:
        return []

    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        raise ValueError("Inline images require an HTML body or HTML template.")

    inline_results: List[InlineImageResult] = []
    for spec in inline_specs:
        path = Path(spec.path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Inline image not found: {path}")

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        cid_value = spec.cid or make_msgid(domain="gmail-smtp-mcp")[1:-1]
        html_part.add_related(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            cid=f"<{cid_value}>",
            filename=path.name,
        )
        inline_results.append(InlineImageResult(cid=cid_value, filename=path.name))

    return inline_results


def _build_message(
    *,
    sender: str,
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    html_body: Optional[str] = None,
    body_template: Optional[str] = None,
    template_variables: Optional[Dict[str, str]] = None,
    inline_images: Optional[List[InlineImageSpec]] = None,
    calendar_event: Optional[CalendarEventInput] = None,
    signature_template: Optional[str] = None,
    signature_variables: Optional[Dict[str, str]] = None,
) -> Tuple[
    EmailMessage,
    List[str],
    List[InlineImageResult],
    Optional[str],
    Optional[str],
    Optional[str],
]:
    attachments = attachments or []
    cc = cc or []
    bcc = bcc or []
    template_variables = template_variables or {}
    signature_variables = signature_variables or template_variables

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject

    template_used: Optional[str] = None
    plain_body = body
    html_content = html_body

    if body_template:
        template_used = body_template
        templated_text, templated_html = _render_template_pair(body_template, template_variables)
        if templated_text:
            plain_body = templated_text
        if templated_html:
            html_content = templated_html
    else:
        plain_body = _safe_template(plain_body, template_variables)
        if html_content:
            html_content = _safe_template(html_content, template_variables)

    if not plain_body:
        raise ValueError("Provide a plain-text body or a template that renders plain text.")

    signature_used: Optional[str] = None
    signature_text: Optional[str] = None
    signature_html: Optional[str] = None
    if signature_template:
        signature_used = signature_template
        signature_text, signature_html = _render_signature_pair(
            signature_template,
            signature_variables or {},
        )
    if signature_text:
        separator = "\n\n" if plain_body.strip() else ""
        plain_body = plain_body.rstrip() + separator + signature_text

    message.set_content(plain_body)
    if html_content:
        message.add_alternative(html_content, subtype="html")

    if signature_html:
        html_part = message.get_body(preferencelist=("html",))
        if html_part is None:
            message.add_alternative(signature_html, subtype="html")
        else:
            html_part.set_content(html_part.get_content().rstrip() + "<br><br>" + signature_html, subtype="html")

    attachment_names = _attach_files(message, attachments)
    inline_results = _attach_inline_images(message, inline_images or [])

    calendar_filename: Optional[str] = None
    if calendar_event:
        calendar_filename, payload = _build_calendar_invite(
            calendar_event,
            sender,
            recipients=list(dict.fromkeys(to + cc)),
        )
        message.add_attachment(
            payload,
            maintype="text",
            subtype="calendar",
            filename=calendar_filename,
            params={"method": "REQUEST", "name": calendar_filename},
        )
        attachment_names.append(calendar_filename)

    return (
        message,
        attachment_names,
        inline_results,
        template_used,
        calendar_filename,
        signature_used,
    )


def _format_smtp_response(response: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(response, tuple) or len(response) != 2:
        return None
    code, message = response
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="ignore")
    return {"code": int(code), "message": message}


def _send_email_sync(
    *,
    username: str,
    password: str,
    smtp_server: str,
    port: int,
    message: EmailMessage,
    recipients: List[str],
    request_diagnostics: bool,
) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
    if port == 465:
        session_factory = smtplib.SMTP_SSL
        connect_kwargs: Dict[str, Any] = {}
    else:
        session_factory = smtplib.SMTP
        connect_kwargs = {"port": port}

    diagnostics: Optional[Dict[str, Any]] = None

    with session_factory(smtp_server, **connect_kwargs) as smtp:
        starttls_response = None
        if port != 465:
            starttls_response = smtp.starttls()
        smtp.login(username, password)
        refused = smtp.send_message(message, from_addr=message["From"], to_addrs=recipients)

        if request_diagnostics:
            diagnostics = {
                "server": smtp_server,
                "port": port,
                "esmtp_features": smtp.esmtp_features,
                "noop": _format_smtp_response(smtp.noop()),
            }
            if starttls_response:
                diagnostics["starttls"] = _format_smtp_response(starttls_response)

        return refused, diagnostics


def _decode_mime_words(value: Optional[str]) -> str:
    if not value:
        return ""
    decoded_parts = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def _quote_imap_text(value: str) -> str:
    escaped = value.replace('"', r"\"")
    return f'"{escaped}"'


def _extract_first_bytes(sequence: Iterable[Any]) -> bytes:
    for item in sequence or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return b""


def _imap_fetch_messages(criteria: List[str], limit: int) -> List[EmailPreview]:
    username, password, *_ = _collect_credentials()
    host, port = _collect_imap_settings()

    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(username, password)
        client.select("INBOX")

        typ, data = client.uid("SEARCH", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        if not uids:
            return []

        limit = max(1, limit)
        uids = uids[-limit:]

        previews: List[EmailPreview] = []
        for uid in reversed(uids):
            header_typ, header_data = client.uid("FETCH", uid, "(RFC822.HEADER)")
            if header_typ != "OK":
                continue
            header_bytes = _extract_first_bytes(header_data)
            header_msg = message_from_bytes(header_bytes)

            subject = _decode_mime_words(header_msg.get("Subject"))
            sender = _decode_mime_words(header_msg.get("From"))
            raw_date = header_msg.get("Date")
            iso_date = raw_date or ""
            if raw_date:
                try:
                    parsed_date = parsedate_to_datetime(raw_date)
                    iso_date = parsed_date.astimezone(timezone.utc).isoformat()
                except Exception:
                    iso_date = raw_date

            snippet = None
            snippet_typ, snippet_data = client.uid("FETCH", uid, "(BODY.PEEK[TEXT]<0.200>)")
            if snippet_typ == "OK":
                snippet_bytes = _extract_first_bytes(snippet_data)
                snippet = snippet_bytes.decode("utf-8", errors="replace").strip()

            previews.append(
                EmailPreview(
                    uid=uid.decode(),
                    subject=subject,
                    sender=sender,
                    date=iso_date,
                    snippet=snippet or None,
                )
            )

        return previews


@server.tool(
    name="gmail_queue_email_with_attachments",
    description="Queue an email payload for the asynchronous spooler to deliver.",
)
def gmail_queue_email_with_attachments(
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    html_body: Optional[str] = None,
    body_template: Optional[str] = None,
    template_variables: Optional[Dict[str, str]] = None,
    inline_images: Optional[List[InlineImageSpec]] = None,
    calendar_event: Optional[CalendarEventInput] = None,
    signature_template: Optional[str] = None,
    signature_variables: Optional[Dict[str, str]] = None,
    diagnostics: bool = False,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "to": to,
        "subject": subject,
        "body": body,
    }
    if attachments:
        payload["attachments"] = attachments
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if html_body:
        payload["html_body"] = html_body
    if body_template:
        payload["body_template"] = body_template
    if template_variables:
        payload["template_variables"] = template_variables
    if inline_images:
        payload["inline_images"] = [spec.model_dump() for spec in inline_images]
    if calendar_event:
        payload["calendar_event"] = calendar_event.model_dump()
    if signature_template:
        payload["signature_template"] = signature_template
    if signature_variables:
        payload["signature_variables"] = signature_variables
    if diagnostics:
        payload["diagnostics"] = diagnostics

    metadata = {"source": "mcp-tool"}
    if note:
        metadata["note"] = note

    queued_path = _queue_payload_file(payload, metadata=metadata)
    pending = len(list(SPOOL_PENDING_DIR.glob("*.json")))
    return {"queued_path": queued_path, "pending_count": pending}


@server.tool(
    name="gmail_send_email_with_attachments",
    description=(
        "Send email via Gmail SMTP with optional CC, BCC, file attachments, HTML bodies, "
        "templates, signatures, inline images, calendar invites, and diagnostics."
    ),
)
async def gmail_send_email_with_attachments(
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    html_body: Optional[str] = None,
    body_template: Optional[str] = None,
    template_variables: Optional[Dict[str, str]] = None,
    inline_images: Optional[List[InlineImageSpec]] = None,
    calendar_event: Optional[CalendarEventInput] = None,
    signature_template: Optional[str] = None,
    signature_variables: Optional[Dict[str, str]] = None,
    diagnostics: bool = False,
) -> EmailSendResult:
    if not to:
        raise ValueError("At least one recipient is required in 'to'.")

    username, password, smtp_server, port, sender = _collect_credentials()

    (
        message,
        attachment_names,
        inline_results,
        template_used,
        calendar_filename,
        signature_used,
    ) = _build_message(
        sender=sender,
        to=to,
        subject=subject,
        body=body,
        attachments=attachments,
        cc=cc,
        bcc=bcc,
        html_body=html_body,
        body_template=body_template,
        template_variables=template_variables,
        inline_images=inline_images,
        calendar_event=calendar_event,
        signature_template=signature_template,
        signature_variables=signature_variables,
    )

    recipient_list = list(dict.fromkeys(to + (cc or []) + (bcc or [])))

    refused, smtp_diag = await asyncio.to_thread(
        _send_email_sync,
        username=username,
        password=password,
        smtp_server=smtp_server,
        port=port,
        message=message,
        recipients=recipient_list,
        request_diagnostics=diagnostics,
    )

    refused = {addr: reason for addr, reason in refused.items()}
    accepted = [addr for addr in recipient_list if addr not in refused]

    return EmailSendResult(
        accepted=accepted,
        refused=refused,
        attachments=attachment_names,
        inline_images=inline_results,
        template_used=template_used,
        calendar_invite=calendar_filename,
        smtp_diagnostics=smtp_diag,
        signature_used=signature_used,
    )


@server.tool(
    name="gmail_list_unread_messages",
    description="List the most recent unread messages in the Gmail inbox (read-only).",
)
async def gmail_list_unread_messages(limit: int = 10) -> EmailPreviewList:
    previews = await asyncio.to_thread(_imap_fetch_messages, ["UNSEEN"], limit)
    return EmailPreviewList(messages=previews, criteria="UNSEEN")


@server.tool(
    name="gmail_search_subject",
    description="Search Gmail inbox for messages matching a subject string. Returns newest matches first.",
)
async def gmail_search_subject(subject: str, limit: int = 10) -> EmailPreviewList:
    if not subject:
        raise ValueError("Subject search term cannot be empty.")
    criteria = ["CHARSET", "UTF-8", "SUBJECT", _quote_imap_text(subject)]
    previews = await asyncio.to_thread(_imap_fetch_messages, criteria, limit)
    return EmailPreviewList(messages=previews, criteria=f"SUBJECT {subject}")


@server.tool(
    name="gmail_fetch_latest_from_sender",
    description="Fetch the latest message from a given sender address (read-only).",
)
async def gmail_fetch_latest_from_sender(sender: str) -> EmailPreviewResult:
    if not sender:
        raise ValueError("Sender email is required.")
    criteria = ["CHARSET", "UTF-8", "FROM", _quote_imap_text(sender)]
    previews = await asyncio.to_thread(_imap_fetch_messages, criteria, 1)
    return EmailPreviewResult(message=previews[0] if previews else None)


if __name__ == "__main__":
    server.run()
