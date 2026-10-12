import os

import pytest

from media_ingest import fileutil
from media_ingest.fileutil import atomic_write_text


def test_writes_new_file(tmp_path):
    path = tmp_path / "log.txt"
    atomic_write_text(path, "hello")
    assert path.read_text() == "hello"


def test_replaces_existing_file(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("old")
    atomic_write_text(path, "new")
    assert path.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["log.txt"]


def test_failure_leaves_original_untouched(tmp_path, monkeypatch):
    path = tmp_path / "log.txt"
    path.write_text("original")

    def crash(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(fileutil.os, "replace", crash)
    with pytest.raises(OSError):
        atomic_write_text(path, "partial")
    assert path.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["log.txt"]


def test_file_is_world_readable(tmp_path):
    path = tmp_path / "log.txt"
    atomic_write_text(path, "x")
    assert os.stat(path).st_mode & 0o777 == 0o644
