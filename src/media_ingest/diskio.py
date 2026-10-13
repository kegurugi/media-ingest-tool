"""Low-level disk I/O used by copy and verify."""
from __future__ import annotations

import contextlib
import fcntl
import os

import xxhash

CHUNK_SIZE = 8 * 1024 * 1024

_F_NOCACHE = getattr(fcntl, "F_NOCACHE", None)  # macOS
_F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None)  # macOS


def disable_cache(fd: int) -> bool:
    """Turn off the OS file cache for fd, so reads come from the drive.

    Used on destination files both when writing (so the written data isn't
    kept in memory) and when verifying (so the re-read isn't served from
    memory). Returns False where unsupported.
    """
    if _F_NOCACHE is None:
        return False
    try:
        fcntl.fcntl(fd, _F_NOCACHE, 1)
        return True
    except OSError:
        return False


def full_sync(fd: int) -> None:
    """Flush fd all the way to the physical drive.

    On macOS, plain fsync can leave data in the drive's own write cache;
    F_FULLFSYNC asks the drive to flush it. Falls back to fsync on
    filesystems that don't support it.
    """
    if _F_FULLFSYNC is not None:
        with contextlib.suppress(OSError):
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
    os.fsync(fd)


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def hash_file(path: "os.PathLike[str] | str", chunk_size: int = CHUNK_SIZE) -> "tuple[str, int]":
    """xxHash64 (hex) and byte count of a file, read with the OS cache disabled."""
    fd = os.open(path, os.O_RDONLY)
    try:
        disable_cache(fd)
        h = xxhash.xxh64()
        size = 0
        while True:
            chunk = os.read(fd, chunk_size)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
        return h.hexdigest(), size
    finally:
        os.close(fd)
