"""Per-card hash record: _REPORTS/<date>/<CARD>_hashes.json on every destination.

It holds every file's size, source xxHash64 and verification result per
destination. The job report (stage 4) and `mit verify` (stage 7) read it;
stage 5 moves this data into the SQLite master log as well.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .errors import DestinationExistsError
from .fileutil import atomic_write_text


def write_hash_record(path: Path, record: Dict[str, Any]) -> None:
    """Write the record atomically. Never replaces an existing record."""
    path = Path(path)
    if path.exists():
        raise DestinationExistsError(f"Hash record already exists: {path}")
    atomic_write_text(path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def read_hash_record(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
