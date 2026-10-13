"""Crash-safe SQLite writes, shared by the drive master logs and the project history.

A database is never changed in place. Each write copies it to a temp file in
the same folder, changes the copy, runs SQLite's integrity check, flushes it,
then atomically replaces the original (safety rule 7). A lock file serialises
writers so concurrent offloads can't lose each other's changes.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .errors import IngestError
from .fileutil import _fsync_dir


class LogError(IngestError):
    """A log database couldn't be read or written."""


@contextmanager
def locked(lock_path: Path, timeout: float, busy_message: str) -> Iterator[None]:
    """Hold an exclusive lock on lock_path, waiting up to timeout seconds."""
    lock_path.parent.mkdir(exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LogError(busy_message) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def is_healthy(path: Path) -> bool:
    try:
        with contextlib.closing(connect_readonly(path)) as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def copy_into(src: Path, conn: sqlite3.Connection) -> None:
    """Copy an existing database into conn (SQLite's backup API)."""
    with contextlib.closing(connect_readonly(src)) as source:
        source.backup(conn)


def set_aside_if_damaged(target: Path) -> Optional[Path]:
    """Rename a damaged database out of the way (never deleted). Returns its new path."""
    if target.exists() and not is_healthy(target):
        aside = target.with_name(f"{target.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}")
        os.rename(target, aside)
        return aside
    return None


def replace_db(target: Path, build: Callable[[sqlite3.Connection], Any]) -> Any:
    """Build the new database in a temp file (build(conn) does the work), then swap it in."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("PRAGMA journal_mode=MEMORY")  # the temp file is disposable; no journal on disk
            result = build(conn)
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise LogError(f"integrity check failed on the new {target.name}")
        finally:
            conn.close()
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()  # our own half-built temp file
        raise
    _fsync_dir(target.parent)
    return result
