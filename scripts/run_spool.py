"""Simple loop to watch the spool and send emails when they appear."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import spool_utils  # noqa: E402
from scripts.payload_utils import execute_payload_sync  # noqa: E402


def process_pending(limit: int | None = None, dry_run: bool = False) -> int:
    count = 0
    for path in spool_utils.iter_pending(limit=limit):
        entry = spool_utils.load_entry(path)
        payload = entry["payload"]
        preset = entry.get("metadata", {}).get("preset")
        print(f"Delivering {path.name} preset={preset!r} to {payload['to']} ...", flush=True)
        if dry_run:
            print(" [dry run] skipped")
            count += 1
            continue
        try:
            result = execute_payload_sync(payload)
        except Exception as exc:  # noqa: BLE001
            print(f" Failed: {exc}")
            spool_utils.move_to_failed(path, error=str(exc))
        else:
            spool_utils.move_to_sent(path, result=result)
            print(" Delivered successfully.")
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Run spool delivery loop.")
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds to sleep between scans (default: 60).")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending items to process per iteration.")
    parser.add_argument("--dry-run", action="store_true", help="Only report items; do not send.")
    parser.add_argument("--once", action="store_true", help="Process once and exit.")
    args = parser.parse_args()

    while True:
        processed = process_pending(limit=args.limit, dry_run=args.dry_run)
        if args.once:
            break
        if processed == 0:
            print("No pending spool entries. Sleeping...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
