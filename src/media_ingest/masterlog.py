"""Master log: _LOGS/media_log.sqlite on each drive, the source of truth for that drive.

Every write follows safety rule 7: copy the log to a temp file, change the
copy, check its integrity, flush it, then atomically replace the original. A
crash at any point leaves the previous log untouched. A lock file serialises
writers, so two offloads running at once can't lose each other's rows.

Each drive's log holds that drive's own verification status. Failed attempts
stay as history; when a card is offloaded again, earlier attempts are marked
superseded and left out of totals.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .sqlitesafe import LogError, connect_readonly, copy_into, is_healthy, locked, replace_db, set_aside_if_damaged
from .incomplete import INCOMPLETE_DIRNAME
from .layout import REPORTS_DIRNAME, logs_dir, master_log_path, require_mounted
from .timecode import end_timecode

SCHEMA_VERSION = 3
LOCK_NAME = ".media_log.lock"
LOCK_TIMEOUT = 60.0

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE offloads (
    id INTEGER PRIMARY KEY,
    offload_uid TEXT NOT NULL UNIQUE,  -- from the card record; identifies one offload attempt
    card TEXT NOT NULL,
    shoot_date TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    camera_model TEXT, camera_description TEXT, camera_shooter TEXT,
    media_type TEXT,
    reel TEXT NOT NULL,
    project_name TEXT, show_code TEXT,
    offload_started TEXT NOT NULL,
    offload_finished TEXT,
    operator TEXT,
    source_volume TEXT,
    destinations TEXT,            -- all volumes this card went to, e.g. "DRV01A & DRV01B"
    volume TEXT NOT NULL,         -- this drive
    result TEXT NOT NULL,         -- PASS/FAIL for the card overall
    drive_result TEXT NOT NULL,   -- PASS/FAIL on this drive
    file_count INTEGER, clip_count INTEGER, total_bytes INTEGER, total_duration REAL,
    elapsed_seconds REAL, transfer_speed REAL,
    notes TEXT, errors TEXT, card_path TEXT, tool_version TEXT,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    offload_id INTEGER NOT NULL REFERENCES offloads(id),
    path TEXT NOT NULL, size INTEGER, xxh64 TEXT,
    verification TEXT             -- on this drive
);
CREATE TABLE clips (
    id INTEGER PRIMARY KEY,
    offload_id INTEGER NOT NULL REFERENCES offloads(id),
    card TEXT NOT NULL, shoot_date TEXT NOT NULL, camera_id TEXT NOT NULL,
    offload_started TEXT NOT NULL, volume TEXT NOT NULL,
    verification TEXT,            -- on this drive
    rel_path TEXT NOT NULL, filename TEXT, kind TEXT, size INTEGER, xxh64 TEXT,
    duration REAL, start_timecode TEXT, end_timecode TEXT, codec TEXT, resolution TEXT, frame_rate TEXT,
    created TEXT, camera_model TEXT, sample_rate INTEGER, channels INTEGER, notes TEXT
);
CREATE TABLE project_summary (  -- copied from the project history on the Mac: every drive set
    drive_set TEXT NOT NULL, shoot_date TEXT NOT NULL,
    cards INTEGER, clips INTEGER, bytes INTEGER, failed INTEGER,
    PRIMARY KEY (drive_set, shoot_date)
);
CREATE INDEX files_offload ON files (offload_id);
CREATE INDEX clips_offload ON clips (offload_id);
CREATE INDEX offloads_date ON offloads (shoot_date);
"""


@contextmanager
def _locked(drive: Path, timeout: float) -> Iterator[None]:
    with locked(logs_dir(drive) / LOCK_NAME, timeout,
                f"The master log on {drive} is busy (another offload is writing it)"):
        yield


def _upgrade(conn: sqlite3.Connection) -> None:
    """Bring a copied log up to the current schema (runs on the temp copy, never in place)."""
    version = int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
    if version < 2:
        conn.execute("ALTER TABLE clips ADD COLUMN end_timecode TEXT")
        for row_id, start, duration, rate in conn.execute(
                "SELECT id, start_timecode, duration, frame_rate FROM clips").fetchall():
            conn.execute("UPDATE clips SET end_timecode = ? WHERE id = ?",
                         (end_timecode(start or "", duration, rate), row_id))
    if version < 3:
        conn.execute("""CREATE TABLE IF NOT EXISTS project_summary (
            drive_set TEXT NOT NULL, shoot_date TEXT NOT NULL,
            cards INTEGER, clips INTEGER, bytes INTEGER, failed INTEGER,
            PRIMARY KEY (drive_set, shoot_date))""")
    if version < SCHEMA_VERSION:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))


def _store_summary(conn: sqlite3.Connection, project_summary: List[Dict[str, Any]]) -> None:
    conn.execute("DELETE FROM project_summary")  # a fresh snapshot each time
    conn.executemany(
        "INSERT INTO project_summary (drive_set, shoot_date, cards, clips, bytes, failed) VALUES (?,?,?,?,?,?)",
        [(r["drive_set"], r["shoot_date"], r["cards"], r["clips"], r["bytes"], r["failed"]) for r in project_summary],
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('project_summary_updated', ?)",
                 (datetime.now().isoformat(timespec="seconds"),))


def _init_schema(conn: sqlite3.Connection, volume: str) -> None:
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [("schema_version", str(SCHEMA_VERSION)), ("volume", volume),
         ("created", datetime.now().isoformat(timespec="seconds"))],
    )


# --- public API -------------------------------------------------------------

def add_record(drive: Path, record: Dict[str, Any], lock_timeout: float = LOCK_TIMEOUT,
               project_summary: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Add one card record to this drive's log. Returns False if it was already there.

    project_summary (from the project history) replaces the drive's copy of the
    all-drives summary, so this drive carries the totals of earlier drive sets.
    """
    drive = require_mounted(drive)
    target = master_log_path(drive)
    with _locked(drive, lock_timeout):
        exists = target.exists()
        if exists and not is_healthy(target):
            raise LogError(f"{target} is damaged. Run `mit log rebuild` to rebuild it from the card records.")

        def build(conn: sqlite3.Connection) -> bool:
            if exists:
                copy_into(target, conn)
                _upgrade(conn)
            else:
                _init_schema(conn, drive.name)
            added = _insert(conn, record, drive.name)
            if project_summary is not None:
                _store_summary(conn, project_summary)
            return added

        return replace_db(target, build)


def rebuild(drive: Path, lock_timeout: float = LOCK_TIMEOUT) -> Tuple[int, Optional[Path]]:
    """Add every card record on the drive that the log doesn't have yet.

    Records come from _REPORTS/*/ and _INCOMPLETE/ (earlier failed attempts).
    Nothing already in a healthy log is removed. A damaged log is renamed
    aside (not deleted) and a fresh one built. Returns (records added, path of
    the damaged log if one was set aside).
    """
    drive = require_mounted(drive)
    target = master_log_path(drive)
    records = sorted(find_records(drive), key=lambda r: r.get("offload_started", ""))
    with _locked(drive, lock_timeout):
        set_aside = set_aside_if_damaged(target)
        exists = target.exists()

        def build(conn: sqlite3.Connection) -> int:
            if exists:
                copy_into(target, conn)
                _upgrade(conn)
            else:
                _init_schema(conn, drive.name)
            return sum(_insert(conn, r, drive.name) for r in records)

        return replace_db(target, build), set_aside


def set_project_summary(drive: Path, project_summary: List[Dict[str, Any]],
                        lock_timeout: float = LOCK_TIMEOUT) -> None:
    """Refresh the drive's copy of the all-drives summary."""
    drive = require_mounted(drive)
    target = master_log_path(drive)
    with _locked(drive, lock_timeout):
        if not target.exists() or not is_healthy(target):
            raise LogError(f"No healthy master log on {drive}")

        def build(conn: sqlite3.Connection) -> None:
            copy_into(target, conn)
            _upgrade(conn)
            _store_summary(conn, project_summary)

        replace_db(target, build)


def connect(drive: Path) -> sqlite3.Connection:
    """Read-only connection to the drive's log, rows as sqlite3.Row."""
    path = master_log_path(drive)
    if not path.exists():
        raise LogError(f"No master log on {drive}")
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    return conn


def find_records(drive: Path) -> List[Dict[str, Any]]:
    """Every card record on a drive, including earlier failed attempts in _INCOMPLETE."""
    paths = list((drive / REPORTS_DIRNAME).glob("*/*_card.json")) + list((drive / INCOMPLETE_DIRNAME).glob("*_card.json"))
    records = []
    for path in paths:
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return records


# --- inserting a record -----------------------------------------------------

def _insert(conn: sqlite3.Connection, record: Dict[str, Any], volume: str) -> bool:
    card = record["card"]
    started = record.get("offload_started", "")
    uid = record.get("offload_id") or f"{card}@{started}"  # records from before offload IDs existed
    if conn.execute("SELECT 1 FROM offloads WHERE offload_uid = ?", (uid,)).fetchone():
        return False

    dest = next((d for d in record.get("destinations", []) if d.get("volume") == volume), None)
    clips = record.get("clips", [])
    camera = record.get("camera", {})
    durations = [c["duration"] for c in clips if c.get("kind") != "still" and c.get("duration") is not None]
    cur = conn.execute(
        """INSERT INTO offloads (offload_uid, card, shoot_date, camera_id, camera_model, camera_description, camera_shooter,
               media_type, reel, project_name, show_code, offload_started, offload_finished, operator,
               source_volume, destinations, volume, result, drive_result, file_count, clip_count, total_bytes,
               total_duration, elapsed_seconds, transfer_speed, notes, errors, card_path, tool_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uid, card, record.get("shoot_date", ""), camera.get("id") or card[9:-2], camera.get("model", ""),
            camera.get("description", ""), camera.get("shooter", ""), record.get("media_type", ""), card[-2:],
            record.get("project_name", ""), record.get("show_code", ""), started,
            record.get("offload_finished", ""), record.get("operator", ""), record.get("source_volume", ""),
            " & ".join(d.get("volume", "") for d in record.get("destinations", [])), volume,
            record.get("result", "FAIL"), (dest or {}).get("result", "FAIL"), record.get("file_count"),
            len(clips), record.get("total_bytes"), sum(durations) if durations else None,
            record.get("elapsed_seconds"), record.get("transfer_speed"), record.get("notes", ""),
            "; ".join(record.get("errors", [])), (dest or {}).get("card_path", ""), record.get("tool_version", ""),
        ),
    )
    offload_id = cur.lastrowid
    status = {f["path"]: f.get("verification", {}).get(volume, "MISSING") for f in record.get("files", [])}
    conn.executemany(
        "INSERT INTO files (offload_id, path, size, xxh64, verification) VALUES (?,?,?,?,?)",
        [(offload_id, f["path"], f.get("size"), f.get("xxh64", ""), status[f["path"]]) for f in record.get("files", [])],
    )
    conn.executemany(
        """INSERT INTO clips (offload_id, card, shoot_date, camera_id, offload_started, volume, verification,
               rel_path, filename, kind, size, xxh64, duration, start_timecode, end_timecode, codec, resolution,
               frame_rate, created, camera_model, sample_rate, channels, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (offload_id, card, record.get("shoot_date", ""), camera.get("id") or card[9:-2], started, volume,
             status.get(c["rel_path"], "MISSING"), c["rel_path"], c.get("filename"), c.get("kind"), c.get("size"),
             c.get("xxh64", ""), c.get("duration"), c.get("start_timecode", ""),
             c.get("end_timecode") or end_timecode(c.get("start_timecode", ""), c.get("duration"), c.get("frame_rate")),
             c.get("codec", ""),
             c.get("resolution", ""), c.get("frame_rate", ""), c.get("created", ""), c.get("camera_model", ""),
             c.get("sample_rate"), c.get("channels"), "; ".join(c.get("notes", [])))
            for c in clips
        ],
    )
    # Only the latest attempt for a card name counts; earlier ones are history.
    conn.execute(
        """UPDATE offloads SET superseded = CASE WHEN id =
               (SELECT id FROM offloads WHERE card = ? ORDER BY offload_started DESC, id DESC LIMIT 1)
               THEN 0 ELSE 1 END
           WHERE card = ?""",
        (card, card),
    )
    return True
