"""Incomplete-card markers and the recopy flow.

When copying starts, a marker file is written into each destination card
folder. It is removed only after every file on every destination verifies.
If verification fails, the marker stays and records FAILED.

A card folder with a marker is incomplete. On a recopy the operator confirms
it's the same card, the incomplete folder is moved (never deleted) to
<DRIVE>/_INCOMPLETE/<CARD>_<timestamp>/, and the card is copied fresh under
the same reel number.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import MEDIA_TYPES
from .errors import IncompleteMismatchError, IngestError, NamingError
from .fileutil import atomic_write_text
from .layout import DriveLike, card_record_path, card_report_path, media_dir, require_mounted
from .naming import CardName
from .sources import SourceListing

MARKER_NAME = ".mit_incomplete.json"
INCOMPLETE_DIRNAME = "_INCOMPLETE"

COPYING = "COPYING"
FAILED = "FAILED"


@dataclass
class Marker:
    card: str
    status: str  # COPYING or FAILED
    source_volume: str
    file_count: int
    total_bytes: int
    started_at: str  # ISO 8601, local time
    tool_version: str
    detail: str = ""


@dataclass
class IncompleteCard:
    card: CardName
    media_type: str
    path: Path
    marker: Marker


def write_marker(card_dir: Path, marker: Marker) -> None:
    atomic_write_text(Path(card_dir) / MARKER_NAME, json.dumps(asdict(marker), indent=2) + "\n")


def read_marker(card_dir: Path) -> Optional[Marker]:
    path = Path(card_dir) / MARKER_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A marker that can't be read still means the folder is incomplete.
        return Marker(card=Path(card_dir).name, status="UNREADABLE MARKER", source_volume="",
                      file_count=0, total_bytes=0, started_at="", tool_version="")
    known = {k: data.get(k) for k in Marker.__dataclass_fields__ if k in data}
    defaults = dict(card=Path(card_dir).name, status=COPYING, source_volume="", file_count=0,
                    total_bytes=0, started_at="", tool_version="")
    return Marker(**{**defaults, **known})


def remove_marker(card_dir: Path) -> None:
    """Remove the marker after a verified copy.

    This is the tool's only deletion of a file on a destination, and it only
    ever removes its own marker.
    """
    (Path(card_dir) / MARKER_NAME).unlink()


def find_incomplete(drive: DriveLike, show: str, shoot_date: date, cam: str) -> List[IncompleteCard]:
    """Incomplete card folders on one drive for show + date + camera, in any media type."""
    require_mounted(drive)
    found = []
    for media_type in MEDIA_TYPES:
        folder = media_dir(drive, shoot_date, media_type)
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            try:
                card = CardName.parse(entry.name)
            except NamingError:
                continue
            if card.show != show.upper() or card.shoot_date != shoot_date or card.cam != cam.upper():
                continue
            marker = read_marker(entry)
            if marker is not None:
                found.append(IncompleteCard(card, media_type, entry, marker))
    return found


def find_incomplete_all(
    drives: List[DriveLike], show: str, shoot_date: date, cam: str
) -> Dict[str, List[IncompleteCard]]:
    """Incomplete cards across all destinations, keyed by card name.

    Every destination must report the same incomplete cards in the same media
    type; otherwise the tool stops for the operator to investigate.
    """
    per_drive = {Path(d): find_incomplete(d, show, shoot_date, cam) for d in drives}
    signatures = {d: sorted((str(i.card), i.media_type) for i in found) for d, found in per_drive.items()}
    first = next(iter(signatures.values()), [])
    if any(sig != first for sig in signatures.values()):
        lines = "\n".join(
            f"  {d}: {', '.join(f'{name} ({mt})' for name, mt in sig) or 'none'}" for d, sig in signatures.items()
        )
        raise IncompleteMismatchError(
            f"Destinations disagree about incomplete cards for camera {cam.upper()} on {shoot_date}:\n{lines}"
        )
    by_card: Dict[str, List[IncompleteCard]] = {}
    for found in per_drive.values():
        for item in found:
            by_card.setdefault(str(item.card), []).append(item)
    return by_card


def marker_differences(marker: Marker, listing: SourceListing, source_volume: str) -> List[str]:
    """Ways the card in the reader differs from the one the marker describes."""
    diffs = []
    if marker.source_volume and marker.source_volume != source_volume:
        diffs.append(f"source volume was {marker.source_volume!r}, now {source_volume!r}")
    if marker.file_count and marker.file_count != len(listing.files):
        diffs.append(f"file count was {marker.file_count}, now {len(listing.files)}")
    if marker.total_bytes and marker.total_bytes != listing.total_bytes:
        diffs.append(f"total size was {marker.total_bytes} bytes, now {listing.total_bytes}")
    return diffs


def quarantine(item: IncompleteCard, drive: DriveLike, now: Optional[datetime] = None) -> Path:
    """Move an incomplete card folder out of the media folders. Nothing is deleted.

    A card record and job report left by a failed attempt are moved alongside
    it, so the recopy can write fresh ones without overwriting the old.
    """
    drive = require_mounted(drive)
    now = now or datetime.now()
    try:
        item.path.resolve().relative_to(drive.resolve())
    except ValueError:
        raise IngestError(f"{item.path} is not on {drive}") from None
    target_root = drive / INCOMPLETE_DIRNAME
    target_root.mkdir(exist_ok=True)
    target = target_root / f"{item.card}_{now:%Y%m%d-%H%M%S}"
    if target.exists():
        raise IngestError(f"{target} already exists")
    leftovers = [
        (card_record_path(drive, item.card), target_root / f"{target.name}_card.json"),
        (card_report_path(drive, item.card), target_root / f"{target.name}_report.html"),
    ]
    for src, dst in leftovers:
        if src.exists() and dst.exists():
            raise IngestError(f"{dst} already exists")
    os.rename(item.path, target)  # same volume: a rename, not a copy
    for src, dst in leftovers:
        if src.exists():
            os.rename(src, dst)
    return target
