from datetime import date, datetime

import pytest

from conftest import SMALL_CHUNK, write_card
from media_ingest import offload
from media_ingest.errors import IncompleteMismatchError
from media_ingest.incomplete import (
    MARKER_NAME,
    find_incomplete_all,
    marker_differences,
    quarantine,
    read_marker,
)
from media_ingest.naming import CardName
from media_ingest.offload import run_offload
from media_ingest.reels import propose_reel
from media_ingest.sources import list_source, volume_name

OCT12 = date(2026, 10, 12)
A01 = CardName.parse("ABC261012A01")


def interrupted_offload(source, drives, card=A01):
    calls = []

    def interrupt(done, total, rel):
        calls.append(rel)
        if len(calls) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_offload(source, drives, card, "VIDEO", progress=interrupt, allow_same_volume=True, chunk_size=SMALL_CHUNK)


def recopy(source, drives, card_name="ABC261012A01", when=datetime(2026, 10, 12, 18, 30, 0)):
    items = find_incomplete_all(drives, "ABC", OCT12, "A")[card_name]
    moved = [quarantine(item, next(d for d in drives if d in item.path.parents), when) for item in items]
    result = run_offload(source, drives, items[0].card, items[0].media_type, allow_same_volume=True,
                         chunk_size=SMALL_CHUNK)
    return result, moved


def test_interrupted_card_is_found_on_all_drives(sample_card, drives):
    interrupted_offload(sample_card, drives)
    found = find_incomplete_all(drives, "ABC", OCT12, "A")
    assert list(found) == ["ABC261012A01"]
    assert [i.path for i in found["ABC261012A01"]] == [d / "10122026/VIDEO/ABC261012A01" for d in drives]


def test_same_card_matches_marker(sample_card, drives):
    interrupted_offload(sample_card, drives)
    marker = find_incomplete_all(drives, "ABC", OCT12, "A")["ABC261012A01"][0].marker
    assert marker_differences(marker, list_source(sample_card), volume_name(sample_card)) == []


def test_different_card_is_detected(sample_card, drives, tmp_path):
    interrupted_offload(sample_card, drives)
    other = write_card(tmp_path / "B002", {"CLIP/C0001.MP4": b"different"})
    marker = find_incomplete_all(drives, "ABC", OCT12, "A")["ABC261012A01"][0].marker
    diffs = marker_differences(marker, list_source(other), "B002")
    assert len(diffs) == 3


def test_recopy_keeps_reel_number_and_deletes_nothing(sample_card, drives):
    interrupted_offload(sample_card, drives)
    # Without the recopy flow, the next card would be A02.
    assert propose_reel(drives, "ABC", OCT12, "A") == 2

    partial = {d: sorted(p.relative_to(d / "10122026/VIDEO/ABC261012A01")
                         for p in (d / "10122026/VIDEO/ABC261012A01").rglob("*")) for d in drives}
    result, moved = recopy(sample_card, drives)

    assert result.passed and str(result.card) == "ABC261012A01"
    for d, m in zip(drives, moved):
        assert m == d / "_INCOMPLETE/ABC261012A01_20261012-183000"
        assert sorted(p.relative_to(m) for p in m.rglob("*")) == partial[d]  # partial copy kept intact
        assert (m / MARKER_NAME).exists()
    assert find_incomplete_all(drives, "ABC", OCT12, "A") == {}
    assert propose_reel(drives, "ABC", OCT12, "A") == 2


def test_rerun_without_recopy_never_overwrites(sample_card, drives):
    interrupted_offload(sample_card, drives)
    from media_ingest.errors import DestinationExistsError
    with pytest.raises(DestinationExistsError):
        run_offload(sample_card, drives, A01, "VIDEO", allow_same_volume=True)


def test_failed_card_recopy_moves_old_hash_record(sample_card, drives, monkeypatch):
    real = offload.copy_card

    def corrupting(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[0] / "PRIVATE/M4ROOT/MEDIAPRO.XML").write_bytes(b"changed!!!!!!!!")
        return r

    monkeypatch.setattr(offload, "copy_card", corrupting)
    assert not run_offload(sample_card, drives, A01, "VIDEO", allow_same_volume=True).passed
    monkeypatch.setattr(offload, "copy_card", real)

    assert read_marker(drives[0] / "10122026/VIDEO/ABC261012A01").status == "FAILED"
    result, moved = recopy(sample_card, drives)
    assert result.passed
    for d, m in zip(drives, moved):
        assert (d / "_INCOMPLETE" / f"{m.name}_card.json").exists()
        assert (d / "_REPORTS/10122026/ABC261012A01_card.json").exists()


def test_destinations_disagree(sample_card, drives):
    interrupted_offload(sample_card, drives)
    (drives[1] / "10122026/VIDEO/ABC261012A01" / MARKER_NAME).unlink()
    with pytest.raises(IncompleteMismatchError, match="disagree"):
        find_incomplete_all(drives, "ABC", OCT12, "A")


def test_only_matching_camera(sample_card, drives):
    interrupted_offload(sample_card, drives)
    assert find_incomplete_all(drives, "ABC", OCT12, "B") == {}
    assert find_incomplete_all(drives, "ABC", date(2026, 10, 13), "A") == {}


def test_unreadable_marker_still_counts(drives):
    for d in drives:
        folder = d / "10122026/AUDIO/ABC261012A03"
        folder.mkdir(parents=True)
        (folder / MARKER_NAME).write_text("{not json")
    found = find_incomplete_all(drives, "ABC", OCT12, "A")
    assert found["ABC261012A03"][0].marker.status == "UNREADABLE MARKER"
