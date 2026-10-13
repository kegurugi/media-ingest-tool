import json
from datetime import date

import pytest

from media_ingest.config import load_config
from media_ingest.daydetails import DayDetails, carried_forward, read_day, write_day
from conftest import EXAMPLE_CONFIG

OCT12, OCT13, OCT14 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)


@pytest.fixture
def cfg():
    return load_config(EXAMPLE_CONFIG)


def details(d, location, notes="", updated="2026-10-12T08:00:00"):
    return DayDetails(shoot_date=d.isoformat(), header_fields=[("Season", "12"), ("Location", location)],
                      crew=[("DP", "Sam")], day_notes=notes, updated_at=updated)


def test_first_day_comes_from_project_json(drives, tmp_path, cfg):
    d = carried_forward(drives, OCT12, tmp_path / "days", cfg)
    assert d.carried_from == "project.json"
    assert d.header_fields == [("Season", "12"), ("Location", "Base Camp"), ("Leg", "1")]
    assert d.crew[0] == ("Producer", "Producer Name")


def test_carried_forward_from_most_recent_earlier_day(drives, tmp_path, cfg):
    write_day(drives, details(OCT12, "Base Camp"), stamp=False)
    write_day(drives, details(OCT13, "Ridge Camp"), stamp=False)
    write_day(drives, details(date(2026, 10, 20), "Later"), stamp=False)  # after the day: ignored
    d = carried_forward(drives, OCT14, tmp_path / "days", cfg)
    assert d.carried_from == "2026-10-13" and dict(d.header_fields)["Location"] == "Ridge Camp"
    assert d.day_notes == ""  # notes belong to their own day


def test_carried_forward_from_project_folder_on_new_drives(tmp_path, cfg):
    days = tmp_path / "days"
    write_day([], details(OCT13, "Ridge Camp"), days, stamp=False)
    fresh = tmp_path / "DRV02A"
    fresh.mkdir()
    d = carried_forward([fresh], OCT14, days, cfg)
    assert dict(d.header_fields)["Location"] == "Ridge Camp"


def test_written_everywhere_and_read_back(drives, tmp_path):
    days = tmp_path / "days"
    errors = write_day([*drives, tmp_path / "NOT_MOUNTED"], details(OCT12, "Base Camp", "Wind all day"), days)
    assert errors == []
    for d in drives:
        data = json.loads((d / "_REPORTS/10122026/10122026_day.json").read_text())
        assert data["header_fields"][1] == {"label": "Location", "value": "Base Camp"}
    assert (days / "10122026_day.json").exists()
    assert not (tmp_path / "NOT_MOUNTED").exists()
    got, notes = read_day(drives, OCT12, days)
    assert got.day_notes == "Wind all day" and notes == []


def test_newest_copy_wins_and_disagreement_is_noted(drives, tmp_path):
    days = tmp_path / "days"
    write_day(drives, details(OCT12, "Base Camp", updated="2026-10-12T08:00:00"), days, stamp=False)
    write_day(drives[:1], details(OCT12, "River", updated="2026-10-12T19:00:00"), stamp=False)
    got, notes = read_day(drives, OCT12, days)
    assert dict(got.header_fields)["Location"] == "River"
    assert len(notes) == 2 and all("differ" in n for n in notes)


def test_no_details_yet(drives, tmp_path):
    assert read_day(drives, OCT12, tmp_path / "days") == (None, [])
