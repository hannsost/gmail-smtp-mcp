#!/usr/bin/env python3
"""Enhanced Gmail MCP server with SMTP, inline assets, calendar invites, templates, and IMAP tools."""

from __future__ import annotations

import asyncio
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
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

ENV_FILE_DEFAULT = Path.home() / ".config" / "mcp" / "gmail_smtp.env"
TEMPLATE_DIR = Path(__file__).with_name("templates")
SIGNATURE_DIR = Path(__file__).with_name("signatures")

server = FastMCP(
    name="gmail-smtp-mcp",
    instructions=(
        "Send Gmail messages with attachments, inline assets, templates/signatures, calendar invites, "
        "and inspect inbox snapshots."
    ),
)


class InlineImageSpec(BaseModel):
    """Input specification for inline (CID) images embedded in HTML mail."""

    path: str = Field(..., description="Path to the image file to embed inline.")
    cid: Optional[str] = Field(
        default=None,
        description="Optional Content-ID to reference inside HTML. Generated when omitted.",
    )


class InlineImageResult(BaseModel):
    """Metadata about an inline image attached to the outgoing email."""

    cid: str = Field(..., description="Content-ID assigned to the inline image.")
    filename: str = Field(..., description="Filename used in the MIME part.")


class CalendarAttendee(BaseModel):
    """Attendee definition for calendar invites."""

    email: str = Field(..., description="Email address of the attendee.")
    name: Optional[str] = Field(default=None, description="Attendee display name.")


class CalendarEventInput(BaseModel):
    """Structured data representing a meeting invite to embed as an .ics attachment."""

    summary: str = Field(..., description="Short summary/title for the event.")
    start: datetime = Field(..., description="Event start (ISO-8601).")
    end: datetime = Field(..., description="Event end (ISO-8601).")
    description: Optional[str] = Field(default=None, description="Longer event description.")
    location: Optional[str] = Field(default=None, description="Event location or meeting link.")
    organizer_email: Optional[str] = Field(
        default=None,
        description="Override the organizer email; defaults to the authenticated sender.",
    )
    attendees: List[CalendarAttendee] = Field(
        default_factory=list,
        description="Optional explicit attendee list. Defaults to to+cc recipients.",
    )
    timezone: Optional[str] = Field(
        default=None,
        description="IANA timezone to apply when datetimes are naive (e.g. 'Europe/Berlin').",
    )
    reminder_minutes: Optional[int] = Field(
        default=15,
        description="Minutes before the event to trigger the default reminder alarm.",
    )
    uid: Optional[str] = Field(
        default=None,
        description="Explicit UID for the calendar invite. Auto-generated when omitted.",
    )


class EmailSendResult(BaseModel):
    """Structured response returned after attempting to send an email."""

    accepted: List[str] = Field(..., description="Recipients accepted by Gmail.")
    refused: Dict[str, str] = Field(
        default_factory=dict,
        description="Recipients refused by Gmail with diagnostic messages.",
    )
    attachments: List[str] = Field(
        default_factory=list,
        description="Attachment file names included in the message.",
    )
    inline_images: List[InlineImageResult] = Field(
        default_factory=list,
        description="Inline images embedded via Content-ID references.",
    )
    template_used: Optional[str] = Field(
        default=None,
        description="Template name applied to generate the email body.",
    )
    calendar_invite: Optional[str] = Field(
        default=None,
        description="Filename of the generated calendar invite attachment, when present.",
    )
    smtp_diagnostics: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional SMTP diagnostics (EHLO features, NOOP response, TLS).",
    )
    signature_used: Optional[str] = Field(
        default=None,
        description="Signature template applied to the outgoing message, when configured.",
    )


class EmailPreview(BaseModel):
    """Lightweight preview of an email message fetched via IMAP."""

    uid: str = Field(..., description="IMAP UID of the message.")
    subject: str = Field(..., description="Decoded subject line.")
    sender: str = Field(..., description="Decoded From header.")
    date: str = Field(..., description="Message date string (ISO-8601 when parsable).")
    snippet: Optional[str] = Field(
        default=None,
        description="First ~200 characters of the message body for quick context.",
    )


class EmailPreviewList(BaseModel):
    """Container for multiple email previews."""

    messages: List[EmailPreview] = Field(default_factory=list)
    criteria: str = Field(..., description="IMAP search criteria used to fetch the messages.")


class EmailPreviewResult(BaseModel):
    """Wrapper for returning a single preview (or null)."""

    message: Optional[EmailPreview] = Field(
        default=None, description="Most recent message matching the request."
    )


def _load_env_file(env_path: Path) -> None:
    """Populate missing environment variables from the given dotenv-style file."""

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
    """Read SMTP configuration from env vars (loading dotenv first if needed)."""

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
    """Read IMAP host/port settings, defaulting to Gmail values."""

    env_path = Path(os.environ.get("GMAIL_SMTP_ENV_FILE", ENV_FILE_DEFAULT))
    _load_env_file(env_path)
    host = os.environ.get("GMAIL_IMAP_SERVER", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", "993"))
    return host, port


def _safe_template(text: str, variables: Dict[str, str]) -> str:
    """Render a string.Template with safe substitution."""

    if not variables:
        return text
    return Template(text).safe_substitute(variables)


def _render_template_pair(
    template_name: str, variables: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Render plain-text and HTML templates when available."""

    text_path = TEMPLATE_DIR / f"{template_name}.txt"
    html_path = TEMPLATE_DIR / f"{template_name}.html"

    rendered_text = None
    rendered_html = None

    if text_path.exists():
        rendered_text = _safe_template(text_path.read_text(encoding="utf-8"), variables)
    if html_path exists():
        rendered_html = _safe_template(html_path.read_text(encoding="utf-8"), variables)

    if rendered_text is None and rendered_html is None:
        raise FileNotFoundError(
            f"No template found for '{template_name}'. Expected {text_path.name} or {html_path.name}."
        )

    return rendered_text, rendered_html


def _render_signature_pair(
    signature_name: str, variables: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Render plain-text and HTML signatures from the signatures directory."""

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


def _apply_timezone(dt: datetime, tz_name: Optional[str]) -> datetime:
    """Ensure datetimes are timezone-aware by applying the provided timezone when necessary."""

    if dt.tzinfo is None:
        zone = ZoneInfo(tz_name) if tz_name else timezone.utc
        dt = dt.replace(tzinfo=zone)
    return dt


def _format_ics_datetime(dt: datetime) -> str:
    """RFC5545-compliant UTC datetime format."""

    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_calendar_invite(
    event: CalendarEventInput,
    sender: str,
    recipients: List[str],
) -> Tuple[str, bytes]:
    """Create a basic RFC5545 calendar invite payload."""

    tz_name = event.timezone
    start = _apply_timezone(event.start, tz_name)
    end = _apply_timezone(event.end, tz_name)
    if end <= start:
        raise ValueError("Calendar event 'end' must be after 'start'.")

    dtstamp = datetime.now(timezone.utc)
    organizer = event.organizer_email or sender
    uid = event.uid or f"{uuid.uuid4()}@gmail-smtp-mcp"

    attendees = event.attendees or [
        CalendarAttendee(email=email.strip()) for email in recipients
    ]

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
*content truncated*