from datetime import date

import pytest

from media_ingest.errors import DestinationExistsError, IngestError
from media_ingest.layout import (
    card_dir,
    card_report_path,
    create_card_folder,
    daily_log_path,
    find_existing_card,
    master_log_path,
    prepare_drive,
    xlsx_log_path,
)
from media_ingest.naming import CardName

CARD = CardName.parse("ABC261012A01")
OCT12 = date(2026, 10, 12)


def test_paths(drives):
    drive = drives[0]
    assert card_dir(drive, CARD, "VIDEO") == drive / "10122026" / "VIDEO" / "ABC261012A01"
    assert card_report_path(drive, CARD) == drive / "_REPORTS" / "10122026" / "ABC261012A01_report.html"
    assert daily_log_path(drive, OCT12) == drive / "_REPORTS" / "10122026" / "10122026_daily_card_log.html"
    assert master_log_path(drive) == drive / "_LOGS" / "media_log.sqlite"
    assert xlsx_log_path(drive) == drive / "_LOGS" / "SHOW01_DRV01A_media_log.xlsx"


def test_invalid_media_type(drives):
    with pytest.raises(ValueError):
        card_dir(drives[0], CARD, "FILM")


def test_create_card_folder(drives):
    drive = drives[0]
    path = create_card_folder(drive, CARD, "VIDEO")
    assert path == drive / "10122026" / "VIDEO" / "ABC261012A01"
    assert path.is_dir() and not any(path.iterdir())
    # Other media folders are only created when needed.
    assert sorted(p.name for p in (drive / "10122026").iterdir()) == ["VIDEO"]


def test_never_overwrites_existing_card_folder(drives):
    drive = drives[0]
    path = create_card_folder(drive, CARD, "VIDEO")
    (path / "C0001.MP4").write_bytes(b"original media")
    with pytest.raises(DestinationExistsError, match="ABC261012A01"):
        create_card_folder(drive, CARD, "VIDEO")
    assert (path / "C0001.MP4").read_bytes() == b"original media"


def test_card_name_already_used_under_another_media_type(drives):
    drive = drives[0]
    create_card_folder(drive, CARD, "STILLS")
    with pytest.raises(DestinationExistsError):
        create_card_folder(drive, CARD, "VIDEO")
    assert not (drive / "10122026" / "VIDEO" / "ABC261012A01").exists()


def test_existing_folder_with_different_case(drives):
    drive = drives[0]
    (drive / "10122026" / "VIDEO" / "abc261012a01").mkdir(parents=True)
    with pytest.raises(DestinationExistsError):
        create_card_folder(drive, CARD, "VIDEO")


def test_find_existing_card(drives):
    drive = drives[0]
    assert find_existing_card(drive, CARD) == []
    path = create_card_folder(drive, CARD, "AUDIO")
    assert find_existing_card(drive, CARD) == [path]


def test_unmounted_drive_is_not_created(tmp_path):
    missing = tmp_path / "NOT_MOUNTED"
    with pytest.raises(IngestError, match="mounted"):
        create_card_folder(missing, CARD, "VIDEO")
    assert not missing.exists()


def test_prepare_drive(drives):
    drive = drives[0]
    prepare_drive(drive, OCT12)
    prepare_drive(drive, OCT12)  # idempotent
    assert (drive / "_REPORTS" / "10122026").is_dir()
    assert (drive / "_LOGS").is_dir()
