import contextlib
import json
import os
import sqlite3
import threading
from datetime import datetime

import pytest

from conftest import SAMPLE_CARD, SMALL_CHUNK, write_card
from media_ingest import masterlog, offload
from media_ingest.cli import main
from media_ingest.incomplete import find_incomplete_all, quarantine
from media_ingest.masterlog import LogError, add_record, connect, rebuild
from media_ingest.naming import CardName
from media_ingest.offload import JobInfo, run_offload
from media_ingest.records import read_card_record

JOB = JobInfo(project_name="Example Show", show_code="ABC", camera_id="A", camera_model="Sony FX6", operator="DIT")


def offload_card(source, drives, name="ABC261012A01", **kw):
    card = CardName.parse(name)
    return run_offload(source, drives, card, "VIDEO", allow_same_volume=True, chunk_size=SMALL_CHUNK,
                       job=JobInfo(**{**JOB.__dict__, "camera_id": card.cam}), **kw)


def rows(drive, sql, *args):
    with contextlib.closing(connect(drive)) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def test_offload_writes_log_on_every_drive(sample_card, drives):
    r = offload_card(sample_card, drives)
    assert r.passed and not r.warnings
    for d in drives:
        (o,) = rows(d, "SELECT * FROM offloads")
        assert (o["card"], o["shoot_date"], o["camera_id"], o["reel"], o["volume"]) == (
            "ABC261012A01", "2026-10-12", "A", "01", d.name)
        assert (o["result"], o["drive_result"], o["operator"], o["superseded"]) == ("PASS", "PASS", "DIT", 0)
        assert o["destinations"] == "SHOW01_DRV01A & SHOW01_DRV01B"
        assert o["clip_count"] == 2 and o["file_count"] == 5
        assert len(rows(d, "SELECT * FROM files")) == 5
        clips = rows(d, "SELECT * FROM clips ORDER BY rel_path")
        assert [c["filename"] for c in clips] == ["C0001.MP4", "C0002.MP4"]
        assert {(c["verification"], c["volume"]) for c in clips} == {("OK", d.name)}
        assert rows(d, "SELECT value FROM meta WHERE key = 'volume'")[0]["value"] == d.name


def test_log_is_cumulative(sample_card, drives, tmp_path):
    other = write_card(tmp_path / "B001", {"CLIP/C0001.MP4": b"b" * 1000})
    offload_card(sample_card, drives, "ABC261012A01")
    offload_card(other, drives, "ABC261012B01")
    offload_card(other, drives, "ABC261013A01")
    assert [r["card"] for r in rows(drives[1], "SELECT card FROM offloads ORDER BY id")] == [
        "ABC261012A01", "ABC261012B01", "ABC261013A01"]


def test_each_drive_logs_its_own_status(sample_card, drives, monkeypatch):
    real = offload.copy_card

    def corrupt_b(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[1] / "PRIVATE/M4ROOT/CLIP/C0001.MP4").write_bytes(b"x" * len(SAMPLE_CARD["PRIVATE/M4ROOT/CLIP/C0001.MP4"]))
        return r

    monkeypatch.setattr(offload, "copy_card", corrupt_b)
    assert not offload_card(sample_card, drives).passed
    a, b = drives
    assert rows(a, "SELECT result, drive_result FROM offloads")[0] == {"result": "FAIL", "drive_result": "PASS"}
    assert rows(b, "SELECT result, drive_result FROM offloads")[0] == {"result": "FAIL", "drive_result": "FAIL"}
    assert rows(a, "SELECT verification FROM clips WHERE filename = 'C0001.MP4'")[0]["verification"] == "OK"
    assert rows(b, "SELECT verification FROM clips WHERE filename = 'C0001.MP4'")[0]["verification"] == "MISMATCH"


def test_recopy_supersedes_the_failed_attempt(sample_card, drives, monkeypatch):
    real = offload.copy_card
    monkeypatch.setattr(offload, "copy_card", lambda l, d, **kw: (real(l, d, **kw), (d[0] / "PRIVATE/M4ROOT/MEDIAPRO.XML").write_bytes(b"?" * 15))[0])
    offload_card(sample_card, drives)
    monkeypatch.setattr(offload, "copy_card", real)
    for item, d in zip(find_incomplete_all(drives, "ABC", CardName.parse("ABC261012A01").shoot_date, "A")["ABC261012A01"], drives):
        quarantine(item, d, datetime(2026, 10, 12, 22, 0, 0))
    assert offload_card(sample_card, drives).passed
    offloads = rows(drives[0], "SELECT result, superseded FROM offloads ORDER BY offload_started, id")
    assert offloads == [{"result": "FAIL", "superseded": 1}, {"result": "PASS", "superseded": 0}]


def test_crash_while_replacing_leaves_old_log_intact(sample_card, drives, tmp_path, monkeypatch):
    offload_card(sample_card, drives)
    log = drives[0] / "_LOGS/media_log.sqlite"
    before = log.read_bytes()
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    record = {**record, "card": "ABC261012A02", "offload_id": "crash-test"}

    def crash(src, dst):
        raise OSError("simulated power loss before the swap")

    monkeypatch.setattr(masterlog.os, "replace", crash)
    with pytest.raises(OSError):
        add_record(drives[0], record)
    assert log.read_bytes() == before
    assert masterlog.is_healthy(log)
    assert sorted(p.name for p in log.parent.iterdir()) == [".media_log.lock", "SHOW01_DRV01A_media_log.xlsx", "media_log.sqlite"]


def test_adding_the_same_record_twice_is_a_no_op(sample_card, drives):
    offload_card(sample_card, drives)
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    assert add_record(drives[0], record) is False
    assert len(rows(drives[0], "SELECT * FROM offloads")) == 1


def test_concurrent_writers_lose_nothing(sample_card, drives):
    offload_card(sample_card, drives)
    base = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    errors = []

    def write(i):
        try:
            add_record(drives[0], {**base, "card": f"ABC261012C{i:02d}", "offload_id": f"concurrent-{i}"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(rows(drives[0], "SELECT * FROM offloads")) == 9


def test_busy_log_times_out(sample_card, drives):
    import fcntl
    offload_card(sample_card, drives)
    fd = os.open(drives[0] / "_LOGS/.media_log.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
        with pytest.raises(LogError, match="busy"):
            add_record(drives[0], {**record, "offload_id": "busy-test"}, lock_timeout=0.3)
    finally:
        os.close(fd)


def test_log_failure_is_a_warning_not_a_fail(sample_card, drives, monkeypatch):
    def broken(drive, record, **kw):
        raise LogError("disk full")

    monkeypatch.setattr(masterlog, "add_record", broken)
    r = offload_card(sample_card, drives)
    assert r.passed
    assert len(r.warnings) == 2 and "mit log rebuild" in r.warnings[0]


def test_damaged_log_blocks_writes_until_rebuilt(sample_card, drives, tmp_path):
    offload_card(sample_card, drives)
    log = drives[0] / "_LOGS/media_log.sqlite"
    log.write_bytes(b"this is not a database" * 100)
    other = write_card(tmp_path / "B001", {"CLIP/C0001.MP4": b"b" * 1000})
    r = offload_card(other, drives, "ABC261012B01")
    assert r.passed and any("damaged" in w for w in r.warnings)

    added, set_aside = rebuild(drives[0])
    assert added == 2 and set_aside is not None and set_aside.read_bytes().startswith(b"this is not")
    assert sorted(r["card"] for r in rows(drives[0], "SELECT card FROM offloads")) == ["ABC261012A01", "ABC261012B01"]


def test_rebuild_matches_the_live_log(sample_card, drives, tmp_path, monkeypatch):
    other = write_card(tmp_path / "B001", {"CLIP/C0001.MP4": b"b" * 1000})
    offload_card(sample_card, drives, "ABC261012A01")
    offload_card(other, drives, "ABC261012B01")
    live = rows(drives[0], "SELECT card, result, superseded, clip_count, total_bytes FROM offloads ORDER BY card")
    live_clips = rows(drives[0], "SELECT card, rel_path, xxh64, verification FROM clips ORDER BY card, rel_path")
    os.rename(drives[0] / "_LOGS/media_log.sqlite", tmp_path / "moved.sqlite")
    added, _ = rebuild(drives[0])
    assert added == 2
    assert rows(drives[0], "SELECT card, result, superseded, clip_count, total_bytes FROM offloads ORDER BY card") == live
    assert rows(drives[0], "SELECT card, rel_path, xxh64, verification FROM clips ORDER BY card, rel_path") == live_clips
    assert rebuild(drives[0])[0] == 0  # nothing missing now


def test_mit_log_commands(sample_card, drives, config_file, capsys):
    offload_card(sample_card, drives)
    xlsx = drives[0] / "_LOGS/SHOW01_DRV01A_media_log.xlsx"
    xlsx.write_bytes(b"stale")
    dest = ["--dest", str(drives[0]), "--dest", str(drives[1])]
    assert main(["--config", str(config_file), "log", "export", *dest]) == 0
    assert xlsx.read_bytes()[:2] == b"PK"
    assert main(["--config", str(config_file), "log", "rebuild", *dest]) == 0
    assert "0 card record(s) added" in capsys.readouterr().out


def test_old_records_without_offload_id(sample_card, drives):
    offload_card(sample_card, drives)
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    old = {k: v for k, v in record.items() if k != "offload_id"}
    assert add_record(drives[0], old) is True  # identified by card + start time instead
    assert add_record(drives[0], old) is False
