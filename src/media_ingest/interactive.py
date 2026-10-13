"""Interactive `mit ingest`: gather choices, confirm with the operator, offload, summarise."""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, TextIO

from . import daily_log, history, masterlog, spotcheck
from .daydetails import DayDetails, carried_forward, project_days_dir, read_day, write_day
from .config import MEDIA_TYPES, load_config
from .errors import ConfigError, IngestError, NamingError
from .incomplete import find_incomplete_all, marker_differences, quarantine
from .layout import card_dir, card_record_path, reports_dir
from .mediatype import choice_warnings, propose_media_type, scan_card
from .metadata import clip_datetimes, find_tools, read_exif, select_clips
from .naming import MAX_REEL, CardName
from .offload import JobInfo, OffloadResult, preflight, run_offload
from .reels import existing_reels, propose_reel
from .shootdate import propose_shoot_date
from .sources import list_source, volume_name
from .records import read_card_record
from .report import iter_card_records, report_filename, write_report
from .space import format_bytes, require_space
from .xlsx_export import export_xlsx

EXIT_PASS = 0
EXIT_FAIL = 1  # verification failed, or the operator cancelled
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130


class Prompter:
    def __init__(self, input_fn: Optional[Callable[[str], str]] = None, out: Optional[TextIO] = None,
                 err: Optional[TextIO] = None):
        self.input_fn = input_fn or (lambda prompt: input(prompt))
        self._out = out
        self._err = err

    @property
    def out(self) -> TextIO:
        return self._out or sys.stdout

    @property
    def err(self) -> TextIO:
        return self._err or sys.stderr

    def say(self, msg: str = "") -> None:
        print(msg, file=self.out)

    def warn_loud(self, lines: List[str]) -> None:
        red, reset = ("\033[1;31m", "\033[0m") if self.err.isatty() else ("", "")
        bar = "!" * 72
        print(f"{red}{bar}", file=self.err)
        for line in lines:
            print(f"!!  {line}", file=self.err)
        print(f"{bar}{reset}", file=self.err)

    def ask(self, prompt: str, default: Optional[str] = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self.input_fn(f"{prompt}{suffix}: ").strip()
            if answer:
                return answer
            if default:
                return default

    def ask_optional(self, prompt: str) -> str:
        return self.input_fn(f"{prompt}: ").strip()

    def confirm(self, prompt: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.input_fn(f"{prompt} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False


def parse_shoot_date(text: str) -> date:
    """Accept MMDDYYYY, MM/DD/YYYY or YYYY-MM-DD."""
    text = text.strip()
    for pattern, order in (
        (r"^(\d{2})(\d{2})(\d{4})$", "mdy"),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "mdy"),
        (r"^(\d{4})-(\d{2})-(\d{2})$", "ymd"),
    ):
        m = re.match(pattern, text)
        if m:
            a, b, c = (int(g) for g in m.groups())
            try:
                return date(c, a, b) if order == "mdy" else date(a, b, c)
            except ValueError:
                break
    raise NamingError(f"{text!r} is not a valid date (use MMDDYYYY, e.g. 10122026)")


def resolve_destination(name: str) -> Path:
    """A bare volume name means /Volumes/<name>; anything with a slash is a path."""
    name = name.strip()
    return Path(name).expanduser() if "/" in name or name.startswith("~") else Path("/Volumes") / name


def parse_media_type(text: str) -> Optional[str]:
    text = text.strip().upper()
    for mt in MEDIA_TYPES:
        if text and mt.startswith(text):
            return mt
    return None


def run_ingest(args, p: Prompter) -> int:
    cfg = load_config(args.config)
    history_file = history.history_path(args.config)

    source = Path(args.source or p.ask("Source card path (e.g. /Volumes/A001)")).expanduser()
    if args.dest:
        drives = [resolve_destination(d) for d in args.dest]
    else:
        default = ", ".join(cfg.default_destinations) or None
        drives = [resolve_destination(d) for d in p.ask("Destinations, comma-separated", default).split(",") if d.strip()]
    preflight(source, drives, args.allow_same_volume)

    if args.camera:
        camera = cfg.camera(args.camera)
    else:
        p.say("Cameras: " + ", ".join(f"{c.id} ({c.description or c.model})" for c in cfg.cameras.values()))
        camera = cfg.camera(p.ask("Camera ID"))
    operator = _choose_operator(p, cfg.operators, args.operator)

    p.say(f"\nScanning {source} ...")
    scan = scan_card(source)
    listing = list_source(source, copy_all=args.copy_all)
    source_vol = volume_name(source)
    p.say(f"  {len(listing.files):,} files, {format_bytes(listing.total_bytes)} ({scan.summary()})")
    if listing.skipped:
        p.say(f"  Skipping {len(listing.skipped)} macOS system item(s): {', '.join(listing.skipped[:5])}"
              + (" ..." if len(listing.skipped) > 5 else ""))

    # Shoot date: proposed from clip dates (read-only pass over the card); the operator confirms.
    tools = find_tools()
    if tools.missing:
        p.warn_loud([f"{', '.join(tools.missing)} not found: clip metadata will be incomplete.",
                     "Install with: brew install exiftool ffmpeg"])
    clips = select_clips(listing.files)
    exif = read_exif(source, [f.rel_path for f, _ in clips], tools.exiftool)
    shoot_date = _confirm_shoot_date(p, args.date, clip_datetimes(exif, clips, root=source))

    # Day details (header fields, crew) are confirmed on the first offload of each day.
    days_dir = project_days_dir(args.config)
    day, day_notes = read_day(drives, shoot_date, days_dir)
    for note in day_notes:
        p.say(f"  note: {note}")
    new_day = day is None
    if new_day:
        day = carried_forward(drives, shoot_date, days_dir, cfg)
        origin = ("project.json" if day.carried_from == "project.json"
                  else f"carried forward from {date.fromisoformat(day.carried_from):%m/%d/%Y}")
        p.say(f"\nDay details for {shoot_date:%m/%d/%Y} ({origin}):")
        _show_day(p, day)
        if not p.confirm(f"Use these for {shoot_date:%m/%d/%Y}?", default=True):
            edit_day(p, day)

    # Recopy of an incomplete or failed card keeps its reel number.
    card: Optional[CardName] = None
    recopy = []
    incomplete = find_incomplete_all(drives, cfg.show_code, shoot_date, camera.id)
    for name, items in incomplete.items():
        m = items[0].marker
        p.say(f"\n{name} ({items[0].media_type}) is INCOMPLETE: status {m.status}, started {m.started_at}, "
              f"source {m.source_volume or 'unknown'}.")
        if m.detail:
            p.say(f"  {m.detail}")
        diffs = marker_differences(m, listing, source_vol)
        if diffs:
            p.warn_loud([f"The card in the reader may not be the card that was copied to {name}:", *diffs])
        if p.confirm(f"Is this the same card? Recopy it as {name}"):
            card, recopy = items[0].card, items
            break
    if card is None:
        if incomplete:
            p.say(f"Incomplete card(s) stay flagged: {', '.join(incomplete)}")
        # Reels used on other drive sets (project history) are never reused.
        elsewhere = history.used_reels(history_file, cfg.show_code, shoot_date, camera.id)
        card = CardName(cfg.show_code, shoot_date, camera.id,
                        propose_reel(drives, cfg.show_code, shoot_date, camera.id, also_used=elsewhere))
        if elsewhere:
            p.say(f"  Camera {camera.id} also has reel(s) {', '.join(f'{r:02d}' for r in sorted(elsewhere))} "
                  f"on {shoot_date:%m/%d/%Y} in the project history (other drives included)")
        if not p.confirm(f"Card name {card}", default=True):
            card = _ask_reel(p, drives, cfg.show_code, shoot_date, camera.id, elsewhere)

    # Media type: proposed, the operator decides.
    proposal = propose_media_type(scan, camera)
    default_mt = recopy[0].media_type if recopy else proposal.media_type
    p.say(f"\nMedia type: {proposal.media_type or 'undecided'} - {proposal.reason}")
    for note in proposal.notes:
        p.say(f"  note: {note}")
    if proposal.warnings:
        p.warn_loud(proposal.warnings)
    while True:
        media_type = args.media_type and parse_media_type(args.media_type) or parse_media_type(
            p.ask("Media type (VIDEO/AUDIO/STILLS)", default_mt)
        )
        if media_type is None:
            continue
        warnings = choice_warnings(scan, media_type)
        if not warnings:
            break
        p.warn_loud(warnings)
        if p.confirm(f"File it under {media_type} anyway?"):
            break
        args.media_type = None

    p.say("\nReady to offload:")
    p.say(f"  Card:    {card}  (camera {camera.id}: {camera.model}"
          + (f", {camera.description}" if camera.description else "") + ")")
    p.say(f"  Media:   {media_type}")
    p.say(f"  Source:  {source}  ({source_vol}): {len(listing.files):,} files, {format_bytes(listing.total_bytes)}")
    for d in drives:
        p.say(f"  Target:  {card_dir(d, card, media_type)}")
    p.say(f"  Operator: {operator}")
    if recopy:
        p.say(f"  Recopy:  the incomplete {card} folders will be moved to _INCOMPLETE (not deleted)")
    if args.notes is not None:
        notes = args.notes
    elif recopy and _earlier_notes(recopy, drives):
        earlier = _earlier_notes(recopy, drives)
        answer = p.ask_optional(f"Notes for this card [{earlier}] (Enter keeps, '-' clears, or type new notes)")
        notes = "" if answer == "-" else (answer or earlier)
    else:
        notes = p.ask_optional("Notes for this card (optional, Enter to skip)")
    if not p.confirm("Start offload?"):
        p.say("Cancelled. Nothing was copied.")
        return EXIT_FAIL

    require_space(listing, drives)  # before moving anything aside
    for problem in write_day(drives, day, days_dir, stamp=new_day):
        p.say(f"  warning: {problem}")
    for item in recopy:
        drive = next(d for d in drives if _is_within(item.path, d))
        moved = quarantine(item, drive)
        p.say(f"  Moved incomplete folder to {moved}")

    progress = _ProgressPrinter(p.err)
    try:
        result = run_offload(
            source, drives, card, media_type, listing=listing, copy_all=args.copy_all,
            progress=progress, allow_same_volume=args.allow_same_volume, exif=exif, tools=tools,
            job=JobInfo(
                project_name=cfg.project_name, show_code=cfg.show_code, camera_id=camera.id,
                camera_model=camera.model, camera_description=camera.description, camera_shooter=camera.shooter,
                operator=operator, notes=notes,
            ),
            project_history=history_file,
        )
    except KeyboardInterrupt:
        progress.finish()
        p.warn_loud([
            "INTERRUPTED. The copy is incomplete.",
            f"Run `mit ingest` again with the same card to recopy it as {card}.",
        ])
        return EXIT_INTERRUPTED
    progress.finish()
    _print_summary(p, result)
    return EXIT_PASS if result.passed else EXIT_FAIL


def run_report(args, p: Prompter) -> int:
    """Rebuild the job reports in _REPORTS/<date>/ from the card records on each destination,
    then the daily card log. The report copies inside card folders are left exactly as written.
    """
    shoot_date = parse_shoot_date(args.date)
    names = args.dest or load_config(args.config).default_destinations
    drives = [resolve_destination(d) for d in names]
    rebuilt = 0
    for drive in drives:
        if not drive.is_dir():
            p.say(f"  {drive}: not mounted, skipped")
            continue
        records = list(iter_card_records(reports_dir(drive, shoot_date)))
        for path in records:
            try:
                record = read_card_record(path)
                target = path.parent / report_filename(record["card"])
                write_report(target, record)
            except (OSError, ValueError, KeyError) as e:
                p.say(f"  {path}: could not rebuild report: {e}")
                continue
            rebuilt += 1
            p.say(f"  {target}")
        if not records:
            p.say(f"  {drive}: no card records for {shoot_date:%m/%d/%Y}")
    p.say(f"Rebuilt {rebuilt} report(s).")
    if rebuilt:
        written, problems = daily_log.write_daily_log(drives, shoot_date, project_days_dir(args.config))
        for path in written:
            p.say(f"  {path}")
        for problem in problems:
            p.say(f"  warning: {problem}")
    return EXIT_PASS if rebuilt else EXIT_ERROR


def run_log(args, p: Prompter) -> int:
    """`mit log export`: rebuild each drive's spreadsheet from its SQLite log.
    `mit log rebuild`: add any card records missing from each drive's SQLite log, then export."""
    names = args.dest or load_config(args.config).default_destinations
    done = 0
    for drive in (resolve_destination(d) for d in names):
        if not drive.is_dir():
            p.say(f"  {drive}: not mounted, skipped")
            continue
        try:
            if args.log_command == "rebuild":
                added, set_aside = masterlog.rebuild(drive)
                if set_aside:
                    p.warn_loud([f"The master log on {drive} was damaged.",
                                 f"It was moved to {set_aside} (not deleted) and rebuilt from the card records."])
                p.say(f"  {drive}: {added} card record(s) added to the master log")
                history_file = history.history_path(args.config)
                if history_file.exists():
                    masterlog.set_project_summary(drive, history.summary(history_file))
            path = export_xlsx(drive)
        except (IngestError, OSError, sqlite3.Error) as e:
            p.say(f"  {drive}: {e}")
            continue
        p.say(f"  {path}")
        done += 1
    return EXIT_PASS if done else EXIT_ERROR


def run_history(args, p: Prompter) -> int:
    """`mit history rebuild`: add card records from the connected drives to the project history,
    then refresh each drive's copy of the all-drives summary.
    `mit history show`: print the project totals per drive set."""
    path = history.history_path(args.config)
    if args.history_command == "rebuild":
        names = args.dest or load_config(args.config).default_destinations
        drives = [resolve_destination(d) for d in names]
        mounted = [d for d in drives if d.is_dir()]
        for d in drives:
            if d not in mounted:
                p.say(f"  {d}: not mounted, skipped")
        if not mounted:
            return EXIT_ERROR
        added, set_aside = history.rebuild_from_drives(path, mounted)
        if set_aside:
            p.warn_loud([f"The project history was damaged. It was moved to {set_aside} (not deleted) and rebuilt."])
        p.say(f"  {added} card record(s) added to {path}")
        summary = history.summary(path)
        for d in mounted:
            try:
                masterlog.set_project_summary(d, summary)
                export_xlsx(d)
                p.say(f"  {d}: all-drives summary updated")
            except (IngestError, OSError, sqlite3.Error) as e:
                p.say(f"  {d}: {e}")
    rows = history.summary(path)
    if not rows:
        p.say(f"No project history at {path}")
        return EXIT_PASS if args.history_command == "rebuild" else EXIT_ERROR
    p.say(f"Project history: {path}")
    totals: dict = {}
    for r in rows:
        t = totals.setdefault(r["drive_set"], [0, 0, 0, set()])
        t[0] += r["cards"]; t[1] += r["bytes"] or 0; t[2] += r["failed"] or 0; t[3].add(r["shoot_date"])
    for name, (cards, size, failed, days) in totals.items():
        p.say(f"  {name}: {cards} card(s), {format_bytes(size)}, {len(days)} day(s)"
              + (f", {failed} failed" if failed else ""))
    p.say(f"  Project total: {sum(t[0] for t in totals.values())} card(s), "
          f"{format_bytes(sum(t[1] for t in totals.values()))}")
    return EXIT_PASS


def run_daylog(args, p: Prompter) -> int:
    """Edit a day's header fields, crew and day notes, then rebuild its daily card log."""
    cfg = load_config(args.config)
    shoot_date = parse_shoot_date(args.date)
    drives = [resolve_destination(d) for d in (args.dest or cfg.default_destinations)]
    for d in drives:
        if not d.is_dir():
            p.say(f"  {d}: not mounted; it will be updated next time it's connected and the log is rebuilt")
    days_dir = project_days_dir(args.config)
    day, notes = read_day(drives, shoot_date, days_dir)
    for note in notes:
        p.say(f"  note: {note}")
    if day is None:
        day = carried_forward(drives, shoot_date, days_dir, cfg)
        p.say(f"No details saved for {shoot_date:%m/%d/%Y} yet; starting from "
              + ("project.json" if day.carried_from == "project.json"
                 else f"{date.fromisoformat(day.carried_from):%m/%d/%Y}"))
    p.say(f"\nDay details for {shoot_date:%m/%d/%Y}:")
    _show_day(p, day)
    if args.notes is not None:
        day.day_notes = args.notes
    else:
        if p.confirm("Edit header fields and crew?"):
            edit_day(p, day)
        current = f" [{day.day_notes}]" if day.day_notes else ""
        text = p.ask_optional(f"Day notes{current} (Enter keeps, '-' clears)")
        if text == "-":
            day.day_notes = ""
        elif text:
            day.day_notes = text
    problems = write_day(drives, day, days_dir)
    written, more = daily_log.write_daily_log(drives, shoot_date, days_dir)
    for problem in problems + more:
        p.say(f"  warning: {problem}")
    for path in written:
        p.say(f"  {path}")
    p.say(f"Saved day details for {shoot_date:%m/%d/%Y}.")
    return EXIT_PASS


def _show_day(p: Prompter, day: DayDetails) -> None:
    fields = "   ".join(f"{k}: {v}" for k, v in day.header_fields) or "(no header fields)"
    crew = "   ".join(f"{r}: {n}" for r, n in day.crew) or "(no crew)"
    p.say(f"  {fields}")
    p.say(f"  {crew}")


def edit_day(p: Prompter, day: DayDetails) -> None:
    """Walk through header fields and crew: Enter keeps, text replaces, '-' removes; then add new ones."""
    day.header_fields = _edit_pairs(p, "Header fields", day.header_fields, "label")
    day.crew = _edit_pairs(p, "Crew", day.crew, "role")
    day.carried_from = ""


def _edit_pairs(p: Prompter, title: str, pairs, key_name: str):
    p.say(f"{title} (Enter keeps, type to replace, '-' removes):")
    result = []
    for key, value in pairs:
        answer = p.ask_optional(f"  {key} [{value}]")
        if answer == "-":
            continue
        result.append((key, answer or value))
    while True:
        key = p.ask_optional(f"  Add to {title.lower()} ({key_name}, or Enter to finish)")
        if not key:
            return result
        value = p.ask_optional(f"  {key}")
        if value:
            result.append((key, value))


def run_verify(args, p: Prompter) -> int:
    """`mit verify`: re-hash card folders against the hashes in the log."""
    if args.drive and args.folders:
        raise IngestError("Give card folders or --drive, not both")
    if args.date and not args.drive:
        raise IngestError("--date needs --drive")
    if not args.drive and not args.folders:
        raise IngestError("Give one or more card folders, or --drive (optionally with --date)")

    targets = []  # (folder, drive, card)
    if args.drive:
        drive = resolve_destination(args.drive)
        if not drive.is_dir():
            raise IngestError(f"{drive} not found. Is the drive mounted?")
        shoot_date = parse_shoot_date(args.date) if args.date else None
        listed = spotcheck.cards_on_drive(drive, shoot_date)
        if not listed:
            raise IngestError(f"No cards logged on {drive}" + (f" for {shoot_date:%m/%d/%Y}" if shoot_date else ""))
        for name, rel in listed:
            targets.append((drive / rel, drive, CardName.parse(name)))
        for rel in spotcheck.unlisted_card_folders(drive, listed, shoot_date):
            p.say(f"  note: {rel} is on the drive but not in its log (not checked)")
    else:
        for folder in args.folders:
            drive, card, _ = spotcheck.locate_card(Path(folder))
            spotcheck.load_expected(drive, card)  # fail early if there's nothing to verify against
            targets.append((Path(folder).expanduser(), drive, card))

    total = len(targets)
    checks = []
    p.say(f"Verifying {total} card(s)...")
    for i, (folder, drive, card) in enumerate(targets, 1):
        progress = _ProgressPrinter(p.err)
        try:
            check = spotcheck.check_card(folder, drive, card, progress=progress)
        except IngestError as e:
            check = spotcheck.CardCheck(card=str(card), drive=drive, folder=folder,
                                        shoot_date=card.shoot_date.isoformat(), problems=[str(e)])
        progress.finish()
        checks.append(check)
        status = "PASS" if check.passed else "FAIL"
        p.say(f"  [{i}/{total}] {status}  {check.card}  {check.files_checked:,} files, "
              f"{format_bytes(check.bytes_checked)}  (hashes from the {check.source or 'n/a'})")
        for note in check.notes:
            if not note.startswith("LOG/RECORD DISAGREE"):
                p.say(f"      note: {note}")
        for w in check.warnings:
            p.say(f"      warning: {w}")

    if not args.no_record:
        for drive in dict.fromkeys(c.drive for c in checks):
            try:
                spotcheck.record_results(drive, [c for c in checks if c.drive == drive])
                export_xlsx(drive)
                p.say(f"  Recorded in the master log and spreadsheet on {drive.name}")
            except (IngestError, OSError, sqlite3.Error) as e:
                p.say(f"  warning: results not recorded on {drive.name}: {e}")

    failed = [c for c in checks if not c.passed]
    checked_bytes = sum(c.bytes_checked for c in checks)
    if failed:
        lines = [f"VERIFY FAILED: {len(failed)} of {total} card(s)", ""]
        for c in failed:
            lines.append(f"{c.card}  ({c.folder})")
            lines += [f"    {problem}" for problem in c.problems]
            lines += [f"    {f.status} {f.rel_path} {f.detail}".rstrip() for f in c.failures[:20]]
            if len(c.failures) > 20:
                lines.append(f"    ... and {len(c.failures) - 20} more")
            lines += [f"    missing folder {d}" for d in c.missing_dirs]
        p.warn_loud(lines)
        return EXIT_FAIL
    p.say("\n" + "=" * 72)
    p.say(f"  PASS  {total} card(s) verified, {format_bytes(checked_bytes)}")
    p.say("=" * 72)
    return EXIT_PASS


def _choose_operator(p: Prompter, operators: List[str], given: Optional[str]) -> str:
    """Operator from the config list, by name or number. Only one listed: chosen automatically."""
    def match(text: str) -> Optional[str]:
        text = text.strip()
        if text.isdigit() and 1 <= int(text) <= len(operators):
            return operators[int(text) - 1]
        return next((o for o in operators if o.lower() == text.lower()), None)

    if given:
        found = match(given)
        if found is None:
            raise ConfigError(f"Operator {given!r} is not in project.json. Operators: {', '.join(operators)}")
        return found
    if len(operators) == 1:
        return operators[0]
    p.say("Operators: " + ", ".join(f"{i}) {o}" for i, o in enumerate(operators, 1)))
    while True:
        found = match(p.ask("Operator (number or name)"))
        if found:
            return found


def _earlier_notes(items, drives) -> str:
    """Notes from the attempt being recopied: its marker, or else its card record."""
    for item in items:
        if item.marker.notes:
            return item.marker.notes
    for item in items:
        drive = next((d for d in drives if _is_within(item.path, d)), None)
        if drive is None:
            continue
        try:
            notes = read_card_record(card_record_path(drive, item.card)).get("notes", "")
        except (OSError, ValueError):
            continue
        if notes:
            return notes
    return ""


def _confirm_shoot_date(p: Prompter, given: Optional[str], times: List) -> date:
    proposal = propose_shoot_date(times)
    if times:
        p.say(f"  Clip dates: {min(times):%Y-%m-%d %H:%M} to {max(times):%Y-%m-%d %H:%M} "
              f"({len(times)} clip(s), times as written by the camera)")
    if given:
        chosen = parse_shoot_date(given)
        if proposal.proposed and chosen != proposal.proposed:
            p.say(f"  note: --date {chosen:%m%d%Y} differs from the earliest clip date {proposal.proposed:%m%d%Y}")
        return chosen

    default = None
    if proposal.spans_midnight:
        p.warn_loud([
            "Clips on this card fall on more than one date: "
            + ", ".join(f"{d:%m/%d/%Y}" for d in proposal.dates),
            "A shoot day can run past midnight. Type the shoot date this card belongs to.",
        ])
    elif proposal.proposed:
        default = f"{proposal.proposed:%m%d%Y}"
    else:
        p.say("  No clip dates found on this card. Type the shoot date.")
    while True:
        try:
            return parse_shoot_date(p.ask("Shoot date (MMDDYYYY)", default))
        except NamingError as e:
            p.say(f"  {e}")


def _ask_reel(p: Prompter, drives, show: str, shoot_date: date, cam: str, elsewhere=frozenset()) -> CardName:
    used = set(elsewhere).union(*(existing_reels(d, show, shoot_date, cam) for d in drives))
    while True:
        text = p.ask(f"Reel number (01-{MAX_REEL:02d})")
        try:
            card = CardName(show, shoot_date, cam, int(text))
        except (ValueError, NamingError):
            p.say("  Enter a number from 01 to 99")
            continue
        if card.reel in used:
            p.say(f"  {card} is already used (on these drives or another drive set)")
            continue
        return card


def _print_summary(p: Prompter, r: OffloadResult) -> None:
    lines = [
        f"{'PASS' if r.passed else 'FAIL'}  {r.card}  ({r.media_type})",
        f"Files:    {r.file_count:,}",
        f"Size:     {format_bytes(r.total_bytes)}",
        *([f"Copy:     {_hms(r.copy.elapsed)}  ({format_bytes(r.copy_speed)}/s)"] if r.copy else []),
        *([f"Verify:   {_hms(r.verify_seconds)}  (every destination re-read)"] if r.verify_seconds is not None else []),
        f"Total:    {_hms(r.elapsed)}",
    ]
    lines += [f"Report:   {path}" for path in r.reports if "_REPORTS" in path.parts]
    lines += [f"Day log:  {path}" for path in r.daily_logs]
    if r.warnings:
        p.warn_loud(["WARNING", *r.warnings])
    if r.passed:
        p.say("\n" + "=" * 72)
        for line in lines:
            p.say(f"  {line}")
        p.say("=" * 72)
        return
    detail = [*lines, ""]
    detail += [f"ERROR: {e}" for e in r.errors]
    for v in r.verifications:
        for c in v.failures[:20]:
            detail.append(f"{v.card_dir}: {c.status} {c.rel_path} {c.detail}".rstrip())
        if len(v.failures) > 20:
            detail.append(f"{v.card_dir}: ... and {len(v.failures) - 20} more")
        detail += [f"{v.card_dir}: missing folder {d}" for d in v.missing_dirs]
        detail += [f"{v.card_dir}: unexpected file {f}" for f in v.extra_files]
    detail += ["", f"Card {r.card} is marked FAILED. Run `mit ingest` again to recopy it as {r.card}."]
    p.warn_loud(["CARD FAILED VERIFICATION", *detail])


class _ProgressPrinter:
    def __init__(self, stream: TextIO, interval: float = 0.25):
        self.stream = stream
        self.interval = interval
        self.start = time.monotonic()
        self.last = 0.0
        self.active = False

    def __call__(self, done: int, total: int, rel_path: str) -> None:
        now = time.monotonic()
        if now - self.last < self.interval and done < total:
            return
        self.last = now
        pct = 100 * done / total if total else 100
        speed = done / max(now - self.start, 1e-6)
        line = f"  {format_bytes(done)} / {format_bytes(total)}  {pct:5.1f}%  {format_bytes(speed)}/s  {rel_path}"
        self.stream.write("\r" + line[:110].ljust(110))
        self.stream.flush()
        self.active = True

    def finish(self) -> None:
        if self.active:
            self.stream.write("\n")
            self.stream.flush()
            self.active = False


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
