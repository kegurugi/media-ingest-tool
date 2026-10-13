import contextlib
import re
import shutil
import sqlite3
import subprocess
from datetime import date, datetime
from html import unescape
from pathlib import Path

import pytest

from conftest import SMALL_CHUNK, save_day_details, write_card
from media_ingest import offload as offload_mod
from media_ingest.cli import main
from media_ingest.daily_log import CardLine, DayData, DriveTotals, gather, render, write_daily_log
from media_ingest.daydetails import DayDetails, project_days_dir, write_day
from media_ingest.history import history_path
from media_ingest.incomplete import find_incomplete_all, quarantine
from media_ingest.naming import CardName
from media_ingest.offload import JobInfo, run_offload

OCT12 = date(2026, 10, 12)
CAMS = {
    "A": dict(camera_model="Sony FX6", camera_shooter="Sam"),
    "D": dict(camera_model="Timelapse", camera_description="Timelapse", camera_shooter="Shooter Name"),
    "DR1": dict(camera_model="Drone 1"),
}


def text_of(html):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html.split("</style>", 1)[-1])))


@pytest.fixture
def project(tmp_path):
    config = tmp_path / "project" / "project.json"
    config.parent.mkdir()
    config.write_text((Path(__file__).resolve().parent.parent / "project.example.json").read_text())
    return config


def offload(source, drives, name, project, notes="", **kw):
    card = CardName.parse(name)
    job = JobInfo(project_name="Example Show", show_code="ABC", camera_id=card.cam, notes=notes, **CAMS[card.cam])
    return run_offload(source, drives, card, "VIDEO", allow_same_volume=True, chunk_size=SMALL_CHUNK, job=job,
                       project_history=history_path(project), **kw)


@pytest.fixture
def day(drives, tmp_path, project):
    """A shoot day: two A cards, a timelapse D card, a drone card; plus an A card the next day."""
    cards = {
        "ABC261012A01": write_card(tmp_path / "a1", {"CLIP/C0001.MP4": b"a" * 2_000_000, "CLIP/C0002.MP4": b"b" * 1000}),
        "ABC261012A02": write_card(tmp_path / "a2", {"CLIP/C0001.MP4": b"c" * 3_000_000}),
        "ABC261012D01": write_card(tmp_path / "d1", {f"DCIM/100/DSC{i:05d}.ARW": bytes([i]) * 1000 for i in range(5)}),
        "ABC261012DR101": write_card(tmp_path / "dr", {"DCIM/DJI_0001.MP4": b"d" * 500_000}),
        "ABC261013A01": write_card(tmp_path / "next", {"CLIP/C0001.MP4": b"e" * 1_000_000}),
    }
    write_day(drives, DayDetails(shoot_date="2026-10-12", header_fields=[("Season", "12"), ("Location", "Base Camp"), ("Leg", "1")],
                                 crew=[("Producer", "Pat"), ("DP", "Sam"), ("DIT", "Dwayne")], day_notes="Wind <40 kt> all day"),
              project_days_dir(project))
    for name, src in cards.items():
        r = offload(src, drives, name, project, notes="ejected mid-clip 0002" if name == "ABC261012A02" else "")
        assert r.passed, r.errors
    return cards


def test_written_on_every_drive_after_offload(day, drives):
    for d in drives:
        assert (d / "_REPORTS/10122026/10122026_daily_card_log.html").exists()
        assert (d / "_REPORTS/10132026/10132026_daily_card_log.html").exists()


def test_page_contents(day, drives):
    page = (drives[0] / "_REPORTS/10122026/10122026_daily_card_log.html").read_text()
    text = text_of(page)
    for expected in [
        "Daily Card Log", "Example Show", "Monday, October 12, 2026", "10/12/2026", "PASS",
        "Season 12", "Location Base Camp", "Leg 1", "Producer Pat", "DP Sam", "DIT Dwayne",
        "Drives SHOW01_DRV01A & SHOW01_DRV01B",
        "Cam A: 2 Cards, 5.00 MB (Sony FX6, Sam)",
        "Cam D: 1 Card, 5.00 KB (Timelapse, Shooter Name)",
        "Cam DR1: 1 Card, 500.00 KB (Drone 1)",
        "ABC261012A01 VIDEO 2 clips 2.00 MB PASS", "ABC261012D01 VIDEO 5 stills",
        "Total cards 4", "Total GB for the day 5.51 MB",
        "ABC261012A02 : ejected mid-clip 0002", "Wind <40 kt> all day",
    ]:
        assert expected in text, expected
    assert "ABC261013A01" not in text  # the next day's card
    assert "&lt;40 kt&gt;" in page and "<script" not in page
    assert "Check:" not in text  # drives agree


def test_running_and_project_totals(day, drives):
    # Totals run through the page's own date, so 10/12 doesn't change when 10/13 is added.
    text = text_of((drives[0] / "_REPORTS/10122026/10122026_daily_card_log.html").read_text())
    assert "SHOW01_DRV01A 5.51 MB" in text and "SHOW01_DRV01B 5.51 MB" in text
    assert "Project total, all drives (through this date) 5.51 MB" in text
    text = text_of((drives[0] / "_REPORTS/10132026/10132026_daily_card_log.html").read_text())
    assert "SHOW01_DRV01A 6.51 MB" in text and "Project total, all drives (through this date) 6.51 MB" in text


def test_failed_card_makes_the_day_fail(day, drives, tmp_path, project, monkeypatch):
    real = offload_mod.copy_card

    def corrupt(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[1] / "CLIP/C0001.MP4").write_bytes(b"x")
        return r

    monkeypatch.setattr(offload_mod, "copy_card", corrupt)
    offload(write_card(tmp_path / "a3", {"CLIP/C0001.MP4": b"f" * 1000}), drives, "ABC261012A03", project)
    text = text_of((drives[0] / "_REPORTS/10122026/10122026_daily_card_log.html").read_text())
    assert " FAIL " in text and "Failed cards: ABC261012A03" in text
    assert "Cam A: 3 Cards" in text

    # Recopied: the day passes again and A03 is counted once.
    monkeypatch.setattr(offload_mod, "copy_card", real)
    for item, d in zip(find_incomplete_all(drives, "ABC", OCT12, "A")["ABC261012A03"], drives):
        quarantine(item, d)
    offload(tmp_path / "a3", drives, "ABC261012A03", project)
    text = text_of((drives[0] / "_REPORTS/10122026/10122026_daily_card_log.html").read_text())
    assert " PASS " in text and "Failed cards" not in text and "Cam A: 3 Cards" in text and "Total cards 5" in text


def test_drives_that_disagree_are_flagged(day, drives):
    with contextlib.closing(sqlite3.connect(drives[1] / "_LOGS/media_log.sqlite")) as conn:
        conn.execute("DELETE FROM offloads WHERE card = 'ABC261012DR101'")
        conn.commit()
    data = gather(drives, OCT12)
    assert "ABC261012DR101 is missing from the log on SHOW01_DRV01B." in data.flags
    assert any("disagree on the card count for the day: SHOW01_DRV01A 4, SHOW01_DRV01B 3" in f for f in data.flags)
    assert any("running total on the drive" in f for f in data.flags)


def test_disconnected_drive_is_noted(day, drives, tmp_path):
    data = gather([drives[0], tmp_path / "SHOW01_DRV01B_gone"], OCT12)
    assert "SHOW01_DRV01B_gone is not connected, so its totals weren't cross-checked." in data.flags
    assert len(data.cards) == 4


def test_camera_d_and_dr1_kept_apart(day, drives):
    cams = dict(gather(drives, OCT12).by_camera())
    assert [c.card for c in cams["D"]] == ["ABC261012D01"]
    assert [c.card for c in cams["DR1"]] == ["ABC261012DR101"]


def test_mit_report_and_daylog_rebuild_the_log(day, drives, project, monkeypatch, capsys):
    log = drives[0] / "_REPORTS/10122026/10122026_daily_card_log.html"
    log.write_text("old")
    dest = ["--dest", str(drives[0]), "--dest", str(drives[1])]
    assert main(["--config", str(project), "report", "10122026", *dest]) == 0
    assert "Daily Card Log" in log.read_text()

    assert main(["--config", str(project), "daylog", "10122026", *dest, "--notes", "Blizzard, wrapped early"]) == 0
    assert "Blizzard, wrapped early" in text_of(log.read_text())

    # Interactive edit: change Location, remove Leg, add Unit; replace the DP; keep the notes.
    answers = iter(["y", "", "River Camp", "-", "Unit", "2", "", "", "Alex", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert main(["--config", str(project), "daylog", "10122026", *dest]) == 0
    text = text_of(log.read_text())
    assert "Location River Camp" in text and "Leg 1" not in text and "Unit 2" in text and "DP Alex" in text
    assert "Blizzard, wrapped early" in text


def test_one_printed_page_for_a_busy_day(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("needs Google Chrome for print-to-PDF")
    day = DayData(shoot_date=OCT12, show_name="Example Show", show_code="ABC",
                  drives=[DriveTotals(volume=v, path=tmp_path, mounted=True, has_log=True, running_bytes=2 * 10**12,
                                      capacity=8 * 10**12, free=6 * 10**12) for v in ("SHOW01_DRV01A", "SHOW01_DRV01B")])
    for cam, n in (("A", 6), ("B", 5), ("C", 3), ("D", 1), ("DR1", 2), ("S", 3)):
        for reel in range(1, n + 1):
            name = f"ABC261012{cam}{reel:02d}"
            day.cards[name] = CardLine(name, cam, "Sony FX6", "Main", "Sam", "VIDEO", 42, 0, 128 * 10**9, "PASS",
                                       "B-cam interviews, card ejected mid-clip 0032" if reel == 2 else "", "")
    details = DayDetails(shoot_date="2026-10-12", header_fields=[("Season", "12"), ("Location", "Base Camp"), ("Leg", "1")],
                         crew=[("Producer", "Pat"), ("DP", "Sam"), ("DIT", "Dwayne")], day_notes="Long day.")
    html_path, pdf = tmp_path / "day.html", tmp_path / "day.pdf"
    html_path.write_text(render(day, details))
    subprocess.run([str(chrome), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", html_path.as_uri()], capture_output=True, timeout=60)
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))
    assert pages == 1, f"{len(day.cards)} cards printed on {pages} pages"
