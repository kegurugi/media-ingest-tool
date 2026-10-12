"""Card folder names ([SHOW][YY][MM][DD][CAM][REEL]) and date folder names (MMDDYYYY).

Parsing rule from the spec: the first 3 characters are SHOW, the next 6 are
the date, the last 2 are REEL, and whatever is in between is CAM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .config import CAMERA_ID_RE, SHOW_CODE_RE
from .errors import NamingError

MAX_REEL = 99

_CARD_RE = re.compile(r"^([A-Z]{3})(\d{2})(\d{2})(\d{2})([A-Z0-9]{1,3})(\d{2})$")
_DATE_FOLDER_RE = re.compile(r"^(\d{2})(\d{2})(\d{4})$")


@dataclass(frozen=True)
class CardName:
    show: str
    shoot_date: date
    cam: str
    reel: int

    def __post_init__(self) -> None:
        show = self.show.strip().upper()
        cam = self.cam.strip().upper()
        if not SHOW_CODE_RE.match(show):
            raise NamingError(f"show code must be exactly 3 letters, got {self.show!r}")
        if not CAMERA_ID_RE.match(cam):
            raise NamingError(f"camera ID must be 1-3 letters or digits, got {self.cam!r}")
        if not 2000 <= self.shoot_date.year <= 2099:
            raise NamingError(f"shoot date {self.shoot_date} is outside 2000-2099")
        if not 1 <= self.reel <= MAX_REEL:
            raise NamingError(f"reel must be 01-{MAX_REEL:02d}, got {self.reel}")
        object.__setattr__(self, "show", show)
        object.__setattr__(self, "cam", cam)

    def __str__(self) -> str:
        return f"{self.show}{self.shoot_date:%y%m%d}{self.cam}{self.reel:02d}"

    @classmethod
    def parse(cls, name: str) -> "CardName":
        """Parse a card folder name. Case-insensitive; the result is uppercase."""
        m = _CARD_RE.match(name.strip().upper())
        if not m:
            raise NamingError(f"{name!r} is not a card name like ABC261012A01")
        show, yy, mm, dd, cam, reel = m.groups()
        try:
            shoot_date = date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            raise NamingError(f"{name!r} contains an invalid date {yy}{mm}{dd}") from None
        return cls(show=show, shoot_date=shoot_date, cam=cam, reel=int(reel))


def date_folder_name(d: date) -> str:
    """Shoot date folder name, MMDDYYYY."""
    return f"{d:%m%d%Y}"


def parse_date_folder(name: str) -> date:
    m = _DATE_FOLDER_RE.match(name.strip())
    if not m:
        raise NamingError(f"{name!r} is not a date folder like 10122026 (MMDDYYYY)")
    mm, dd, yyyy = (int(g) for g in m.groups())
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        raise NamingError(f"{name!r} is not a valid date") from None
