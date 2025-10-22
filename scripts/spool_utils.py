"""Utilities for queuing and delivering email payloads via a local spool."""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.payload_utils import PROJECT_ROOT as _PROJECT_ROOT  # noqa: E402

PROJECT_ROOT = _PROJECT_ROOT

SPOOL_ROOT = PROJECT_ROOT / "spool"
PENDING_DIR = SPOOL_ROOT / "pending"
SENT_DIR = SPOOL_ROOT / "sent"
FAILED_DIR = SPOOL_ROOT / "failed"


def ensure_spool_dirs() -> None:
    for directory in (PENDING_DIR, SENT_DIR, FAILED_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def queue_payload(payload: Dict[str, Any], metadata: Dict[str, Any] | None = None) -> Path:
    """Write the payload to the pending spool directory and return the path."""

    ensure_spool_dirs()
    entry = {
        "schema_version": 1,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata or {},
        "payload": payload,
    }
    file_name = f"{int(time.time())}-{uuid.uuid4().hex}.json"
    pending_path = PENDING_DIR / file_name
    pending_path.write_text(json.dumps(entry, indent=2))
    return pending_path


def iter_pending(limit: int | None = None) -> Iterable[Path]:
    """Yield pending spool file paths, oldest first."""

    ensure_spool_dirs()
    files = sorted(PENDING_DIR.glob("*.json"))
    if limit is not None:
        files = files[:limit]
    return files


def load_entry(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def move_to_sent(path: Path, result: Dict[str, Any]) -> Path:
    ensure_spool_dirs()
    entry = load_entry(path)
    entry["sent_at"] = datetime.now(timezone.utc).isoformat()
    entry["result"] = result
    target = SENT_DIR / path.name
    target.write_text(json.dumps(entry, indent=2))
    path.unlink()
    return target


def move_to_failed(path: Path, error: str, traceback_text: str | None = None) -> Path:
    ensure_spool_dirs()
    entry = load_entry(path)
    entry["failed_at"] = datetime.now(timezone.utc).isoformat()
    entry["error"] = error
    if traceback_text:
        entry["traceback"] = traceback_text
    target = FAILED_DIR / path.name
    target.write_text(json.dumps(entry, indent=2))
    path.unlink()
    return target


def discard(path: Path) -> None:
    if path.exists():
        path.unlink()


def reset_spool() -> None:
    """Utility to clear the spool directories (for testing)."""

    if SPOOL_ROOT.exists():
        shutil.rmtree(SPOOL_ROOT)
