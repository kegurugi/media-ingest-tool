"""Project history: project_history.sqlite on the Mac, next to project.json.

Each drive's master log only knows that drive. The project history records
every offload across every drive set, so running totals carry on when a season
moves from one drive set to the next (e.g. DRV01A/B to DRV02A/B). A summary of it
is copied into each drive's master log, so later drives carry the earlier
drives' totals even when those drives are on a shelf.

It also keeps reel numbers unique across drive sets: a camera that shot A01 to
A03 on DRV01 continues at A04 on DRV02.

Writes use the same crash-safe copy-and-replace as the drive logs. If the file
is lost, `mit history rebuild` recreates it from the card records on the drives.
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from .errors import NamingError
from .masterlog import find_records
from .naming import CardName
from .sqlitesafe import LogError, connect_readonly, copy_into, is_healthy, locked, replace_db, set_aside_if_damaged

HISTORY_NAME = "project_history.sqlite"
LOCK_NAME = ".project_history.lock"
LOCK_TIMEOUT = 60.0
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE offloads (
    id INTEGER PRIMARY KEY,
    offload_uid TEXT NOT NULL UNIQUE,
    card TEXT NOT NULL,
    show_code TEXT,
    shoot_date TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    drive_set TEXT NOT NULL,      -- destination volumes, sorted, e.g. "DRV01A & DRV01B"
    result TEXT NOT NULL,
    clip_count INTEGER, total_bytes INTEGER,
    offload_started TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX offloads_card ON offloads (card);
"""

PathLike = Union[str, Path]


def history_path(config_path: PathLike) -> Path:
    """The history lives beside the project's project.json."""
    return Path(config_path).expanduser().resolve().parent / HISTORY_NAME


def drive_set(record: Dict[str, Any]) -> str:
    return " & ".join(sorted(d.get("volume", "") for d in record.get("destinations", [])))


def add_record(path: PathLike, record: Dict[str, Any], lock_timeout: float = LOCK_TIMEOUT) -> bool:
    """Add one card record to the project history. Returns False if it was already there."""
    return _write(Path(path), [record], lock_timeout)[0] == 1


def rebuild_from_drives(path: PathLike, drives: Iterable[PathLike], lock_timeout: float = LOCK_TIMEOUT
                        ) -> Tuple[int, Optional[Path]]:
    """Add the card records found on the given drives that the history is missing.

    Nothing already in a healthy history is removed. A damaged history is
    renamed aside (not deleted) and rebuilt. Returns (added, set-aside path).
    """
    records = [r for d in drives for r in find_records(Path(d))]
    records.sort(key=lambda r: r.get("offload_started", ""))
    return _write(Path(path), records, lock_timeout, rebuilding=True)


def summary(path: PathLike) -> List[Dict[str, Any]]:
    """Per drive set and shoot date: cards, clips, bytes and failed cards (current attempts only)."""
    path = Path(path)
    if not path.exists():
        return []
    with contextlib.closing(connect_readonly(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT drive_set, shoot_date, COUNT(*) AS cards, SUM(clip_count) AS clips,
                      SUM(total_bytes) AS bytes, SUM(CASE WHEN result = 'PASS' THEN 0 ELSE 1 END) AS failed
               FROM offloads WHERE superseded = 0
               GROUP BY drive_set, shoot_date ORDER BY shoot_date, drive_set"""
        ).fetchall()
    return [dict(r) for r in rows]


def used_reels(path: PathLike, show: str, shoot_date: date, cam: str) -> Set[int]:
    """Reels already used for show + date + camera on any drive set."""
    path = Path(path)
    if not path.exists():
        return set()
    with contextlib.closing(connect_readonly(path)) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT card FROM offloads WHERE shoot_date = ?", (shoot_date.isoformat(),))]
    reels = set()
    for name in names:
        try:
            card = CardName.parse(name)
        except NamingError:
            continue
        if card.show == show.upper() and card.cam == cam.upper():
            reels.add(card.reel)
    return reels


def where_used(path: PathLike, card: str) -> List[str]:
    """Drive sets a card name was offloaded to."""
    path = Path(path)
    if not path.exists():
        return []
    with contextlib.closing(connect_readonly(path)) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT drive_set FROM offloads WHERE card = ?", (card,))]


# --- internals --------------------------------------------------------------

def _write(path: Path, records: List[Dict[str, Any]], lock_timeout: float, rebuilding: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.parent / LOCK_NAME, lock_timeout, f"The project history {path} is busy"):
        set_aside = set_aside_if_damaged(path) if rebuilding else None
        if not rebuilding and path.exists() and not is_healthy(path):
            raise LogError(f"{path} is damaged. Run `mit history rebuild` with the drives connected.")
        exists = path.exists()

        def build(conn: sqlite3.Connection) -> int:
            if exists:
                copy_into(path, conn)
            else:
                conn.executescript(_SCHEMA)
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("created", datetime.now().isoformat(timespec="seconds")),
                ])
            return sum(_insert(conn, r) for r in records)

        added = replace_db(path, build)
    return added, set_aside


def _insert(conn: sqlite3.Connection, record: Dict[str, Any]) -> int:
    card = record["card"]
    started = record.get("offload_started", "")
    uid = record.get("offload_id") or f"{card}@{started}"
    if conn.execute("SELECT 1 FROM offloads WHERE offload_uid = ?", (uid,)).fetchone():
        return 0
    camera = record.get("camera", {})
    conn.execute(
        """INSERT INTO offloads (offload_uid, card, show_code, shoot_date, camera_id, drive_set, result,
               clip_count, total_bytes, offload_started) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (uid, card, record.get("show_code", card[:3]), record.get("shoot_date", ""),
         camera.get("id") or card[9:-2], drive_set(record), record.get("result", "FAIL"),
         len(record.get("clips", [])), record.get("total_bytes"), started),
    )
    conn.execute(
        """UPDATE offloads SET superseded = CASE WHEN id =
               (SELECT id FROM offloads WHERE card = ? ORDER BY offload_started DESC, id DESC LIMIT 1)
               THEN 0 ELSE 1 END
           WHERE card = ?""",
        (card, card),
    )
    return 1
