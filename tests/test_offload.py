import os
from collections import namedtuple

import pytest
import xxhash

from conftest import SAMPLE_CARD, SMALL_CHUNK
from media_ingest import offload, space
from media_ingest.errors import DestinationExistsError, InsufficientSpaceError, IngestError
from media_ingest.incomplete import FAILED, MARKER_NAME, read_marker
from media_ingest.naming import CardName
from media_ingest.offload import preflight, run_offload
from media_ingest.records import read_card_record

CARD = CardName.parse("ABC261012A01")
CLIP1 = "PRIVATE/M4ROOT/CLIP/C0001.MP4"


def offload_card(source, drives, **kw):
    kw.setdefault("allow_same_volume", True)
    kw.setdefault("chunk_size", SMALL_CHUNK)
    return run_offload(source, drives, CARD, "VIDEO", **kw)


def test_pass(sample_card, drives):
    r = offload_card(sample_card, drives)
    assert r.passed and not r.errors
    for drive, card_dir in zip(drives, r.card_dirs):
        assert card_dir == drive / "10122026/VIDEO/ABC261012A01"
        for rel, data in SAMPLE_CARD.items():
            if rel.startswith("."):
                assert not (card_dir / rel.rstrip("/")).exists()  # clutter skipped
            elif data is None:
                assert (card_dir / rel).is_dir()
            else:
                assert (card_dir / rel).read_bytes() == data
        assert not (card_dir / MARKER_NAME).exists()
        record = read_card_record(drive / "_REPORTS/10122026/ABC261012A01_card.json")
        assert record["result"] == "PASS"
        assert record["file_count"] == 5
        clip = next(f for f in record["files"] if f["path"] == CLIP1)
        assert clip["xxh64"] == xxhash.xxh64(SAMPLE_CARD[CLIP1]).hexdigest()
        assert clip["verification"] == {"SHOW01_DRV01A": "OK", "SHOW01_DRV01B": "OK"}
        assert ".DS_Store" in record["skipped"]
        assert (drive / "_LOGS").is_dir()
    assert r.file_count == 5 and r.total_bytes == sum(len(v) for k, v in SAMPLE_CARD.items() if v and k[0] != ".")


def test_copy_all(sample_card, drives):
    r = offload_card(sample_card, drives, copy_all=True)
    assert r.passed
    assert (r.card_dirs[0] / ".DS_Store").read_bytes() == b"finder"
    assert (r.card_dirs[1] / ".Trashes/501").is_dir()


def test_source_card_untouched(sample_card, drives):
    paths = [sample_card, *sample_card.rglob("*")]
    before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    for p in paths:
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    try:
        assert offload_card(sample_card, drives).passed
        assert {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths} == before
        assert sorted(map(str, sample_card.rglob("*"))) == sorted(before)[1:]
    finally:
        for p in paths:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)


def _after_copy(monkeypatch, action):
    real = offload.copy_card

    def wrapped(listing, card_dirs, **kw):
        result = real(listing, card_dirs, **kw)
        action(card_dirs)
        return result

    monkeypatch.setattr(offload, "copy_card", wrapped)


def test_one_changed_byte_fails_the_card(sample_card, drives, monkeypatch):
    def corrupt(card_dirs):
        path = card_dirs[1] / CLIP1
        data = bytearray(path.read_bytes())
        data[1000] ^= 0xFF
        path.write_bytes(bytes(data))

    _after_copy(monkeypatch, corrupt)
    r = offload_card(sample_card, drives)
    assert not r.passed
    assert r.verifications[0].ok
    assert [(c.rel_path, c.status) for c in r.verifications[1].failures] == [(CLIP1, "MISMATCH")]
    for drive, card_dir in zip(drives, r.card_dirs):
        assert read_marker(card_dir).status == FAILED
        record = read_card_record(drive / "_REPORTS/10122026/ABC261012A01_card.json")
        assert record["result"] == "FAIL"
        clip = next(f for f in record["files"] if f["path"] == CLIP1)
        assert clip["verification"] == {"SHOW01_DRV01A": "OK", "SHOW01_DRV01B": "MISMATCH"}


def test_extra_file_fails_the_card(sample_card, drives, monkeypatch):
    _after_copy(monkeypatch, lambda dirs: (dirs[0] / "stray.txt").write_text("x"))
    r = offload_card(sample_card, drives)
    assert not r.passed and r.verifications[0].extra_files == ["stray.txt"]


def test_copy_error_marks_failed(sample_card, drives, monkeypatch):
    from media_ingest import diskio

    def broken(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(diskio, "write_all", broken)
    r = offload_card(sample_card, drives)
    assert not r.passed and "No space left" in r.errors[0]
    assert r.card_records == []
    assert all(read_marker(d).status == FAILED for d in r.card_dirs)


def test_existing_card_folder_stops_before_creating_anything(sample_card, drives):
    (drives[1] / "10122026/STILLS/ABC261012A01").mkdir(parents=True)
    with pytest.raises(DestinationExistsError):
        offload_card(sample_card, drives)
    assert not (drives[0] / "10122026").exists()


def test_insufficient_space_stops_before_creating_anything(sample_card, drives, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(space.shutil, "disk_usage", lambda d: Usage(0, 0, 1_000_000))
    with pytest.raises(InsufficientSpaceError):
        offload_card(sample_card, drives)
    assert [list(d.iterdir()) for d in drives] == [[], []]


def test_interrupt_leaves_copying_markers(sample_card, drives):
    def interrupt(done, total, rel):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        offload_card(sample_card, drives, progress=interrupt)
    for drive in drives:
        marker = read_marker(drive / "10122026/VIDEO/ABC261012A01")
        assert marker.status == "COPYING" and marker.source_volume and marker.file_count == 5


class TestPreflight:
    def test_needs_two_destinations(self, sample_card, drives):
        with pytest.raises(IngestError, match="At least 2"):
            preflight(sample_card, drives[:1], allow_same_volume=True)

    def test_duplicate_destination(self, sample_card, drives):
        with pytest.raises(IngestError, match="more than once"):
            preflight(sample_card, [drives[0], drives[0]], allow_same_volume=True)

    def test_destination_inside_source(self, sample_card, drives):
        with pytest.raises(IngestError, match="overlap"):
            preflight(sample_card, [drives[0], sample_card / "PRIVATE"], allow_same_volume=True)

    def test_source_inside_destination(self, drives):
        card = drives[0] / "card"
        card.mkdir()
        with pytest.raises(IngestError, match="overlap"):
            preflight(card, drives, allow_same_volume=True)

    def test_same_drive_rejected(self, sample_card, drives):
        with pytest.raises(IngestError, match="same drive"):
            preflight(sample_card, drives)

    def test_unmounted(self, sample_card, drives, tmp_path):
        with pytest.raises(IngestError, match="mounted"):
            preflight(sample_card, [drives[0], tmp_path / "GONE"], allow_same_volume=True)
        assert not (tmp_path / "GONE").exists()


def test_card_record_includes_clip_metadata(sample_card, drives):
    r = offload_card(sample_card, drives)
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    clips = {c["rel_path"]: c for c in record["clips"]}
    assert set(clips) == {CLIP1, "PRIVATE/M4ROOT/CLIP/C0002.MP4"}  # sidecars aren't clips
    assert clips[CLIP1]["xxh64"] == xxhash.xxh64(SAMPLE_CARD[CLIP1]).hexdigest()
    assert clips[CLIP1]["size"] == len(SAMPLE_CARD[CLIP1])
    assert r.clips and r.clips[0].rel_path == CLIP1


def test_no_metadata_from_a_failed_copy(sample_card, drives, monkeypatch):
    def corrupt_all(card_dirs):
        for d in card_dirs:
            (d / CLIP1).write_bytes(b"bad")

    _after_copy(monkeypatch, corrupt_all)
    r = offload_card(sample_card, drives)
    assert not r.passed and r.clips == []
