from datetime import date

import pytest

from media_ingest.errors import IngestError, NamingError, ReelMismatchError
from media_ingest.layout import create_card_folder
from media_ingest.naming import CardName
from media_ingest.reels import existing_reels, propose_reel

OCT12 = date(2026, 10, 12)


def make(drive, *names, media_type="VIDEO", date_folder="10122026"):
    for name in names:
        (drive / date_folder / media_type / name).mkdir(parents=True)


def test_first_card_is_01(drives):
    assert propose_reel(drives, "ABC", OCT12, "A") == 1


def test_three_cards_numbered_01_02_03(drives):
    names = []
    for _ in range(3):
        card = CardName("ABC", OCT12, "A", propose_reel(drives, "ABC", OCT12, "A"))
        for drive in drives:
            create_card_folder(drive, card, "VIDEO")
        names.append(str(card))
    assert names == ["ABC261012A01", "ABC261012A02", "ABC261012A03"]


def test_camera_d_does_not_match_drone_dr1(drives):
    for drive in drives:
        make(drive, "ABC261012DR101", "ABC261012DR102")
    assert propose_reel(drives, "ABC", OCT12, "D") == 1
    for drive in drives:
        make(drive, "ABC261012D01")
    assert propose_reel(drives, "ABC", OCT12, "DR1") == 3
    assert propose_reel(drives, "ABC", OCT12, "D") == 2


def test_gaps_are_not_filled(drives):
    for drive in drives:
        make(drive, "ABC261012A01", "ABC261012A03")
    assert propose_reel(drives, "ABC", OCT12, "A") == 4


def test_reels_counted_across_media_types(drives):
    for drive in drives:
        make(drive, "ABC261012T01", media_type="STILLS")
        make(drive, "ABC261012T02", media_type="VIDEO")
    assert propose_reel(drives, "ABC", OCT12, "T") == 3


def test_other_shows_dates_and_junk_ignored(drives):
    for drive in drives:
        make(drive, "XYZ261012A01", "ABC261013A05", "notes", "ABC261012A01_old", ".DS_Store")
        (drive / "10122026" / "VIDEO" / "readme.txt").write_text("x")
    assert propose_reel(drives, "ABC", OCT12, "A") == 1


def test_case_insensitive_folder_names(drives):
    for drive in drives:
        make(drive, "abc261012a01")
    assert existing_reels(drives[0], "abc", OCT12, "a") == {1}
    assert propose_reel(drives, "ABC", OCT12, "A") == 2


def test_destinations_disagree_on_next_reel(drives):
    a, b = drives
    make(a, "ABC261012A01", "ABC261012A02")
    make(b, "ABC261012A01")
    with pytest.raises(ReelMismatchError) as exc:
        propose_reel(drives, "ABC", OCT12, "A")
    message = str(exc.value)
    assert f"{a}: 01, 02" in message and f"{b}: 01" in message


def test_destinations_disagree_even_with_same_next_reel(drives):
    a, b = drives
    make(a, "ABC261012A01", "ABC261012A02")
    make(b, "ABC261012A02")
    with pytest.raises(ReelMismatchError):
        propose_reel(drives, "ABC", OCT12, "A")


def test_reels_exhausted(drives):
    for drive in drives:
        make(drive, "ABC261012A99")
    with pytest.raises(NamingError, match="no reels left"):
        propose_reel(drives, "ABC", OCT12, "A")


def test_unmounted_destination(drives, tmp_path):
    with pytest.raises(IngestError, match="mounted"):
        propose_reel([drives[0], tmp_path / "MISSING"], "ABC", OCT12, "A")


def test_no_destinations():
    with pytest.raises(ValueError):
        propose_reel([], "ABC", OCT12, "A")
