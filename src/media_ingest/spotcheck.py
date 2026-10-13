"""`mit verify`: re-hash existing card folders against the hashes in the log.

Used for drive spot-checks and for checking a shuttle drive on arrival. Files
are only read (with the OS cache off), never changed.

Expected hashes come from the drive's master log (the card's current offload).
If there's no healthy log, the card record in _REPORTS is used instead; if both
exist and disagree, that's flagged. With --drive, the cards to check come from
the log, so a card folder that should be there but isn't is a FAIL.
"""
from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .config import MEDIA_TYPES
from .errors import IngestError, NamingError
from .incomplete import COPYING, MARKER_NAME, read_marker
from .layout import REPORTS_DIRNAME, card_record_path, master_log_path
from .masterlog import connect
from .naming import CardName, parse_date_folder
from .mhl import MHL_DIRNAME
from .report import report_filename
from .sources import is_macos_clutter
from .sqlitesafe import is_healthy
from .verify import ExpectedFile, FileCheck, verify_destination

SOURCE_LOG = "master log"
SOURCE_RECORD = "card record"
MISSING_FOLDER = "card folder missing"


@dataclass
class Expected:
    card: CardName
    files: List[ExpectedFile]
    dirs: Optional[List[str]]  # None when unknown (no card record)
    source: str
    offload_uid: str = ""
    card_path: str = ""  # relative to the drive, from the log/record
    offload_result: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class CardCheck:
    card: str
    drive: Path
    folder: Path
    shoot_date: str = ""
    source: str = ""
    offload_uid: str = ""
    files_checked: int = 0
    bytes_checked: int = 0
    failures: List[FileCheck] = field(default_factory=list)
    missing_dirs: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)  # card-level failures (missing folder, no hashes, ...)
    warnings: List[str] = field(default_factory=list)  # unexpected extra files
    notes: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def passed(self) -> bool:
        return not (self.failures or self.missing_dirs or self.problems)

    def details(self) -> str:
        parts = list(self.problems)
        parts += [f"{c.status} {c.rel_path}" for c in self.failures]
        parts += [f"missing folder {d}" for d in self.missing_dirs]
        parts += [f"warning: {w}" for w in self.warnings]
        return "; ".join(parts)


# --- locating cards ---------------------------------------------------------

def locate_card(folder: Path) -> Tuple[Path, CardName, str]:
    """<DRIVE>/<MMDDYYYY>/<MEDIA>/<CARD> -> (drive, card, media type)."""
    folder = Path(folder).expanduser()
    parts = folder.resolve().parts if folder.exists() else folder.absolute().parts
    if len(parts) < 4:
        raise IngestError(f"{folder} is not a card folder (<DRIVE>/<date>/<VIDEO|AUDIO|STILLS>/<CARD>)")
    date_name, media_type, card_name = parts[-3], parts[-2], parts[-1]
    try:
        shoot_date = parse_date_folder(date_name)
        card = CardName.parse(card_name)
    except NamingError:
        raise IngestError(f"{folder} is not a card folder (<DRIVE>/<date>/<VIDEO|AUDIO|STILLS>/<CARD>)") from None
    if media_type.upper() not in MEDIA_TYPES or card.shoot_date != shoot_date:
        raise IngestError(f"{folder} is not a card folder (<DRIVE>/<date>/<VIDEO|AUDIO|STILLS>/<CARD>)")
    return Path(*parts[:-3]), card, media_type.upper()


def cards_on_drive(drive: Path, shoot_date: Optional[date] = None) -> List[Tuple[str, str]]:
    """(card name, card folder relative to the drive) for every current card in the drive's log
    (or card records, if there's no healthy log), optionally for one shoot date."""
    drive = Path(drive)
    if master_log_path(drive).exists() and is_healthy(master_log_path(drive)):
        with contextlib.closing(connect(drive)) as conn:
            sql = "SELECT card, card_path FROM offloads WHERE superseded = 0"
            args: tuple = ()
            if shoot_date:
                sql += " AND shoot_date = ?"
                args = (shoot_date.isoformat(),)
            return [(r["card"], r["card_path"]) for r in conn.execute(sql + " ORDER BY shoot_date, card", args)]
    found = []
    pattern = f"{shoot_date:%m%d%Y}/*_card.json" if shoot_date else "*/*_card.json"
    for path in sorted((drive / REPORTS_DIRNAME).glob(pattern)):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dest = next((d for d in record.get("destinations", []) if d.get("volume") == drive.name), None)
        if dest:
            found.append((record["card"], dest["card_path"]))
    return found


def unlisted_card_folders(drive: Path, listed: List[Tuple[str, str]], shoot_date: Optional[date] = None) -> List[str]:
    """Card folders on the drive that the log doesn't list (e.g. copied there by hand)."""
    known = {path for _, path in listed}
    extra = []
    date_dirs = [drive / f"{shoot_date:%m%d%Y}"] if shoot_date else [p for p in Path(drive).iterdir() if p.is_dir()]
    for date_dir in date_dirs:
        try:
            parse_date_folder(date_dir.name)
        except NamingError:
            continue
        for media in MEDIA_TYPES:
            for folder in sorted((date_dir / media).glob("*")) if (date_dir / media).is_dir() else []:
                rel = str(folder.relative_to(drive))
                if folder.is_dir() and rel not in known:
                    try:
                        CardName.parse(folder.name)
                    except NamingError:
                        continue
                    extra.append(rel)
    return extra


# --- expected hashes --------------------------------------------------------

def load_expected(drive: Path, card: CardName) -> Expected:
    record = _read_record(drive, card)
    from_log = _expected_from_log(drive, card)
    if from_log is not None:
        expected = from_log
        if record is not None:
            expected.dirs = list(record.get("dirs", []))
            expected.notes += _compare(from_log.files, _record_files(record))
        else:
            expected.notes.append("No card record found, so folders weren't checked.")
        return expected
    if record is None:
        raise IngestError(f"No master log entry or card record for {card} on {drive}: nothing to verify against")
    dest = next((d for d in record.get("destinations", []) if d.get("volume") == drive.name), {})
    return Expected(
        card=card, files=_record_files(record), dirs=list(record.get("dirs", [])), source=SOURCE_RECORD,
        offload_uid=record.get("offload_id", ""), card_path=dest.get("card_path", ""),
        offload_result=record.get("result", ""),
        notes=["The master log has no entry for this card (or is missing or damaged); "
               "hashes came from the card record."],
    )


def _expected_from_log(drive: Path, card: CardName) -> Optional[Expected]:
    log = master_log_path(drive)
    if not log.exists() or not is_healthy(log):
        return None
    with contextlib.closing(connect(drive)) as conn:
        row = conn.execute(
            "SELECT * FROM offloads WHERE card = ? AND superseded = 0 ORDER BY offload_started DESC, id DESC LIMIT 1",
            (str(card),)).fetchone()
        if row is None:
            return None
        files = [ExpectedFile(r["path"], r["size"], r["xxh64"] or "")
                 for r in conn.execute("SELECT path, size, xxh64 FROM files WHERE offload_id = ? ORDER BY path", (row["id"],))]
    return Expected(card=card, files=files, dirs=None, source=SOURCE_LOG, offload_uid=row["offload_uid"],
                    card_path=row["card_path"] or "", offload_result=row["result"])


def _read_record(drive: Path, card: CardName) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(card_record_path(drive, card).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record_files(record: Dict[str, Any]) -> List[ExpectedFile]:
    return [ExpectedFile(f["path"], f["size"], f.get("xxh64", "")) for f in record.get("files", [])]


def _compare(log_files: List[ExpectedFile], record_files: List[ExpectedFile]) -> List[str]:
    log = {f.rel_path: f for f in log_files}
    rec = {f.rel_path: f for f in record_files}
    notes = []
    for path in sorted(set(log) | set(rec)):
        a, b = log.get(path), rec.get(path)
        if a is None or b is None:
            notes.append(f"LOG/RECORD DISAGREE: {path} is only in the {'card record' if a is None else 'master log'}")
        elif (a.size, a.xxh64) != (b.size, b.xxh64):
            notes.append(f"LOG/RECORD DISAGREE: {path} has a different size or hash in the master log and card record")
    return notes


# --- checking ---------------------------------------------------------------

def check_card(folder: Path, drive: Optional[Path] = None, card: Optional[CardName] = None,
               progress: Optional[Callable[[int, int, str], None]] = None) -> CardCheck:
    """Re-hash one card folder against its expected hashes."""
    folder = Path(folder)
    if drive is None or card is None:
        drive, card, _ = locate_card(folder)
    t0 = time.monotonic()
    check = CardCheck(card=str(card), drive=drive, folder=folder, shoot_date=card.shoot_date.isoformat())
    expected = load_expected(drive, card)
    check.source, check.offload_uid = expected.source, expected.offload_uid
    check.notes += expected.notes
    check.problems += [n for n in expected.notes if n.startswith("LOG/RECORD DISAGREE")]
    if expected.offload_result and expected.offload_result != "PASS":
        check.notes.append(f"This card's original offload was {expected.offload_result} and it hasn't been recopied.")
    if expected.card_path and str(folder.resolve()) != str((drive / expected.card_path).resolve()):
        check.notes.append(f"The log lists this card at {expected.card_path}.")
    if not folder.is_dir():
        check.problems.append(MISSING_FOLDER)
        return check
    marker = read_marker(folder)
    if marker is not None and marker.status == COPYING:
        # Either an offload is writing this card right now, or one was interrupted. Its
        # record and log entry may not exist yet, so there's nothing reliable to check against.
        check.problems.append(
            f"The card is still being offloaded (started {marker.started_at}), or that offload was "
            "interrupted. Check again once it has finished; if it was interrupted, recopy the card.")
        return check
    if marker is not None:
        check.notes.append(f"The card folder is marked incomplete ({marker.status}).")

    unhashed = [e for e in expected.files if not e.xxh64]
    if unhashed:
        check.problems.append(f"{len(unhashed)} file(s) have no recorded hash (the original copy failed)")
    hashed = [e for e in expected.files if e.xxh64]
    result = verify_destination(folder, hashed, expected.dirs or (), ignore=[MARKER_NAME, report_filename(str(card))],
                                progress=progress)
    check.files_checked = len(hashed)
    check.bytes_checked = sum(e.size for e in hashed)
    check.failures = result.failures
    check.missing_dirs = result.missing_dirs
    for rel in result.extra_files:
        parts = PurePosixPath(rel).parts
        if parts[0] == MHL_DIRNAME:
            continue  # the card's ASC MHL manifest
        if not any(is_macos_clutter(part) for part in parts):
            check.warnings.append(f"unexpected file {rel}")
    check.elapsed = time.monotonic() - t0
    return check


def record_results(drive: Path, checks: List[CardCheck], when: Optional[datetime] = None) -> None:
    """Add the checks to the drive's master log."""
    from .masterlog import add_verifications
    when = when or datetime.now()
    add_verifications(drive, [{
        "card": c.card, "shoot_date": c.shoot_date, "offload_uid": c.offload_uid,
        "verified_at": when.isoformat(timespec="seconds"), "result": "PASS" if c.passed else "FAIL",
        "files_checked": c.files_checked, "bytes_checked": c.bytes_checked,
        "failures": len(c.failures) + len(c.missing_dirs) + len(c.problems),
        "source": c.source, "details": c.details(), "tool_version": __version__,
    } for c in checks])
