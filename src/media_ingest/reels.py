"""Reel numbering: sequential per camera per day, starting at 01."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Set

from .config import MEDIA_TYPES
from .errors import NamingError, ReelMismatchError
from .layout import DriveLike, media_dir, require_mounted
from .naming import MAX_REEL, CardName


def existing_reels(drive: DriveLike, show: str, shoot_date: date, cam: str) -> Set[int]:
    """Reel numbers already used on this drive for show + date + camera.

    Looks in VIDEO, AUDIO and STILLS together, so a reel number is unique per
    camera per day whichever folder its card was filed in. The camera must
    match exactly: camera D does not match DR1's cards.
    """
    require_mounted(drive)
    show, cam = show.upper(), cam.upper()
    reels: Set[int] = set()
    for media_type in MEDIA_TYPES:
        folder = media_dir(drive, shoot_date, media_type)
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            try:
                card = CardName.parse(entry.name)
            except NamingError:
                continue
            if card.show == show and card.shoot_date == shoot_date and card.cam == cam:
                reels.add(card.reel)
    return reels


def propose_reel(drives: Iterable[DriveLike], show: str, shoot_date: date, cam: str) -> int:
    """Next reel number (highest existing + 1). Destinations must agree.

    Every destination is meant to be identical, so if they hold different
    reels for this camera and day, something went wrong earlier (e.g. an
    interrupted offload) and the operator has to sort it out first.
    """
    per_drive: Dict[Path, Set[int]] = {
        Path(d): existing_reels(d, show, shoot_date, cam) for d in drives
    }
    if not per_drive:
        raise ValueError("at least one destination is required")

    reel_sets = list(per_drive.values())
    if any(s != reel_sets[0] for s in reel_sets[1:]):
        details = "\n".join(f"  {drive}: {_format_reels(reels)}" for drive, reels in per_drive.items())
        raise ReelMismatchError(
            f"Destinations disagree about existing reels for camera {cam.upper()} on {shoot_date}:\n{details}"
        )

    next_reel = max(reel_sets[0], default=0) + 1
    if next_reel > MAX_REEL:
        raise NamingError(f"Camera {cam.upper()} already has reel {MAX_REEL:02d} on {shoot_date}; no reels left")
    return next_reel


def _format_reels(reels: Set[int]) -> str:
    return ", ".join(f"{r:02d}" for r in sorted(reels)) or "none"
