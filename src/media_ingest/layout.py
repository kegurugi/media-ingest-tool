"""Destination drive layout.

<DRIVE>/
  MMDDYYYY/{VIDEO,AUDIO,STILLS}/<CARD>/   card contents, copied as-is
  _REPORTS/MMDDYYYY/                       job reports + daily card log
  _LOGS/                                   media_log.sqlite + <DRIVE>_media_log.xlsx
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Union

from .config import MEDIA_TYPES
from .errors import DestinationExistsError, IngestError
from .naming import CardName, date_folder_name

REPORTS_DIRNAME = "_REPORTS"
LOGS_DIRNAME = "_LOGS"
MASTER_LOG_FILENAME = "media_log.sqlite"

DriveLike = Union[str, Path]


def require_mounted(drive: DriveLike) -> Path:
    """Return the drive root, or fail if it isn't there.

    This stops the tool from creating a stray /Volumes/<name> folder on the
    boot disk when a destination drive isn't mounted.
    """
    root = Path(drive)
    if not root.is_dir():
        raise IngestError(f"Destination {root} not found. Is the drive mounted?")
    return root


def date_dir(drive: DriveLike, shoot_date: date) -> Path:
    return Path(drive) / date_folder_name(shoot_date)


def media_dir(drive: DriveLike, shoot_date: date, media_type: str) -> Path:
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media type must be one of {', '.join(MEDIA_TYPES)}, got {media_type!r}")
    return date_dir(drive, shoot_date) / media_type


def card_dir(drive: DriveLike, card: CardName, media_type: str) -> Path:
    return media_dir(drive, card.shoot_date, media_type) / str(card)


def reports_dir(drive: DriveLike, shoot_date: date) -> Path:
    return Path(drive) / REPORTS_DIRNAME / date_folder_name(shoot_date)


def card_report_path(drive: DriveLike, card: CardName) -> Path:
    return reports_dir(drive, card.shoot_date) / f"{card}_report.html"


def card_record_path(drive: DriveLike, card: CardName) -> Path:
    """Per-card JSON record: hashes, verification results and clip metadata."""
    return reports_dir(drive, card.shoot_date) / f"{card}_card.json"


def daily_log_path(drive: DriveLike, shoot_date: date) -> Path:
    name = date_folder_name(shoot_date)
    return reports_dir(drive, shoot_date) / f"{name}_daily_card_log.html"


def logs_dir(drive: DriveLike) -> Path:
    return Path(drive) / LOGS_DIRNAME


def master_log_path(drive: DriveLike) -> Path:
    return logs_dir(drive) / MASTER_LOG_FILENAME


def xlsx_log_path(drive: DriveLike) -> Path:
    return logs_dir(drive) / f"{Path(drive).name}_media_log.xlsx"


def find_existing_card(drive: DriveLike, card: CardName) -> List[Path]:
    """Every folder on the drive already using this card name, in any media type.

    Compared case-insensitively, so it behaves the same on case-sensitive volumes.
    """
    found = []
    for media_type in MEDIA_TYPES:
        folder = media_dir(drive, card.shoot_date, media_type)
        if not folder.is_dir():
            continue
        found.extend(entry for entry in folder.iterdir() if entry.name.upper() == str(card))
    return sorted(found)


def create_card_folder(drive: DriveLike, card: CardName, media_type: str) -> Path:
    """Create the (empty) card folder. Never reuses or overwrites an existing one.

    Fails if the card name already exists under any media type, so the same
    card name can never appear twice on a drive.
    """
    require_mounted(drive)
    target = card_dir(drive, card, media_type)
    existing = find_existing_card(drive, card)
    if existing:
        raise DestinationExistsError(
            f"Card folder {card} already exists: {', '.join(str(p) for p in existing)}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.mkdir()
    except FileExistsError:
        raise DestinationExistsError(f"Card folder already exists: {target}") from None
    return target


def prepare_drive(drive: DriveLike, shoot_date: date) -> None:
    """Create the shared _REPORTS/<date> and _LOGS folders if they don't exist yet."""
    require_mounted(drive)
    reports_dir(drive, shoot_date).mkdir(parents=True, exist_ok=True)
    logs_dir(drive).mkdir(exist_ok=True)
