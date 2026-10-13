import os

import pytest

from conftest import CLUTTER, SAMPLE_CARD
from media_ingest.errors import SourceError
from media_ingest.sources import is_macos_clutter, list_source, volume_name


def test_lists_files_and_folders_skipping_clutter(sample_card):
    listing = list_source(sample_card)
    assert sorted(f.rel_path for f in listing.files) == [
        "PRIVATE/M4ROOT/CLIP/C0001.MP4",
        "PRIVATE/M4ROOT/CLIP/C0001M01.XML",
        "PRIVATE/M4ROOT/CLIP/C0002.MP4",
        "PRIVATE/M4ROOT/EMPTY.BIN",
        "PRIVATE/M4ROOT/MEDIAPRO.XML",
    ]
    assert listing.dirs == ["PRIVATE", "PRIVATE/M4ROOT", "PRIVATE/M4ROOT/CLIP", "PRIVATE/M4ROOT/SUB"]
    assert set(listing.skipped) == {".Spotlight-V100/", ".Trashes/", "._C0001.MP4", ".DS_Store"}
    assert listing.total_bytes == sum(len(v) for k, v in SAMPLE_CARD.items() if v is not None and k[0] != ".")


def test_parents_listed_before_children(sample_card):
    dirs = list_source(sample_card).dirs
    for d in dirs:
        parent = d.rpartition("/")[0]
        assert not parent or dirs.index(parent) < dirs.index(d)


def test_copy_all_includes_clutter(sample_card):
    listing = list_source(sample_card, copy_all=True)
    listed = {f.rel_path for f in listing.files} | {d + "/" for d in listing.dirs}
    assert CLUTTER <= listed
    assert listing.skipped == []


def test_other_hidden_files_are_copied(tmp_path):
    (tmp_path / "card").mkdir()
    (tmp_path / "card" / ".camera_hidden").write_bytes(b"x")
    assert [f.rel_path for f in list_source(tmp_path / "card").files] == [".camera_hidden"]


def test_symlink_stops_the_offload(sample_card):
    os.symlink("PRIVATE/M4ROOT/MEDIAPRO.XML", sample_card / "link.xml")
    with pytest.raises(SourceError, match="symlink"):
        list_source(sample_card)


def test_unreadable_file_stops_the_offload(sample_card):
    path = sample_card / "PRIVATE/M4ROOT/MEDIAPRO.XML"
    os.chmod(path, 0)
    try:
        with pytest.raises(SourceError, match="not readable"):
            list_source(sample_card)
    finally:
        os.chmod(path, 0o644)


def test_missing_source(tmp_path):
    with pytest.raises(SourceError):
        list_source(tmp_path / "nope")


def test_clutter_names():
    assert is_macos_clutter("._C0001.MP4") and is_macos_clutter(".fseventsd")
    assert not is_macos_clutter(".camera_hidden") and not is_macos_clutter("C0001.MP4")


def test_volume_name():
    assert volume_name("/Volumes/A001/PRIVATE/M4ROOT") == "A001"


def test_volume_name_for_local_folder(tmp_path):
    (tmp_path / "A001").mkdir()
    assert volume_name(tmp_path / "A001") == "A001"
