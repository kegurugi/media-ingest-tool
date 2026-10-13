"""Copy a card to several destinations at once, hashing the source as it is read.

Each source file is read once. Every chunk updates the xxHash64 and is handed
to one writer thread per destination, so a slow drive doesn't stall the others
more than the queue depth allows.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import xxhash

from . import diskio
from .errors import CopyError, SourceError
from .sources import SourceListing

QUEUE_DEPTH = 4

# progress(bytes_done, bytes_total, current_rel_path)
ProgressFn = Callable[[int, int, str], None]


@dataclass
class CopyResult:
    hashes: Dict[str, str]  # rel_path -> xxh64 of the bytes read from the source
    bytes_copied: int
    elapsed: float


class _DestinationWriter:
    """Writes files into one destination card folder on its own thread."""

    def __init__(self, card_dir: Path) -> None:
        self.card_dir = card_dir
        self.error: Optional[BaseException] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_DEPTH)
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=f"writer:{card_dir}", daemon=True)
        self._thread.start()

    def put(self, *op) -> None:
        self._queue.put(op)

    def stop(self, timeout: Optional[float] = None) -> None:
        if not self._stopped:
            self._stopped = True
            self._queue.put(("stop",))
        self._thread.join(timeout)

    def _run(self) -> None:
        fd: Optional[int] = None
        path: Optional[Path] = None
        while True:
            op = self._queue.get()
            kind = op[0]
            if kind == "stop":
                break
            if self.error is not None:
                continue  # keep draining so the reader never blocks
            try:
                if kind == "open":
                    path = self.card_dir / op[1]
                    # O_EXCL: never overwrite, even a single file.
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                    diskio.disable_cache(fd)
                elif kind == "write":
                    diskio.write_all(fd, op[1])
                elif kind == "close":
                    diskio.full_sync(fd)
                    os.close(fd)
                    fd = None
                    os.utime(path, ns=(op[1], op[1]))
            except BaseException as e:  # noqa: BLE001 - reported to the reader thread
                self.error = e
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
        if fd is not None:
            os.close(fd)


def copy_card(
    listing: SourceListing,
    card_dirs: Sequence[Path],
    progress: Optional[ProgressFn] = None,
    chunk_size: int = diskio.CHUNK_SIZE,
) -> CopyResult:
    """Copy every listed file and folder into each (empty) card folder.

    Raises CopyError if any read or write fails. Files already written stay
    where they are; nothing is deleted.
    """
    card_dirs = [Path(d) for d in card_dirs]
    for card_dir in card_dirs:
        for rel in listing.dirs:
            try:
                (card_dir / rel).mkdir()
            except OSError as e:
                raise CopyError(f"Cannot create folder {card_dir / rel}: {e}") from e

    total = listing.total_bytes
    done = 0
    hashes: Dict[str, str] = {}
    start = time.monotonic()
    writers: List[_DestinationWriter] = [_DestinationWriter(d) for d in card_dirs]

    def check_writers() -> None:
        for w in writers:
            if w.error is not None:
                raise CopyError(f"Writing to {w.card_dir} failed: {w.error}") from w.error

    try:
        for f in listing.files:
            for w in writers:
                w.put("open", f.rel_path)
            h = xxhash.xxh64()
            size = 0
            try:
                src = os.open(listing.root / f.rel_path, os.O_RDONLY)
            except OSError as e:
                raise CopyError(f"Cannot open {f.rel_path} on the source card: {e}") from e
            try:
                while True:
                    try:
                        chunk = os.read(src, chunk_size)
                    except OSError as e:
                        raise CopyError(f"Reading {f.rel_path} from the source card failed: {e}") from e
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)
                    for w in writers:
                        w.put("write", chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, f.rel_path)
                    check_writers()
            finally:
                os.close(src)
            if size != f.size:
                raise SourceError(
                    f"{f.rel_path} changed size during the copy ({f.size} -> {size} bytes). "
                    "Is something writing to the card?"
                )
            for w in writers:
                w.put("close", f.mtime_ns)
            hashes[f.rel_path] = h.hexdigest()

        for w in writers:
            w.stop()
        check_writers()
    finally:
        for w in writers:
            w.stop(timeout=30)

    return CopyResult(hashes=hashes, bytes_copied=done, elapsed=time.monotonic() - start)
