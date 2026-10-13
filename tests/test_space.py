from collections import namedtuple

import pytest

from media_ingest import space
from media_ingest.errors import InsufficientSpaceError
from media_ingest.sources import SourceFile, SourceListing
from media_ingest.space import check_space, format_bytes, require_space, required_bytes

Usage = namedtuple("Usage", "total used free")


def listing(*sizes, dirs=0):
    return SourceListing(
        root=None, files=[SourceFile(f"f{i}", s, 0) for i, s in enumerate(sizes)], dirs=[f"d{i}" for i in range(dirs)]
    )


def test_files_round_up_to_whole_blocks():
    # 1 byte -> 1 block, 4096 -> 1 block, 4097 -> 2 blocks, empty -> 0; plus 2 dirs + root
    assert required_bytes(listing(1, 4096, 4097, 0, dirs=2), 4096, margin=0) == (4 + 3) * 4096


def test_margin_added():
    assert required_bytes(listing(), 4096, margin=1000) == 4096 + 1000


def test_require_space_names_only_short_drives(drives, monkeypatch):
    a, b = drives
    monkeypatch.setattr(space.shutil, "disk_usage", lambda d: Usage(0, 0, 10**12 if d == a else 10))
    with pytest.raises(InsufficientSpaceError) as exc:
        require_space(listing(5000), drives)
    assert str(b) in str(exc.value) and str(a) not in str(exc.value)
    assert "Nothing was copied" in str(exc.value)


def test_enough_space(drives):
    checks = require_space(listing(5000), drives, margin=0)
    assert all(c.ok for c in checks)
    assert [c.drive for c in check_space(listing(5000), drives)] == drives


def test_format_bytes():
    assert format_bytes(512) == "512 B"
    assert format_bytes(64_200_000_000) == "64.20 GB"
    assert format_bytes(2_500_000_000_000) == "2.50 TB"
