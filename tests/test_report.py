import copy
import json
import re
from html import unescape
from datetime import datetime

import pytest

from conftest import SMALL_CHUNK, write_card
from media_ingest import offload
from media_ingest.cli import main
from media_ingest.incomplete import find_incomplete_all, quarantine
from media_ingest.naming import CardName
from media_ingest.offload import JobInfo, run_offload
from media_ingest.records import read_card_record
from media_ingest.report import STILLS_SUMMARY_THRESHOLD, render_card_report

A01 = CardName.parse("ABC261012A01")
JOB = JobInfo(project_name="Example Show", show_code="ABC", camera_id="A", camera_model="Sony FX6",
              camera_description="Main cam", camera_shooter="Sam", operator="DIT Name",
              notes="B-cam interviews, card ejected mid-clip 0032")


def offload_card(source, drives, **kw):
    return run_offload(source, drives, A01, "VIDEO", allow_same_volume=True, chunk_size=SMALL_CHUNK, job=JOB, **kw)


@pytest.fixture
def passed(sample_card, drives):
    r = offload_card(sample_card, drives)
    assert r.passed
    return r, read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")


def text_of(html):
    body = html.split("</style>", 1)[-1]
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", body)))


def test_record_has_job_details(passed):
    _, record = passed
    assert record["project_name"] == "Example Show" and record["operator"] == "DIT Name"
    assert record["camera"] == {"id": "A", "model": "Sony FX6", "description": "Main cam", "shooter": "Sam"}
    assert record["notes"].startswith("B-cam interviews")
    assert record["offload_finished"] and record["elapsed_seconds"] > 0 and record["transfer_speed"] > 0


def test_report_written_to_reports_and_card_folders(passed, drives):
    r, _ = passed
    for d in drives:
        assert (d / "_REPORTS/10122026/ABC261012A01_report.html").exists()
        assert (d / "10122026/VIDEO/ABC261012A01/ABC261012A01_report.html").exists()
    assert len(r.reports) == 4 and not r.warnings


def test_report_contents(passed):
    _, record = passed
    html = render_card_report(record, generated_at=datetime(2026, 10, 12, 20, 0))
    text = text_of(html)
    assert html.startswith("<!doctype html>") and "<script" not in html
    for expected in ["PASS", "ABC261012A01", "Example Show", "ABC", "A: Sony FX6 (Main cam, Sam)", "10/12/2026",
                     "DIT Name", "A001", "SHOW01_DRV01A & SHOW01_DRV01B", "Clips (2)", "C0001.MP4",
                     "PRIVATE/M4ROOT/CLIP", "B-cam interviews, card ejected mid-clip 0032", "Other files (3)",
                     "Total size", "Transfer speed", "report generated 2026-10-12 20:00:00", ".DS_Store"]:
        assert expected in text, expected
    assert html.count('<td class="ok">') == 4 + 6  # 2 clips + 3 other files, x 2 destinations
    assert "Verification failures" not in html


def test_failed_report(sample_card, drives, monkeypatch):
    real = offload.copy_card

    def corrupting(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[1] / "PRIVATE/M4ROOT/CLIP/C0002.MP4").write_bytes(b"x" * 1_500_001)
        return r

    monkeypatch.setattr(offload, "copy_card", corrupting)
    assert not offload_card(sample_card, drives).passed
    html = (drives[0] / "_REPORTS/10122026/ABC261012A01_report.html").read_text()
    assert 'class="banner fail"' in html and "Verification failures" in html
    failures = html.split("Verification failures")[1].split("</section>")[0]
    assert "PRIVATE/M4ROOT/CLIP/C0002.MP4" in failures and "MISMATCH" in failures
    assert "<tr class=failed>" in html  # highlighted in the clip table too


def test_failed_copy_report(sample_card, drives, monkeypatch):
    from media_ingest import diskio
    monkeypatch.setattr(diskio, "write_all", lambda fd, data: (_ for _ in ()).throw(OSError(5, "I/O error")))
    offload_card(sample_card, drives)
    text = text_of((drives[1] / "_REPORTS/10122026/ABC261012A01_report.html").read_text())
    assert "FAIL" in text and "I/O error" in text and "NOT VERIFIED" in text


def test_everything_is_escaped(passed):
    _, record = passed
    record = copy.deepcopy(record)
    record["notes"] = "<script>alert('notes')</script>"
    record["clips"][0]["filename"] = "<img src=x onerror=alert(1)>.MP4"
    html = render_card_report(record)
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x" in html


def _stills_record(base, n, failed_index=None):
    record = copy.deepcopy(base)
    record["clips"], record["files"] = [], []
    for i in range(n):
        path = f"DCIM/100MSDCF/DSC{i:05d}.ARW"
        status = "MISMATCH" if i == failed_index else "OK"
        record["files"].append({"path": path, "size": 25_000_000, "xxh64": f"{i:016x}",
                                "verification": {"SHOW01_DRV01A": status, "SHOW01_DRV01B": "OK"}})
        record["clips"].append({"rel_path": path, "filename": path.rsplit("/", 1)[1], "kind": "still",
                                "size": 25_000_000, "xxh64": f"{i:016x}", "resolution": "6000x4000",
                                "created": f"2026-10-12T{6 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}", "notes": []})
    if failed_index is not None:
        record["result"] = "FAIL"
    return record


def test_small_stills_card_lists_every_still(passed):
    html = render_card_report(_stills_record(passed[1], STILLS_SUMMARY_THRESHOLD))
    assert f"Stills ({STILLS_SUMMARY_THRESHOLD})" in html and "Show all" not in html


def test_large_stills_card_is_summarised(passed):
    html = render_card_report(_stills_record(passed[1], 3000, failed_index=1234))
    text = text_of(html)
    assert "Stills (3,000)" in text and "2,999 of 3,000" in text and "6000x4000" in text and "75.00 GB" in text
    assert "<details class='no-print'><summary>Show all 3,000 stills" in html
    failures = html.split("Verification failures")[1].split("</section>")[0]
    assert "DSC01234.ARW" in failures  # failed stills are always shown in full


def test_missing_durations_are_flagged(passed):
    record = copy.deepcopy(passed[1])
    record["clips"][0]["duration"] = 61.0
    record["clips"][1]["duration"] = None
    assert "00:01:01 (1 clip(s) without a duration)" in text_of(render_card_report(record))


def test_quarantine_moves_old_report(sample_card, drives, monkeypatch):
    real = offload.copy_card

    def corrupting(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[0] / "PRIVATE/M4ROOT/MEDIAPRO.XML").write_bytes(b"x" * 15)
        return r

    monkeypatch.setattr(offload, "copy_card", corrupting)
    offload_card(sample_card, drives)
    monkeypatch.setattr(offload, "copy_card", real)
    items = find_incomplete_all(drives, "ABC", A01.shoot_date, "A")["ABC261012A01"]
    for item, d in zip(items, drives):
        moved = quarantine(item, d, datetime(2026, 10, 12, 21, 0, 0))
        assert (d / "_INCOMPLETE" / f"{moved.name}_report.html").exists()
        assert (moved / "ABC261012A01_report.html").exists()  # the card-folder copy moves with the folder
    assert offload_card(sample_card, drives).passed


def test_mit_report_rebuilds_reports_dir_only(passed, drives, config_file, capsys):
    card_copy = drives[0] / "10122026/VIDEO/ABC261012A01/ABC261012A01_report.html"
    before = (card_copy.read_bytes(), card_copy.stat().st_mtime_ns)
    target = drives[0] / "_REPORTS/10122026/ABC261012A01_report.html"
    target.write_text("old")
    code = main(["--config", str(config_file), "report", "10122026",
                 "--dest", str(drives[0]), "--dest", str(drives[1]), "--dest", str(drives[0].parent / "GONE")])
    out = capsys.readouterr().out
    assert code == 0 and "Rebuilt 2 report(s)" in out and "GONE: not mounted, skipped" in out
    assert target.read_text().startswith("<!doctype html>")
    assert (card_copy.read_bytes(), card_copy.stat().st_mtime_ns) == before


def test_mit_report_with_no_records(drives, config_file, capsys):
    code = main(["--config", str(config_file), "report", "10122026", "--dest", str(drives[0])])
    assert code == 2 and "no card records" in capsys.readouterr().out
