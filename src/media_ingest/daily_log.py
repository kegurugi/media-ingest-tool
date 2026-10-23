"""Daily card log: one page per shoot date, replacing the hand-written card log.

Built from each destination's master log and the day details, then written to
_REPORTS/<date>/<date>_daily_card_log.html on every destination. Rebuilt after
every offload for that date, by `mit report <date>` and by `mit daylog <date>`.

It covers the cards on the given drives. If a day is split across drive sets,
each set gets its own daily log for the cards it holds.
"""
from __future__ import annotations

import contextlib
import html
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .daydetails import DayDetails, read_day
from .errors import IngestError
from .fileutil import atomic_write_text
from .layout import daily_log_path, master_log_path
from .masterlog import connect
from .space import format_bytes


@dataclass
class CardLine:
    card: str
    camera_id: str
    camera_model: str
    description: str
    shooter: str
    media_type: str
    clips: int
    stills: int
    total_bytes: int
    result: str
    notes: str
    offload_started: str
    volumes: List[str] = field(default_factory=list)


@dataclass
class DriveTotals:
    volume: str
    path: Path
    mounted: bool
    has_log: bool = False
    day_cards: int = 0
    day_bytes: int = 0
    running_bytes: int = 0
    capacity: Optional[int] = None
    free: Optional[int] = None
    project_bytes: Optional[int] = None
    cards: Dict[str, Tuple[int, str]] = field(default_factory=dict)  # card -> (bytes, result)


@dataclass
class DayData:
    shoot_date: date
    show_name: str = ""
    show_code: str = ""
    drives: List[DriveTotals] = field(default_factory=list)
    cards: Dict[str, CardLine] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.cards) and all(c.result == "PASS" for c in self.cards.values())

    @property
    def failed_cards(self) -> List[str]:
        return sorted(c.card for c in self.cards.values() if c.result != "PASS")

    @property
    def total_bytes(self) -> int:
        return sum(c.total_bytes for c in self.cards.values())

    def by_camera(self) -> List[Tuple[str, List[CardLine]]]:
        cams: Dict[str, List[CardLine]] = {}
        for c in sorted(self.cards.values(), key=lambda c: (c.camera_id, c.card)):
            cams.setdefault(c.camera_id, []).append(c)
        return list(cams.items())


def gather(drives: Iterable[Path], shoot_date: date) -> DayData:
    """Read the day from each drive's master log and cross-check the drives."""
    day = DayData(shoot_date=shoot_date)
    iso = shoot_date.isoformat()
    for path in (Path(d) for d in drives):
        totals = DriveTotals(volume=path.name, path=path, mounted=path.is_dir())
        day.drives.append(totals)
        if not totals.mounted:
            day.flags.append(f"{path.name} is not connected, so its totals weren't cross-checked.")
            continue
        usage = shutil.disk_usage(path)
        totals.capacity, totals.free = usage.total, usage.free
        if not master_log_path(path).exists():
            day.flags.append(f"{path.name} has no master log.")
            continue
        try:
            with contextlib.closing(connect(path)) as conn:
                _read_drive(conn, totals, day, iso)
        except (sqlite3.Error, IngestError) as e:
            day.flags.append(f"{path.name}: master log could not be read ({e}).")
    _cross_check(day)
    return day


def _read_drive(conn: sqlite3.Connection, totals: DriveTotals, day: DayData, iso: str) -> None:
    totals.has_log = True
    # Totals run up to and including this shoot date, so each day's page keeps the same
    # figures however many later days are added (matching the spreadsheet's Days sheet).
    totals.running_bytes = conn.execute(
        "SELECT COALESCE(SUM(total_bytes), 0) FROM offloads WHERE superseded = 0 AND shoot_date <= ?",
        (iso,)).fetchone()[0]
    if conn.execute("SELECT COUNT(*) FROM project_summary").fetchone()[0]:
        totals.project_bytes = conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM project_summary WHERE shoot_date <= ?", (iso,)).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM offloads WHERE shoot_date = ? AND superseded = 0 ORDER BY camera_id, card", (iso,)).fetchall()
    for r in rows:
        kinds = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM clips WHERE offload_id = ? GROUP BY kind", (r["id"],)).fetchall())
        totals.cards[r["card"]] = (r["total_bytes"] or 0, r["result"])
        totals.day_cards += 1
        totals.day_bytes += r["total_bytes"] or 0
        line = day.cards.get(r["card"])
        if line is None or r["offload_started"] > line.offload_started:
            line = CardLine(
                card=r["card"], camera_id=r["camera_id"], camera_model=r["camera_model"] or "",
                description=r["camera_description"] or "", shooter=r["camera_shooter"] or "",
                media_type=r["media_type"] or "", clips=kinds.get("video", 0) + kinds.get("audio", 0),
                stills=kinds.get("still", 0), total_bytes=r["total_bytes"] or 0, result=r["result"],
                notes=r["notes"] or "", offload_started=r["offload_started"],
                volumes=(day.cards[r["card"]].volumes if r["card"] in day.cards else []),
            )
            day.cards[r["card"]] = line
        line.volumes.append(totals.volume)
        if r["project_name"]:
            day.show_name = r["project_name"]
        if r["show_code"]:
            day.show_code = r["show_code"]


def _cross_check(day: DayData) -> None:
    logged = [d for d in day.drives if d.has_log]
    if len(logged) < 2:
        return
    for card in sorted(day.cards):
        missing = [d.volume for d in logged if card not in d.cards]
        if missing:
            day.flags.append(f"{card} is missing from the log on {', '.join(missing)}.")
    for label, get, fmt in (
        ("card count for the day", lambda d: d.day_cards, str),
        ("GB for the day", lambda d: d.day_bytes, format_bytes),
        ("running total on the drive", lambda d: d.running_bytes, format_bytes),
    ):
        values = {d.volume: get(d) for d in logged}
        if len(set(values.values())) > 1:
            day.flags.append(f"Destinations disagree on the {label}: "
                             + ", ".join(f"{v} {fmt(x)}" for v, x in values.items()) + ".")


# --- rendering --------------------------------------------------------------

def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def camera_heading(cam: str, cards: List[CardLine]) -> str:
    """'Cam D: 1 Card, 64.10 GB (Timelapse, Shooter Name)' as in the spec."""
    first = cards[-1]
    extras = [first.description or first.camera_model, first.shooter]
    extras_text = ", ".join(x for x in extras if x)
    n = len(cards)
    text = f"Cam {cam}: {n} Card{'s' if n != 1 else ''}, {format_bytes(sum(c.total_bytes for c in cards))}"
    return text + (f" ({extras_text})" if extras_text else "")


def render(day: DayData, details: Optional[DayDetails], generated_at: Optional[datetime] = None) -> str:
    generated_at = generated_at or datetime.now()
    status = "PASS" if day.passed else ("NO CARDS" if not day.cards else "FAIL")
    volumes = " & ".join(d.volume for d in day.drives)
    parts = [_HEAD.format(title=_e(f"{day.shoot_date:%m%d%Y} Daily Card Log"), css=_CSS)]

    fields = list(details.header_fields) if details else []
    crew = list(details.crew) if details else []
    parts.append(
        "<header><div class='titles'><p class='kicker'>Daily Card Log</p>"
        f"<h1>{_e(day.show_name or day.show_code)}</h1>"
        f"<p class='date'>{day.shoot_date:%A, %B} {day.shoot_date.day}, {day.shoot_date.year} "
        f"&middot; {day.shoot_date:%m/%d/%Y}</p></div>"
        f"<div class='status {status.lower().replace(' ', '-')}'>{status}</div></header>"
    )
    info = [(k, v) for k, v in fields] + [("Drives", volumes)]
    parts.append("<dl class='fields'>" + "".join(
        f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in info) + "</dl>")
    if crew:
        parts.append("<dl class='fields crew'>" + "".join(
            f"<div><dt>{_e(r)}</dt><dd>{_e(n)}</dd></div>" for r, n in crew) + "</dl>")

    if day.failed_cards:
        parts.append("<section class='alert'><strong>Failed cards:</strong> "
                     + ", ".join(_e(c) for c in day.failed_cards)
                     + ". Do not rely on these copies; recopy the cards.</section>")
    if day.flags:
        parts.append("<section class='alert'><strong>Check:</strong><ul>"
                     + "".join(f"<li>{_e(f)}</li>" for f in day.flags) + "</ul></section>")

    parts.append("<h2>Cameras</h2>")
    if not day.cards:
        parts.append("<p class='muted'>No cards logged for this date on these drives.</p>")
    parts.append("<div class='cameras'>")
    for cam, cards in day.by_camera():
        rows = "".join(
            f"<tr{' class=failed' if c.result != 'PASS' else ''}><td class='mono'>{_e(c.card)}</td>"
            f"<td>{_e(c.media_type)}</td>"
            f"<td class='num'>{_e(_media_count(c))}</td>"
            f"<td class='num'>{_e(format_bytes(c.total_bytes))}</td>"
            f"<td class='{'ok' if c.result == 'PASS' else 'bad'}'>{_e(c.result)}</td></tr>"
            for c in cards
        )
        parts.append(f"<div class='camera'><h3>{_e(camera_heading(cam, cards))}</h3>"
                     f"<table><tbody>{rows}</tbody></table></div>")
    parts.append("</div>")

    parts.append("<h2>Totals</h2>")
    parts.append("<dl class='fields'>"
                 f"<div><dt>Total cards</dt><dd>{len(day.cards)}</dd></div>"
                 f"<div><dt>Total for the day</dt><dd>{_e(format_bytes(day.total_bytes))}</dd></div>"
                 + _project_total(day) + "</dl>")
    drive_rows = "".join(
        f"<tr><td class='mono'>{_e(d.volume)}</td>"
        + (f"<td class='num'>{_e(format_bytes(d.running_bytes))}</td><td class='num'>{_e(format_bytes(d.capacity))}</td>"
           f"<td class='num'>{_e(format_bytes(d.free))}</td>" if d.mounted and d.capacity is not None
           else "<td colspan='3' class='muted'>not connected</td>")
        + "</tr>"
        for d in day.drives
    )
    parts.append("<table class='drives'><colgroup><col class='drive'><col><col><col></colgroup>"
                 "<thead><tr><th>Drive</th><th class='num'>Running total<br>(through this date)</th>"
                 f"<th class='num'>Capacity</th><th class='num'>Free<br>(at {generated_at:%m/%d %H:%M})</th></tr></thead>"
                 f"<tbody>{drive_rows}</tbody></table>")

    parts.append("<h2>Camera Notes</h2>")
    notes = [(c.card, c.notes) for _, cards in day.by_camera() for c in cards if c.notes]
    if notes:
        parts.append("<ul class='notes'>" + "".join(
            f"<li><span class='mono'>{_e(card)}</span>: {_e(n)}</li>" for card, n in notes) + "</ul>")
    else:
        parts.append("<p class='muted'>No card notes.</p>")
    parts.append("<h3>Day notes</h3>")
    day_notes = details.day_notes if details else ""
    parts.append(f"<p class='daynotes'>{_e(day_notes)}</p>" if day_notes else "<p class='daynotes empty'>&nbsp;</p>")

    parts.append(f"<footer>Media Ingest Tool &middot; built from the master logs on {_e(volumes)} "
                 f"&middot; generated {generated_at:%Y-%m-%d %H:%M:%S}</footer></body></html>\n")
    return "\n".join(parts)


def _media_count(c: CardLine) -> str:
    parts = []
    if c.clips:
        parts.append(f"{c.clips:,} clip{'s' if c.clips != 1 else ''}")
    if c.stills:
        parts.append(f"{c.stills:,} still{'s' if c.stills != 1 else ''}")
    return ", ".join(parts) or "0 clips"


def _project_total(day: DayData) -> str:
    values = [d.project_bytes for d in day.drives if d.project_bytes is not None]
    if not values:
        return ""
    return f"<div><dt>Project total, all drives (through this date)</dt><dd>{_e(format_bytes(max(values)))}</dd></div>"


def write_daily_log(drives: Iterable[Path], shoot_date: date, project_dir: Optional[Path] = None,
                    now: Optional[datetime] = None) -> Tuple[List[Path], List[str]]:
    """Build the day's log and write it to every mounted drive. Returns (paths written, warnings)."""
    drives = [Path(d) for d in drives]
    day = gather(drives, shoot_date)
    details, detail_notes = read_day(drives, shoot_date, project_dir)
    day.flags.extend(detail_notes)
    page = render(day, details, now)
    written, warnings = [], []
    for d in drives:
        if not d.is_dir():
            continue
        path = daily_log_path(d, shoot_date)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, page)
            written.append(path)
        except OSError as e:
            warnings.append(f"Could not write the daily card log on {d}: {e}")
    return written, warnings


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style></head><body>"""

_CSS = """
:root { --ink:#1b1d21; --muted:#5f6670; --line:#d9dce1; --bg:#ffffff; --zebra:#f6f7f9;
        --pass:#1d7a3e; --pass-bg:#e6f4ea; --fail:#b3261e; --fail-bg:#fdecea; }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:900px; padding:24px 16px 40px; background:var(--bg); color:var(--ink);
       font:13px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
header { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:flex-start; gap:12px;
         border-bottom:3px solid var(--ink); padding-bottom:10px; }
.kicker { margin:0; font-size:11px; letter-spacing:1.5px; text-transform:uppercase; color:var(--muted); }
h1 { margin:2px 0; font-size:24px; overflow-wrap:anywhere; }
.date { margin:0; font-size:15px; font-weight:600; }
.status { font-size:22px; font-weight:800; padding:6px 16px; border-radius:6px; color:#fff; background:var(--muted); }
.status.pass { background:var(--pass); } .status.fail { background:var(--fail); }
h2 { font-size:15px; margin:20px 0 6px; padding-bottom:3px; border-bottom:2px solid var(--line); }
h3 { font-size:13px; margin:12px 0 4px; }
.fields { display:grid; grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); gap:6px 20px; margin:12px 0 0; }
.fields dt { font-size:10px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); }
.fields dd { margin:0; font-weight:600; overflow-wrap:anywhere; }
.crew { padding-top:8px; border-top:1px dashed var(--line); }
.alert { border:2px solid var(--fail); background:var(--fail-bg); color:var(--fail); border-radius:6px;
         padding:8px 12px; margin-top:14px; }
.alert ul { margin:4px 0 0; padding-left:18px; }
.camera { break-inside:avoid; }
.camera h3 { margin-top:10px; }
table { width:100%; border-collapse:collapse; }
td, th { padding:3px 6px; border-bottom:1px solid var(--line); text-align:left; }
th { font-size:10px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); }
.camera table td:first-child { padding-left:18px; }
td.num, th.num { text-align:right; white-space:nowrap; }
.drives { table-layout:fixed; } .drives col { width:20%; } .drives col.drive { width:40%; }
.drives th { vertical-align:bottom; }
td.ok { color:var(--pass); font-weight:700; } td.bad { color:#fff; background:var(--fail); font-weight:700; }
tr.failed td { background:var(--fail-bg); } tr.failed td.bad { background:var(--fail); }
.mono { font-family:"SF Mono", Menlo, Consolas, monospace; }
.drives { margin-top:8px; }
.muted { color:var(--muted); }
.notes { margin:4px 0; padding-left:18px; }
.daynotes { white-space:pre-wrap; background:var(--zebra); padding:8px 10px; border-radius:6px; min-height:2.5em; }
footer { margin-top:24px; color:var(--muted); font-size:10px; border-top:1px solid var(--line); padding-top:6px; }
@media print {
  @page { size:portrait; margin:10mm 12mm; }
  body { max-width:none; padding:0; font-size:9.5px; line-height:1.3; }
  .cameras { column-count:2; column-gap:22px; }
  .camera h3 { margin:6px 0 2px; font-size:10.5px; }
  td, th { padding:1px 4px; }
  .camera table td:first-child { padding-left:8px; }
  h2 { margin:12px 0 4px; font-size:12.5px; }
  .fields { margin-top:8px; gap:3px 16px; }
  h1 { font-size:20px; } .status { font-size:18px; padding:4px 12px; }
  footer { margin-top:12px; }
  .status, .alert, td.bad, tr.failed td { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}
"""
