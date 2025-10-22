"""Send an email using a preset payload module."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from server import (  # noqa: E402
    CalendarEventInput,
    InlineImageSpec,
    gmail_send_email_with_attachments,
)


SAMPLE_PNG_HEX = (
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000A49"
    "444154789C6360000002000100ABFE28D90000000049454E44AE426082"
)


def _ensure_sample_png(path: Path) -> None:
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


def _build_calendar_event(data: Optional[Dict[str, Any]]) -> Optional[CalendarEventInput]:
    if not data:
        return None
    calendar_fields = data.copy()
    for key in ("start", "end", "dtstamp"):
        if key in calendar_fields and calendar_fields[key] is not None:
            calendar_fields[key] = _to_datetime(calendar_fields[key])
    return CalendarEventInput(**calendar_fields)


def _build_inline_images(items: Optional[List[Dict[str, Any]]]) -> List[InlineImageSpec]:
    result: List[InlineImageSpec] = []
    for item in items or []:
        path = Path(item["path"]).expanduser()
        if item.get("ensure_sample_png"):
            _ensure_sample_png(path)
        result.append(InlineImageSpec(path=str(path), cid=item["cid"]))
    return result


async def send_from_module(module_name: str) -> None:
    module = importlib.import_module(f"scripts.payloads.{module_name}")
    payload = module.build_payload()

    calendar_event = _build_calendar_event(payload.get("calendar_event"))
    inline_images = _build_inline_images(payload.get("inline_images"))

    attachments = [str(Path(p).expanduser()) for p in payload.get("attachments", [])]

    print(f"Sending payload '{module_name}' to {payload['to']} ...", flush=True)
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

    print("SMTP result:")
    print(result.model_dump())


def main() -> None:
    parser = argparse.ArgumentParser(description="Send email using preset payload.")
    parser.add_argument(
        "preset",
        help="Payload module name under scripts.payloads (e.g. 'modern_launch').",
    )
    args = parser.parse_args()

    asyncio.run(send_from_module(args.preset))


if __name__ == "__main__":
    main()
