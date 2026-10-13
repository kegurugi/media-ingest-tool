"""Per-day details for the daily card log: header fields, crew and day notes.

They change during a season (location, leg, crew), so they're stored per
shoot date rather than in project.json:
  - _REPORTS/<date>/<date>_day.json on every destination (travels with the media)
  - days/<date>_day.json in the project folder, next to project.json, so a day
    that continues on new drives keeps its details.

A new day starts from the most recent earlier day's details (carried forward),
or from project.json on the first day of a project. If copies disagree, the
most recently edited one wins.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .config import ProjectConfig
from .errors import NamingError
from .fileutil import atomic_write_text
from .layout import REPORTS_DIRNAME, reports_dir
from .naming import date_folder_name, parse_date_folder

PathLike = Union[str, Path]
PROJECT_DAYS_DIRNAME = "days"


@dataclass
class DayDetails:
    shoot_date: str  # ISO date
    header_fields: List[Tuple[str, str]] = field(default_factory=list)  # ordered (label, value)
    crew: List[Tuple[str, str]] = field(default_factory=list)  # ordered (role, name)
    day_notes: str = ""
    updated_at: str = ""
    carried_from: str = ""  # "2026-10-11", "project.json", or "" once edited/confirmed as-is

    def to_json(self) -> str:
        data = asdict(self)
        data["header_fields"] = [{"label": k, "value": v} for k, v in self.header_fields]
        data["crew"] = [{"role": r, "name": n} for r, n in self.crew]
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DayDetails":
        return cls(
            shoot_date=str(data.get("shoot_date", "")),
            header_fields=[(str(f["label"]), str(f["value"])) for f in data.get("header_fields", [])],
            crew=[(str(c["role"]), str(c["name"])) for c in data.get("crew", [])],
            day_notes=str(data.get("day_notes", "")),
            updated_at=str(data.get("updated_at", "")),
            carried_from=str(data.get("carried_from", "")),
        )

    def same_content(self, other: "DayDetails") -> bool:
        return (self.header_fields, self.crew, self.day_notes) == (other.header_fields, other.crew, other.day_notes)


def day_filename(shoot_date: date) -> str:
    return f"{date_folder_name(shoot_date)}_day.json"


def project_days_dir(config_path: PathLike) -> Path:
    return Path(config_path).expanduser().resolve().parent / PROJECT_DAYS_DIRNAME


def _locations(drives: Iterable[PathLike], project_dir: Optional[Path], shoot_date: date) -> List[Path]:
    paths = [reports_dir(d, shoot_date) / day_filename(shoot_date) for d in drives if Path(d).is_dir()]
    if project_dir is not None:
        paths.append(project_dir / day_filename(shoot_date))
    return paths


def _read(path: Path) -> Optional[DayDetails]:
    try:
        return DayDetails.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_day(drives: Iterable[PathLike], shoot_date: date, project_dir: Optional[Path] = None
             ) -> Tuple[Optional[DayDetails], List[str]]:
    """The day's details from the drives and project folder; the newest edit wins.

    Returns (details or None, notes about copies that disagreed or were missing).
    """
    found = []
    for path in _locations(drives, project_dir, shoot_date):
        details = _read(path)
        if details is not None:
            found.append((path, details))
    if not found:
        return None, []
    newest_path, newest = max(found, key=lambda pd: pd[1].updated_at)
    notes = [f"Day details in {path} differ from the newest copy ({newest_path}); the newest was used."
             for path, d in found if not d.same_content(newest)]
    return newest, notes


def carried_forward(drives: Iterable[PathLike], shoot_date: date, project_dir: Optional[Path],
                    config: ProjectConfig) -> DayDetails:
    """Details for a new day: the most recent earlier day's, or project.json's."""
    candidates: List[Tuple[date, DayDetails]] = []
    folders = [Path(d) / REPORTS_DIRNAME for d in drives if Path(d).is_dir()]
    for folder in folders:
        for path in folder.glob("*/*_day.json"):
            _collect(path, path.parent.name, shoot_date, candidates)
    if project_dir is not None and project_dir.is_dir():
        for path in project_dir.glob("*_day.json"):
            _collect(path, path.name[:8], shoot_date, candidates)
    if candidates:
        when, previous = max(candidates, key=lambda c: (c[0], c[1].updated_at))
        return DayDetails(shoot_date=shoot_date.isoformat(), header_fields=list(previous.header_fields),
                          crew=list(previous.crew), carried_from=when.isoformat())
    return DayDetails(
        shoot_date=shoot_date.isoformat(),
        header_fields=list(config.header_fields.items()),
        crew=[(m.role, m.name) for m in config.crew],
        carried_from="project.json",
    )


def _collect(path: Path, folder_name: str, before: date, out: List[Tuple[date, DayDetails]]) -> None:
    try:
        when = parse_date_folder(folder_name)
    except NamingError:
        return
    if when < before:
        details = _read(path)
        if details is not None:
            out.append((when, details))


def write_day(drives: Iterable[PathLike], details: DayDetails, project_dir: Optional[Path] = None,
              stamp: bool = True) -> List[str]:
    """Write the details (atomically) to every mounted drive and the project folder. Returns errors."""
    if stamp:
        details.updated_at = datetime.now().isoformat(timespec="seconds")
    shoot_date = date.fromisoformat(details.shoot_date)
    errors = []
    targets = [reports_dir(d, shoot_date) for d in drives if Path(d).is_dir()]
    if project_dir is not None:
        targets.append(project_dir)
    for folder in targets:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write_text(folder / day_filename(shoot_date), details.to_json())
        except OSError as e:
            errors.append(f"Could not save day details in {folder}: {e}")
    return errors
