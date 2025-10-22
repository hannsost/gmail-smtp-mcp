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

server = FastMCP(
    name="gmail-smtp-mcp",
    instructions=(
        "Send Gmail messages with attachments, inline assets, html/templates, calendar invites, "
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
    if html_path.exists():
        rendered_html = _safe_template(html_path.read_text(encoding="utf-8"), variables)

    if rendered_text is None and rendered_html is None:
        raise FileNotFoundError(
            f"No template found for '{template_name}'. Expected {text_path.name} or {html_path.name}."
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
    """Attach files to the message and return the list of filenames added."""

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
    """Attach inline images to the HTML body part and return CID metadata."""

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
) -> Tuple[EmailMessage, List[str], List[InlineImageResult], Optional[str], Optional[str]]:
    """Construct an EmailMessage with advanced options."""

    attachments = attachments or []
    cc = cc or []
    bcc = bcc or []
    template_variables = template_variables or {}

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

    message.set_content(plain_body)
    if html_content:
        message.add_alternative(html_content, subtype="html")

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

    return message, attachment_names, inline_results, template_used or None, calendar_filename


def _format_smtp_response(response: Any) -> Optional[Dict[str, Any]]:
    """Convert SMTP response tuples to structured dictionaries."""

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
    """Send the prepared message synchronously using smtplib."""

    if port == 465:
        session_factory = smtplib.SMTP_SSL
        connect_kwargs = {}
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
    """Decode MIME-encoded words in headers."""

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
    """Quote a string for IMAP search."""

    escaped = value.replace('"', r'\"')
    return f'"{escaped}"'


def _extract_first_bytes(sequence: List[Any]) -> bytes:
    """Pull the first bytes payload out of an imaplib fetch response."""

    for item in sequence or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return b""


def _imap_fetch_messages(criteria: List[str], limit: int) -> List[EmailPreview]:
    """Common IMAP search helper that returns message previews."""

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
    name="gmail_send_email_with_attachments",
    description=(
        "Send email via Gmail SMTP with optional CC, BCC, file attachments, HTML bodies, "
        "templates, inline images, calendar invites, and diagnostics."
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
