"""Send or queue a preset Gmail MCP payload."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any, Dict

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.payload_utils import ensure_sample_png, execute_payload_sync  # noqa: E402
from scripts import spool_utils  # noqa: E402


def load_module_payload(module_name: str) -> Dict[str, Any]:
    module = importlib.import_module(f"scripts.payloads.{module_name}")
    payload: Dict[str, Any] = module.build_payload()
    return payload


def send_payload(payload: Dict[str, Any], module_name: str | None = None) -> Dict[str, Any]:
    print(f"Sending payload{f' {module_name!r}' if module_name else ''} to {payload['to']} ...", flush=True)
    result = execute_payload_sync(payload)
    print("SMTP result:")
    print(result)
    return result


def queue_payload(payload: Dict[str, Any], module_name: str | None = None) -> None:
    metadata = {"preset": module_name} if module_name else {}
    path = spool_utils.queue_payload(payload, metadata=metadata)
    print(f"Queued payload{f' {module_name!r}' if module_name else ''} at {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send email using preset payload.")
    parser.add_argument(
        "preset",
        help="Payload module name under scripts.payloads (e.g. 'modern_launch').",
    )
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="Do not send immediately; write the payload to the spool.",
    )
    args = parser.parse_args()

    payload = load_module_payload(args.preset)

    # Ensure inline assets that rely on the sample PNG exist.
    for item in payload.get("inline_images", []) or []:
        if item.get("ensure_sample_png"):
            ensure_sample_png(Path(item["path"]))

    if args.queue_only:
        queue_payload(payload, module_name=args.preset)
    else:
        send_payload(payload, module_name=args.preset)


if __name__ == "__main__":
    main()
