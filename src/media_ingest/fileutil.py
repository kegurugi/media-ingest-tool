"""Safe file writing helpers."""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, "os.PathLike[str]"]


def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    """Write data to path so readers only ever see the old or the new file.

    The data goes to a temp file in the same directory, is fsynced, and then
    os.replace()s the original. If anything fails, the original is untouched
    and the half-written temp file (created by this call) is removed.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def atomic_write_text(path: PathLike, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _fsync_dir(directory: Path) -> None:
    # Persist the rename itself. Not every filesystem supports this (e.g. exFAT).
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
