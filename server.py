#!/usr/bin/env python3
"""Minimal Gmail MCP server that can send attachments via SMTP."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

ENV_FILE_DEFAULT = Path.home() / ".config" / "mcp" / "gmail_smtp.env"

server = FastMCP(name="gmail-smtp-mcp", instructions="Send email through Gmail including attachments.")


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


def _load_env_file(env_path: Path) -> None:
    """Populate missing environment variables from the given dotenv-style file."""

    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
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
    server = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
    port = int(os.environ.get("GMAIL_SMTP_PORT", "465"))
    from_address = os.environ.get("GMAIL_FROM_ADDRESS", username or "")

    if not username:
        raise ValueError("Set GMAIL_SMTP_USERNAME in the environment or env file.")
    if not password:
        raise ValueError("Set GMAIL_SMTP_APP_PASSWORD in the environment or env file.")
    if not from_address:
        raise ValueError("Set GMAIL_FROM_ADDRESS when username is unavailable.")

    return username, password, server, port, from_address


def _build_message(
    *,
    sender: str,
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> Tuple[EmailMessage, List[Path]]:
    """Construct an EmailMessage and resolve attachment paths."""

    attachments = attachments or []
    cc = cc or []
    bcc = bcc or []

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject
    message.set_content(body)

    resolved_paths: List[Path] = []
    for raw_path in attachments:
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
        resolved_paths.append(path)

    return message, resolved_paths


def _send_email_sync(
    *,
    username: str,
    password: str,
    smtp_server: str,
    port: int,
    message: EmailMessage,
    recipients: List[str],
) -> Dict[str, str]:
    """Send the prepared message synchronously using smtplib."""

    if port == 465:
        session_factory = smtplib.SMTP_SSL
        connect_kwargs = {}
    else:
        session_factory = smtplib.SMTP
        connect_kwargs = {"port": port}

    with session_factory(smtp_server, **connect_kwargs) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(username, password)
        return smtp.send_message(message, from_addr=message["From"], to_addrs=recipients)


@server.tool(
    name="gmail_send_email_with_attachments",
    description="Send email via Gmail SMTP with optional CC, BCC, and file attachments.",
)
async def gmail_send_email_with_attachments(
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> EmailSendResult:
    if not to:
        raise ValueError("At least one recipient is required in 'to'.")

    username, password, smtp_server, port, sender = _collect_credentials()

    message, resolved_paths = _build_message(
        sender=sender,
        to=to,
        subject=subject,
        body=body,
        attachments=attachments,
        cc=cc,
        bcc=bcc,
    )

    all_recipients = to + (cc or []) + (bcc or [])

    refused: Dict[str, str] = await asyncio.to_thread(
        _send_email_sync,
        username=username,
        password=password,
        smtp_server=smtp_server,
        port=port,
        message=message,
        recipients=all_recipients,
    )

    refused = {addr: reason for addr, reason in refused.items()}
    accepted = [addr for addr in all_recipients if addr not in refused]

    return EmailSendResult(
        accepted=accepted,
        refused=refused,
        attachments=[path.name for path in resolved_paths],
    )


if __name__ == "__main__":
    server.run()
