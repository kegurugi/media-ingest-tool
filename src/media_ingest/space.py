"""Free-space check on every destination before anything is copied."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Union

from .errors import InsufficientSpaceError
from .sources import SourceListing

# Room left over for reports, logs and filesystem overhead.
SAFETY_MARGIN_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class SpaceCheck:
    drive: Path
    free: int
    required: int

    @property
    def ok(self) -> bool:
        return self.free >= self.required

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.free)


def required_bytes(listing: SourceListing, block_size: int, margin: int = SAFETY_MARGIN_BYTES) -> int:
    """Space the card will take: every file rounded up to whole blocks, plus folders and a margin."""
    blocks = sum(-(-f.size // block_size) for f in listing.files) + len(listing.dirs) + 1
    return blocks * block_size + margin


def check_space(
    listing: SourceListing, drives: Iterable[Union[str, Path]], margin: int = SAFETY_MARGIN_BYTES
) -> List[SpaceCheck]:
    checks = []
    for drive in drives:
        drive = Path(drive)
        block_size = os.statvfs(drive).f_frsize or 4096
        checks.append(
            SpaceCheck(drive=drive, free=shutil.disk_usage(drive).free, required=required_bytes(listing, block_size, margin))
        )
    return checks


def require_space(
    listing: SourceListing, drives: Iterable[Union[str, Path]], margin: int = SAFETY_MARGIN_BYTES
) -> List[SpaceCheck]:
    """Raise InsufficientSpaceError if any destination lacks room. Nothing is copied in that case."""
    checks = check_space(listing, drives, margin)
    short = [c for c in checks if not c.ok]
    if short:
        lines = [
            f"  {c.drive}: needs {format_bytes(c.required)}, has {format_bytes(c.free)} "
            f"(short by {format_bytes(c.shortfall)})"
            for c in short
        ]
        raise InsufficientSpaceError("Not enough free space. Nothing was copied.\n" + "\n".join(lines))
    return checks


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000
    return f"{n:.2f} TB"
