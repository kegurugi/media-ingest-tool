import xxhash
import pytest

from conftest import SMALL_CHUNK
from media_ingest import copier, diskio
from media_ingest.copier import copy_card
from media_ingest.errors import CopyError
from media_ingest.sources import list_source
from media_ingest.verify import (
    MISMATCH,
    MISSING,
    OK,
    SIZE,
    ExpectedFile,
    expected_files,
    verify_destination,
)


def test_known_xxh64_values(tmp_path):
    (tmp_path / "hello").write_bytes(b"hello")
    (tmp_path / "empty").write_bytes(b"")
    assert diskio.hash_file(tmp_path / "hello") == ("26c7827d889f6da3", 5)
    assert diskio.hash_file(tmp_path / "empty") == ("ef46db3751d8e999", 0)


def _dests(tmp_path, n):
    dirs = [tmp_path / f"dest{i}" for i in range(n)]
    for d in dirs:
        d.mkdir()
    return dirs


@pytest.mark.parametrize("n", [1, 2, 3])
def test_copy_to_n_destinations(sample_card, tmp_path, n):
    listing = list_source(sample_card)
    dests = _dests(tmp_path, n)
    seen = []
    result = copy_card(listing, dests, progress=lambda done, total, rel: seen.append((done, total)), chunk_size=SMALL_CHUNK)
    for f in listing.files:
        data = (sample_card / f.rel_path).read_bytes()
        assert result.hashes[f.rel_path] == xxhash.xxh64(data).hexdigest()
        for d in dests:
            assert (d / f.rel_path).read_bytes() == data
    for d in dests:
        assert (d / "PRIVATE/M4ROOT/SUB").is_dir()
    assert result.bytes_copied == listing.total_bytes
    assert seen[-1] == (listing.total_bytes, listing.total_bytes)


def test_modification_times_preserved(sample_card, tmp_path):
    listing = list_source(sample_card)
    (dest,) = _dests(tmp_path, 1)
    copy_card(listing, [dest], chunk_size=SMALL_CHUNK)
    for f in listing.files:
        assert (dest / f.rel_path).stat().st_mtime_ns == f.mtime_ns


def test_never_overwrites_a_file(sample_card, tmp_path):
    listing = list_source(sample_card)
    a, b = _dests(tmp_path, 2)
    (b / "PRIVATE/M4ROOT/CLIP").mkdir(parents=True)
    with pytest.raises(CopyError):
        copy_card(listing, [a, b], chunk_size=SMALL_CHUNK)
    # copy_card refuses to create folders that already exist, before touching any file
    assert not (b / "PRIVATE/M4ROOT/CLIP/C0001.MP4").exists()

    (c,) = [tmp_path / "dest_c"]
    c.mkdir()
    for d in listing.dirs:
        (c / d).mkdir(parents=True, exist_ok=True)
    existing = c / "PRIVATE/M4ROOT/CLIP/C0002.MP4"
    existing.write_bytes(b"do not touch")
    writer = copier._DestinationWriter(c)
    writer.put("open", "PRIVATE/M4ROOT/CLIP/C0002.MP4")
    writer.stop()
    assert isinstance(writer.error, FileExistsError)
    assert existing.read_bytes() == b"do not touch"


def test_write_failure_raises_copy_error(sample_card, tmp_path, monkeypatch):
    listing = list_source(sample_card)
    a, b = _dests(tmp_path, 2)
    real = diskio.write_all

    def failing(fd, data):
        if "dest1" in _fd_path(fd):
            raise OSError(5, "Input/output error")
        real(fd, data)

    monkeypatch.setattr(diskio, "write_all", failing)
    with pytest.raises(CopyError, match="dest1"):
        copy_card(listing, [a, b], chunk_size=SMALL_CHUNK)


def _fd_path(fd):
    import fcntl
    return fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0").decode()


def test_writes_bypass_cache_and_fully_sync(sample_card, tmp_path, monkeypatch):
    calls = {"nocache": 0, "sync": 0}
    real_nocache, real_sync = diskio.disable_cache, diskio.full_sync
    monkeypatch.setattr(diskio, "disable_cache", lambda fd: calls.__setitem__("nocache", calls["nocache"] + 1) or real_nocache(fd))
    monkeypatch.setattr(diskio, "full_sync", lambda fd: calls.__setitem__("sync", calls["sync"] + 1) or real_sync(fd))
    listing = list_source(sample_card)
    copy_card(listing, _dests(tmp_path, 2), chunk_size=SMALL_CHUNK)
    assert calls == {"nocache": 2 * len(listing.files), "sync": 2 * len(listing.files)}


def test_verification_reads_bypass_cache(tmp_path, monkeypatch):
    (tmp_path / "f").write_bytes(b"hello")
    seen = []
    monkeypatch.setattr(diskio, "disable_cache", lambda fd: seen.append(fd) or True)
    diskio.hash_file(tmp_path / "f")
    assert len(seen) == 1


def test_disable_cache_works_on_macos(tmp_path):
    import os, sys
    (tmp_path / "f").write_bytes(b"x")
    fd = os.open(tmp_path / "f", os.O_RDONLY)
    try:
        assert diskio.disable_cache(fd) is (sys.platform == "darwin")
    finally:
        os.close(fd)


@pytest.fixture
def copied(sample_card, tmp_path):
    listing = list_source(sample_card)
    (dest,) = _dests(tmp_path, 1)
    result = copy_card(listing, [dest], chunk_size=SMALL_CHUNK)
    return dest, listing, expected_files(listing.files, result.hashes)


def test_verify_clean_copy(copied):
    dest, listing, expected = copied
    v = verify_destination(dest, expected, listing.dirs)
    assert v.ok and all(c.status == OK for c in v.checks)


def test_verify_detects_one_changed_byte(copied):
    dest, listing, expected = copied
    path = dest / "PRIVATE/M4ROOT/CLIP/C0001.MP4"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    v = verify_destination(dest, expected, listing.dirs)
    assert not v.ok
    assert [(c.rel_path, c.status) for c in v.failures] == [("PRIVATE/M4ROOT/CLIP/C0001.MP4", MISMATCH)]


def test_verify_detects_missing_truncated_extra_and_missing_dir(copied):
    dest, listing, expected = copied
    (dest / "PRIVATE/M4ROOT/MEDIAPRO.XML").rename(dest / "moved.xml")
    with open(dest / "PRIVATE/M4ROOT/CLIP/C0002.MP4", "r+b") as f:
        f.truncate(10)
    (dest / "PRIVATE/M4ROOT/SUB").rmdir()
    v = verify_destination(dest, expected, listing.dirs)
    statuses = v.statuses()
    assert statuses["PRIVATE/M4ROOT/MEDIAPRO.XML"] == MISSING
    assert statuses["PRIVATE/M4ROOT/CLIP/C0002.MP4"] == SIZE
    assert v.extra_files == ["moved.xml"]
    assert v.missing_dirs == ["PRIVATE/M4ROOT/SUB"]


def test_verify_ignores_listed_files(copied):
    dest, listing, expected = copied
    (dest / ".mit_incomplete.json").write_text("{}")
    assert verify_destination(dest, expected, listing.dirs, ignore=[".mit_incomplete.json"]).ok


def test_matching_size_is_not_enough(tmp_path):
    (tmp_path / "f").write_bytes(b"world")
    v = verify_destination(tmp_path, [ExpectedFile("f", 5, xxhash.xxh64(b"hello").hexdigest())])
    assert v.failures[0].status == MISMATCH
