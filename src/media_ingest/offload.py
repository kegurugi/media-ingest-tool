"""Offload one card: checks, copy, verify, hash record, markers.

Steps:
  1. Pre-flight: destinations mounted, on separate drives, not the source.
  2. Space check on every destination. Nothing is created if any is short.
  3. Create card folders and write incomplete markers.
  4. Copy to all destinations, hashing the source.
  5. Re-read and verify every destination file.
  6. Extract clip metadata from a verified copy.
  7. Write the card record (hashes, results, metadata) on every destination.
  8. Remove the markers only if everything passed; otherwise mark FAILED.
"""
from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, diskio
from .copier import CopyResult, ProgressFn, copy_card
from .errors import CopyError, DestinationExistsError, IngestError, SourceError
from .incomplete import COPYING, FAILED, MARKER_NAME, Marker, remove_marker, write_marker
from .layout import card_record_path, create_card_folder, find_existing_card, prepare_drive, require_mounted
from .naming import CardName
from .metadata import ClipMetadata, ToolStatus, extract_card, find_tools
from .records import write_card_record
from .sources import SourceListing, list_source, volume_name
from .space import require_space
from .verify import DestinationVerification, expected_files, verify_destinations

MIN_DESTINATIONS = 2


@dataclass
class OffloadResult:
    card: CardName
    media_type: str
    source: Path
    source_volume: str
    drives: List[Path]
    listing: SourceListing
    started_at: datetime
    card_dirs: List[Path] = field(default_factory=list)
    copy: Optional[CopyResult] = None
    verifications: List[DestinationVerification] = field(default_factory=list)
    clips: List[ClipMetadata] = field(default_factory=list)
    card_records: List[Path] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and self.copy is not None
            and len(self.verifications) == len(self.drives)
            and all(v.ok for v in self.verifications)
        )

    @property
    def file_count(self) -> int:
        return len(self.listing.files)

    @property
    def total_bytes(self) -> int:
        return self.listing.total_bytes

    @property
    def copy_speed(self) -> float:
        """Bytes per second during the copy phase."""
        if not self.copy or self.copy.elapsed <= 0:
            return 0.0
        return self.copy.bytes_copied / self.copy.elapsed


def preflight(source: Path, drives: Sequence[Path], allow_same_volume: bool = False) -> None:
    """Refuse setups that could lose data or defeat the point of multiple copies."""
    source = Path(source)
    if not source.is_dir():
        raise SourceError(f"Source card {source} not found or not a folder")
    if len(drives) < MIN_DESTINATIONS:
        raise IngestError(f"At least {MIN_DESTINATIONS} destinations are required, got {len(drives)}")

    resolved = [require_mounted(d).resolve() for d in drives]
    if len(set(resolved)) != len(resolved):
        raise IngestError("The same destination is listed more than once")

    src = source.resolve()
    for d in resolved:
        if _is_within(src, d) or _is_within(d, src):
            raise IngestError(f"Destination {d} and source {src} overlap")

    if not allow_same_volume:
        devices: Dict[int, Path] = {}
        for d in resolved:
            dev = os.stat(d).st_dev
            if dev in devices:
                raise IngestError(
                    f"{devices[dev]} and {d} are on the same drive. Each copy must go to a separate drive."
                )
            devices[dev] = d
        if os.stat(src).st_dev in devices:
            raise IngestError(f"Source {src} is on the same drive as destination {devices[os.stat(src).st_dev]}")


def run_offload(
    source: Path,
    drives: Sequence[Path],
    card: CardName,
    media_type: str,
    listing: Optional[SourceListing] = None,
    copy_all: bool = False,
    progress: Optional[ProgressFn] = None,
    allow_same_volume: bool = False,
    chunk_size: int = diskio.CHUNK_SIZE,
    exif: Optional[Dict[str, Dict[str, Any]]] = None,
    tools: Optional[ToolStatus] = None,
) -> OffloadResult:
    """Offload one card. Returns a result whose .passed says PASS or FAIL.

    Raises (before anything is created) for pre-flight, space and
    existing-folder problems. A KeyboardInterrupt during the copy leaves the
    COPYING markers in place, so the next run flags the card as incomplete.
    """
    source = Path(source)
    drives = [Path(d) for d in drives]
    started_at = datetime.now()
    t0 = time.monotonic()

    preflight(source, drives, allow_same_volume)
    if listing is None:
        listing = list_source(source, copy_all=copy_all)
    for d in drives:
        existing = find_existing_card(d, card)
        if existing:
            raise DestinationExistsError(f"Card folder {card} already exists: {existing[0]}")
    require_space(listing, drives)

    result = OffloadResult(
        card=card,
        media_type=media_type,
        source=source,
        source_volume=volume_name(source),
        drives=drives,
        listing=listing,
        started_at=started_at,
    )
    marker = Marker(
        card=str(card),
        status=COPYING,
        source_volume=result.source_volume,
        file_count=len(listing.files),
        total_bytes=listing.total_bytes,
        started_at=started_at.isoformat(timespec="seconds"),
        tool_version=__version__,
    )
    for d in drives:
        prepare_drive(d, card.shoot_date)
        card_dir = create_card_folder(d, card, media_type)
        write_marker(card_dir, marker)
        result.card_dirs.append(card_dir)

    try:
        result.copy = copy_card(listing, result.card_dirs, progress=progress, chunk_size=chunk_size)
    except (CopyError, SourceError) as e:
        result.errors.append(str(e))
        _mark_failed(result, marker, str(e))
        result.elapsed = time.monotonic() - t0
        return result

    result.verifications = verify_destinations(
        result.card_dirs,
        expected_files(listing.files, result.copy.hashes),
        expected_dirs=listing.dirs,
        ignore=[MARKER_NAME],
    )

    # Metadata comes from the first destination that verified, never the card again.
    verified = next((v.card_dir for v in result.verifications if v.ok), None)
    if verified is not None:
        result.clips = extract_card(verified, listing.files, result.copy.hashes, exif=exif, tools=tools or find_tools())

    record = _card_record(result, copy_all)
    for d in drives:
        path = card_record_path(d, card)
        try:
            write_card_record(path, record)
            result.card_records.append(path)
        except (OSError, IngestError) as e:
            result.errors.append(f"Could not write card record on {d}: {e}")

    if result.passed:
        for card_dir in result.card_dirs:
            remove_marker(card_dir)
    else:
        _mark_failed(result, marker, _failure_summary(result))
    result.elapsed = time.monotonic() - t0
    return result


def _mark_failed(result: OffloadResult, marker: Marker, detail: str) -> None:
    failed = Marker(**{**marker.__dict__, "status": FAILED, "detail": detail})
    for card_dir in result.card_dirs:
        with contextlib.suppress(OSError):
            write_marker(card_dir, failed)


def _failure_summary(result: OffloadResult) -> str:
    parts = list(result.errors)
    for v in result.verifications:
        if not v.ok:
            parts.append(
                f"{v.card_dir}: {len(v.failures)} file(s) failed, "
                f"{len(v.missing_dirs)} folder(s) missing, {len(v.extra_files)} unexpected file(s)"
            )
    return "; ".join(parts)


def _card_record(result: OffloadResult, copy_all: bool) -> Dict[str, Any]:
    statuses = {v.card_dir: v.statuses() for v in result.verifications}
    destinations = []
    for drive, card_dir, v in zip(result.drives, result.card_dirs, result.verifications):
        destinations.append(
            {
                "volume": drive.name,
                "card_path": str(card_dir.relative_to(drive)),
                "result": "PASS" if v.ok else "FAIL",
                "missing_dirs": v.missing_dirs,
                "extra_files": v.extra_files,
            }
        )
    files = []
    for f in result.listing.files:
        files.append(
            {
                "path": f.rel_path,
                "size": f.size,
                "xxh64": result.copy.hashes[f.rel_path],
                "verification": {
                    drive.name: statuses[card_dir].get(f.rel_path, "MISSING")
                    for drive, card_dir in zip(result.drives, result.card_dirs)
                },
            }
        )
    return {
        "card": str(result.card),
        "media_type": result.media_type,
        "shoot_date": result.card.shoot_date.isoformat(),
        "source_volume": result.source_volume,
        "offload_started": result.started_at.isoformat(timespec="seconds"),
        "tool_version": __version__,
        "algorithm": "xxh64",
        "result": "PASS" if all(v.ok for v in result.verifications) else "FAIL",
        "copy_all": copy_all,
        "file_count": result.file_count,
        "total_bytes": result.total_bytes,
        "destinations": destinations,
        "dirs": result.listing.dirs,
        "skipped": result.listing.skipped,
        "files": files,
        "clips": [c.to_dict() for c in result.clips],
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
