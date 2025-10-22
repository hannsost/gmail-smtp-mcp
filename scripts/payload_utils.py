"""Helpers for executing Gmail MCP payloads and handling inline assets."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from server import (
    CalendarEventInput,
    InlineImageSpec,
    gmail_send_email_with_attachments,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_PNG_HEX = (
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000A49"
    "444154789C6360000002000100ABFE28D90000000049454E44AE426082"
)


def ensure_sample_png(path: Path) -> None:
    """Create a minimal PNG file at the provided path if missing."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(bytes.fromhex(SAMPLE_PNG_HEX))


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(f"Unsupported datetime value: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_calendar_event(data: Optional[Dict[str, Any]]) -> Optional[CalendarEventInput]:
    if not data:
        return None
    calendar_fields = data.copy()
    for key in ("start", "end", "dtstamp"):
        if key in calendar_fields and calendar_fields[key] is not None:
            calendar_fields[key] = _to_datetime(calendar_fields[key])
    return CalendarEventInput(**calendar_fields)


def build_inline_images(items: Optional[List[Dict[str, Any]]]) -> List[InlineImageSpec]:
    result: List[InlineImageSpec] = []
    for item in items or []:
        path = Path(item["path"]).expanduser()
        if item.get("ensure_sample_png"):
            ensure_sample_png(path)
        result.append(
            InlineImageSpec(
                path=str(path),
                cid=item.get("cid"),
            )
        )
    return result


async def execute_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run gmail_send_email_with_attachments for a dictionary payload."""

    calendar_event = build_calendar_event(payload.get("calendar_event"))
    inline_images = build_inline_images(payload.get("inline_images"))
    attachments = [str(Path(p).expanduser()) for p in payload.get("attachments", [])]

    result = await gmail_send_email_with_attachments(
        to=payload["to"],
        subject=payload["subject"],
        body=payload.get("body", ""),
        attachments=attachments,
        cc=payload.get("cc"),
        bcc=payload.get("bcc"),
        html_body=payload.get("html_body"),
        body_template=payload.get("body_template"),
        template_variables=payload.get("template_variables"),
        inline_images=inline_images,
        calendar_event=calendar_event,
        signature_template=payload.get("signature_template"),
        signature_variables=payload.get("signature_variables"),
        diagnostics=payload.get("diagnostics", False),
    )
    return result.model_dump()


def execute_payload_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous wrapper for execute_payload."""

    return asyncio.run(execute_payload(payload))
