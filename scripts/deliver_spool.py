"""Deliver queued email payloads from the spool directory."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.payload_utils import execute_payload_sync  # noqa: E402
from scripts import spool_utils  # noqa: E402


def deliver_file(path: Path, dry_run: bool = False) -> None:
    entry = spool_utils.load_entry(path)
    payload = entry["payload"]
    preset = entry.get("metadata", {}).get("preset")

    if dry_run:
        print(f"[DRY RUN] Would deliver {path.name} preset={preset!r} to {payload['to']}")
        return

    print(f"Delivering {path.name} preset={preset!r} to {payload['to']} ...", flush=True)
    try:
        result = execute_payload_sync(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}")
        spool_utils.move_to_failed(path, error=str(exc), traceback_text=traceback.format_exc())
    else:
        spool_utils.move_to_sent(path, result=result)
        print(" Delivered successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver queued spool entries.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of queued files to process.")
    parser.add_argument("--dry-run", action="store_true", help="Only report which entries would be sent.")
    args = parser.parse_args()

    count = 0
    for path in spool_utils.iter_pending(limit=args.limit):
        deliver_file(path, dry_run=args.dry_run)
        count += 1

    if count == 0:
        print("No pending spool entries.")


if __name__ == "__main__":
    main()
