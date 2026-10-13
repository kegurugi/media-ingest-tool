"""Per-card job report (HTML), built only from the card record.

Written to _REPORTS/<date>/<CARD>_report.html on every destination, and a copy
inside each card folder so the report travels with the media. The card-folder
copy is written once, after verification, and never regenerated.

The page is self-contained (inline styles, no scripts) and prints cleanly, so
File > Print > Save as PDF gives the PDF version.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .fileutil import atomic_write_text
from .space import format_bytes
from .timecode import end_timecode

OK = "OK"
STILLS_SUMMARY_THRESHOLD = 100


def report_filename(card: str) -> str:
    return f"{card}_report.html"


def write_report(path: Path, record: Dict[str, Any], generated_at: Optional[datetime] = None) -> None:
    atomic_write_text(path, render_card_report(record, generated_at))


# --- formatting helpers -----------------------------------------------------

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _datetime(iso: str) -> str:
    """'2026-08-14T13:29:01-08:00' -> '2026-08-14 13:29:01 -08:00' (as written; no conversion)."""
    if not iso:
        return ""
    text = iso.replace("T", " ")
    if len(text) > 19 and text[19] in "+-":
        text = f"{text[:19]} {text[19:]}"
    return text


def _path_breaks(path: str) -> str:
    """Escaped path that may only wrap after a slash."""
    return _e(path).replace("/", "/<wbr>")


def clip_end_tc(clip: Dict[str, Any]) -> str:
    """End TC from the record, or calculated for records made before it was stored."""
    return clip.get("end_timecode") or end_timecode(
        clip.get("start_timecode", ""), clip.get("duration"), clip.get("frame_rate"))


def _status_cell(status: str) -> str:
    if status == OK:
        return '<td class="ok">&#10003;</td>'
    return f'<td class="bad">{_e(status)}</td>'


# --- rendering --------------------------------------------------------------

def render_card_report(record: Dict[str, Any], generated_at: Optional[datetime] = None) -> str:
    generated_at = generated_at or datetime.now()
    card = record["card"]
    passed = record.get("result") == "PASS"
    volumes = [d["volume"] for d in record.get("destinations", [])]
    files = record.get("files", [])
    clips = record.get("clips", [])
    file_status = {f["path"]: f.get("verification", {}) for f in files}
    clip_paths = {c["rel_path"] for c in clips}
    failures = [f for f in files if any(s != OK for s in f.get("verification", {}).values())]

    parts: List[str] = [_HEAD.format(title=_e(f"{card} Job Report"), css=_CSS)]
    status = "PASS" if passed else "FAIL"
    parts.append(
        f'<header class="banner {status.lower()}"><div class="status">{status}</div>'
        f"<div><h1>{_e(card)}</h1><p>{_e(record.get('project_name', ''))}"
        f"{' &middot; ' if record.get('project_name') else ''}{_e(record.get('show_code', ''))}"
        f" &middot; {_e(record.get('media_type', ''))}</p></div></header>"
    )
    parts.append(_header_fields(record, volumes))

    if not passed:
        parts.append(_failure_section(record, failures, volumes))

    av = [c for c in clips if c.get("kind") != "still"]
    stills = [c for c in clips if c.get("kind") == "still"]
    if av:
        parts.append(f"<section><h2>Clips ({len(av)})</h2>{_clip_table(av, volumes, file_status)}</section>")
    if stills:
        parts.append(_stills_section(stills, volumes, file_status))
    if not clips:
        parts.append("<section><h2>Clips</h2><p class='muted'>No clips listed. "
                     "See the file list below.</p></section>")

    others = [f for f in files if f["path"] not in clip_paths]
    if others:
        parts.append(_other_files_section(others, volumes))

    parts.append(_totals_section(record, clips))
    parts.append(_notes_section(record, clips))
    parts.append(
        f"<footer>Media Ingest Tool {_e(record.get('tool_version', ''))} &middot; checksum "
        f"{_e(record.get('algorithm', 'xxh64'))} &middot; card record <code>{_e(card)}_card.json</code> "
        f"&middot; report generated {generated_at:%Y-%m-%d %H:%M:%S}</footer>"
    )
    parts.append("</body></html>\n")
    return "\n".join(parts)


def _header_fields(record: Dict[str, Any], volumes: List[str]) -> str:
    camera = record.get("camera", {})
    cam_text = ": ".join(p for p in (camera.get("id", ""), camera.get("model", "")) if p)
    extras = [camera.get(k) for k in ("description", "shooter") if camera.get(k)]
    if extras:
        cam_text += f" ({', '.join(extras)})"
    started = _datetime(record.get("offload_started", ""))
    finished = _datetime(record.get("offload_finished", ""))
    fields = [
        ("Project", record.get("project_name", "")),
        ("Show code", record.get("show_code", "")),
        ("Card", record["card"]),
        ("Camera", cam_text),
        ("Shoot date", _shoot_date(record.get("shoot_date", ""))),
        ("Offload", f"{started} to {finished[11:] if finished[:10] == started[:10] else finished}" if finished else started),
        ("Operator", record.get("operator", "")),
        ("Source volume", record.get("source_volume", "")),
        ("Destinations", " & ".join(volumes)),
        ("Separate drives", record.get("separate_drives", "")),
        ("Result", record.get("result", "")),
    ]
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in fields)
    return f'<section><dl class="fields">{rows}</dl></section>'


def _shoot_date(iso: str) -> str:
    try:
        return f"{datetime.fromisoformat(iso):%m/%d/%Y}"
    except ValueError:
        return iso


def _failure_section(record: Dict[str, Any], failures: List[Dict[str, Any]], volumes: List[str]) -> str:
    rows = []
    for f in failures:
        cells = "".join(_status_cell(f.get("verification", {}).get(v, "MISSING")) for v in volumes)
        rows.append(f"<tr class='failed'><td>{_e(f['path'])}</td><td class='num'>{_e(format_bytes(f['size']))}</td>{cells}</tr>")
    problems = [f"<li>{_e(e)}</li>" for e in record.get("errors", [])]
    for d in record.get("destinations", []):
        problems += [f"<li>{_e(d['volume'])}: missing folder {_e(x)}</li>" for x in d.get("missing_dirs", [])]
        problems += [f"<li>{_e(d['volume'])}: unexpected file {_e(x)}</li>" for x in d.get("extra_files", [])]
    head = "".join(f"<th>{_e(v)}</th>" for v in volumes)
    table = (f"<div class='table-wrap'><table><thead><tr><th>File</th><th>Size</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
             if rows else "")
    return (
        '<section class="failures"><h2>Verification failures</h2>'
        "<p>This card did <strong>not</strong> verify. Do not rely on these copies; recopy the card.</p>"
        + (f"<ul>{''.join(problems)}</ul>" if problems else "")
        + table
        + "</section>"
    )


def _clip_table(clips: List[Dict[str, Any]], volumes: List[str], file_status: Dict[str, Dict[str, str]]) -> str:
    head = "".join(f"<th>{_e(v)}</th>" for v in volumes)
    rows = []
    for i, c in enumerate(clips, 1):
        statuses = file_status.get(c["rel_path"], {})
        failed = any(s != OK for s in statuses.values())
        folder = c["rel_path"].rsplit("/", 1)[0] if "/" in c["rel_path"] else ""
        cells = "".join(_status_cell(statuses.get(v, "MISSING")) for v in volumes)
        rows.append(
            f"<tr{' class=failed' if failed else ''}><td class='num'>{i}</td>"
            f"<td><div class='name'>{_e(c['filename'])}</div><div class='path'>{_path_breaks(folder)}</div></td>"
            f"<td class='mono'>{_e(c.get('start_timecode'))}</td><td class='mono'>{_e(clip_end_tc(c))}</td>"
            f"<td class='mono'>{_e(hms(c.get('duration')))}</td>"
            f"<td>{_e(c.get('codec'))}</td><td>{_e(c.get('resolution'))}</td><td>{_e(c.get('frame_rate'))}</td>"
            f"<td class='nowrap'>{_e(_datetime(c.get('created', '')))}</td>"
            f"<td class='num'>{_e(format_bytes(c.get('size', 0)))}</td><td class='mono small'>{_e(c.get('xxh64'))}</td>"
            f"{cells}</tr>"
        )
    return (
        "<div class='table-wrap'><table class='clips'><thead><tr><th>#</th><th>Clip</th><th>Start TC</th><th>End TC</th><th>Duration</th><th>Codec</th>"
        f"<th>Resolution</th><th>FPS</th><th>Created</th><th>Size</th><th>xxHash64</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _stills_section(stills: List[Dict[str, Any]], volumes: List[str], file_status: Dict[str, Dict[str, str]]) -> str:
    if len(stills) <= STILLS_SUMMARY_THRESHOLD:
        return f"<section><h2>Stills ({len(stills)})</h2>{_clip_table(stills, volumes, file_status)}</section>"
    created = sorted(c["created"] for c in stills if c.get("created"))
    resolutions = sorted({c["resolution"] for c in stills if c.get("resolution")})
    verified = sum(1 for c in stills if all(s == OK for s in file_status.get(c["rel_path"], {}).values()))
    summary = [
        ("Stills", f"{len(stills):,}"),
        ("Verified on all destinations", f"{verified:,} of {len(stills):,}"),
        ("First / last", f"{_datetime(created[0])} / {_datetime(created[-1])}" if created else ""),
        ("Resolution", ", ".join(resolutions)),
        ("Total size", format_bytes(sum(c.get("size", 0) for c in stills))),
    ]
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in summary)
    return (
        f"<section><h2>Stills ({len(stills):,})</h2><dl class='fields'>{rows}</dl>"
        f"<details class='no-print'><summary>Show all {len(stills):,} stills</summary>"
        f"{_clip_table(stills, volumes, file_status)}</details></section>"
    )


def _other_files_section(others: List[Dict[str, Any]], volumes: List[str]) -> str:
    head = "".join(f"<th>{_e(v)}</th>" for v in volumes)
    rows = "".join(
        f"<tr><td>{_e(f['path'])}</td><td class='num'>{_e(format_bytes(f['size']))}</td>"
        f"<td class='mono small'>{_e(f.get('xxh64'))}</td>"
        + "".join(_status_cell(f.get("verification", {}).get(v, "MISSING")) for v in volumes)
        + "</tr>"
        for f in others
    )
    failed = sum(1 for f in others if any(s != OK for s in f.get("verification", {}).values()))
    return (
        f"<section><h2>Other files ({len(others):,})</h2>"
        f"<p class='muted'>Sidecars, proxies, thumbnails and camera system files: "
        f"{format_bytes(sum(f['size'] for f in others))}, "
        f"{'all verified' if not failed else f'{failed} failed (listed above)'}.</p>"
        f"<details class='no-print'><summary>Show file list</summary><div class='table-wrap'><table><thead><tr><th>File</th><th>Size</th>"
        f"<th>xxHash64</th>{head}</tr></thead><tbody>{rows}</tbody></table></div></details></section>"
    )


def _totals_section(record: Dict[str, Any], clips: List[Dict[str, Any]]) -> str:
    av = [c for c in clips if c.get("kind") != "still"]
    durations = [c["duration"] for c in av if c.get("duration") is not None]
    missing = len(av) - len(durations)
    duration = hms(sum(durations)) + (f" ({missing} clip(s) without a duration)" if missing else "") if av else ""
    speed = record.get("transfer_speed")
    fields = [
        ("Clips", f"{len(clips):,}"),
        ("Files", f"{record.get('file_count', 0):,}"),
        ("Total size", format_bytes(record.get("total_bytes", 0))),
        ("Total duration", duration),
    ]
    if record.get("verify_seconds") is not None and record.get("copy_seconds") is not None:
        fields += [
            ("Copy", f"{hms(record['copy_seconds'])}" + (f" ({format_bytes(speed)}/s)" if speed else "")),
            ("Verify", f"{hms(record['verify_seconds'])} (every destination re-read)"),
            ("Total time", hms(record.get("elapsed_seconds"))),
        ]
    else:  # records from before the copy/verify breakdown was kept
        fields += [
            ("Elapsed", hms(record.get("elapsed_seconds"))),
            ("Transfer speed", f"{format_bytes(speed)}/s" if speed else ""),
        ]
    skipped = record.get("skipped", [])
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in fields)
    skipped_html = (
        f"<p class='muted'>Skipped macOS system items ({len(skipped)}): {_e(', '.join(skipped))}</p>" if skipped else ""
    )
    return f"<section><h2>Totals</h2><dl class='fields'>{rows}</dl>{skipped_html}</section>"


def _notes_section(record: Dict[str, Any], clips: List[Dict[str, Any]]) -> str:
    notes = record.get("notes", "")
    body = f"<p class='notes'>{_e(notes)}</p>" if notes else "<p class='muted'>No notes.</p>"
    clip_notes = [(c["filename"], n) for c in clips for n in c.get("notes", [])]
    if clip_notes:
        body += "<h3>Metadata notes</h3><ul>" + "".join(
            f"<li><strong>{_e(name)}</strong>: {_e(n)}</li>" for name, n in clip_notes) + "</ul>"
    return f"<section><h2>Notes</h2>{body}</section>"


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style></head><body>"""

_CSS = """
:root { --ink:#1b1d21; --muted:#5f6670; --line:#d9dce1; --bg:#ffffff; --zebra:#f6f7f9;
        --pass:#1d7a3e; --pass-bg:#e6f4ea; --fail:#b3261e; --fail-bg:#fdecea; }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:1400px; padding:24px 16px 48px; background:var(--bg); color:var(--ink);
       font:13px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
h1 { margin:0; font-size:26px; letter-spacing:.5px; }
h2 { font-size:16px; margin:28px 0 8px; padding-bottom:4px; border-bottom:2px solid var(--line); }
h3 { font-size:13px; margin:16px 0 4px; }
.banner { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center; padding:16px 20px; border-radius:8px; }
h1 { overflow-wrap:anywhere; }
.table-wrap { overflow-x:auto; }
.banner p { margin:4px 0 0; color:var(--muted); }
.banner .status { font-size:30px; font-weight:800; padding:6px 18px; border-radius:6px; color:#fff; }
.banner.pass { background:var(--pass-bg); } .banner.pass .status { background:var(--pass); }
.banner.fail { background:var(--fail-bg); } .banner.fail .status { background:var(--fail); }
.fields { display:grid; grid-template-columns:repeat(auto-fill, minmax(230px, 1fr)); gap:8px 24px; margin:16px 0 0; }
.fields dt { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); }
.fields dd { margin:0; font-weight:600; word-break:break-word; }
table { width:100%; border-collapse:collapse; margin-top:8px; }
th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:var(--muted); position:sticky; top:0; background:var(--bg); }
tbody tr:nth-child(even) { background:var(--zebra); }
td.num { text-align:right; white-space:nowrap; } .nowrap { white-space:nowrap; }
.mono { font-family:"SF Mono", Menlo, Consolas, monospace; white-space:nowrap; }
.small { font-size:11px; }
.name { font-weight:600; white-space:nowrap; } .path { color:var(--muted); font-size:11px; white-space:nowrap; }
td.ok { color:var(--pass); font-weight:700; text-align:center; }
tr.failed td { background:var(--fail-bg); }
td.bad, tr.failed td.bad { color:#fff; background:var(--fail); font-weight:700; text-align:center; }
.failures { border:2px solid var(--fail); border-radius:8px; padding:4px 16px 16px; margin-top:20px; }
.failures h2 { color:var(--fail); border-color:var(--fail); }
.muted { color:var(--muted); }
.notes { white-space:pre-wrap; background:var(--zebra); padding:10px 12px; border-radius:6px; }
details summary { cursor:pointer; color:var(--muted); margin-top:6px; }
footer { margin-top:32px; color:var(--muted); font-size:11px; border-top:1px solid var(--line); padding-top:8px; }
@media print {
  @page { size:landscape; margin:12mm; }
  body { max-width:none; padding:0; font-size:9.5px; }
  .no-print { display:none; }
  th { position:static; }
  .table-wrap { overflow:visible; }
  .path { white-space:normal; }
  tr { break-inside:avoid; }
  .banner, td.bad, tr.failed td, .banner .status { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}
"""


def iter_card_records(reports_dir: Path) -> Iterable[Path]:
    return sorted(Path(reports_dir).glob("*_card.json"))
