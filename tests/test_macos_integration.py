"""Real-world checks on macOS: a genuinely full volume, and a process killed mid-copy."""
import os
import signal
import subprocess
import sys
import time
from datetime import date

import pytest

from conftest import write_card
from media_ingest.errors import DestinationExistsError, InsufficientSpaceError
from media_ingest.incomplete import find_incomplete_all, quarantine, read_marker
from media_ingest.naming import CardName
from media_ingest.offload import run_offload

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

A01 = CardName.parse("ABC261012A01")


@pytest.fixture
def small_volume(tmp_path):
    """A real 16 MB HFS+ volume from a disk image, detached afterwards."""
    image = tmp_path / "small.dmg"
    mount = tmp_path / "SMALLVOL"
    mount.mkdir()
    try:
        subprocess.run(["hdiutil", "create", "-size", "16m", "-fs", "HFS+", "-volname", "SMALLVOL",
                        "-o", str(image)], check=True, capture_output=True)
        subprocess.run(["hdiutil", "attach", str(image), "-nobrowse", "-mountpoint", str(mount)],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"hdiutil unavailable: {e}")
    yield mount
    subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)


def test_full_destination_aborts_before_copying(tmp_path, small_volume):
    card = write_card(tmp_path / "A001", {f"CLIP/C{i:04d}.MP4": os.urandom(1_000_000) for i in range(30)})
    big = tmp_path / "BIG"
    big.mkdir()
    before = set(os.listdir(small_volume))
    with pytest.raises(InsufficientSpaceError, match=str(small_volume)):
        run_offload(card, [big, small_volume], A01, "VIDEO", allow_same_volume=True)
    assert set(os.listdir(small_volume)) == before
    assert list(big.iterdir()) == []


KILL_SCRIPT = """
import sys, time
from pathlib import Path
from media_ingest.naming import CardName
from media_ingest.offload import run_offload

source, a, b, ready = sys.argv[1:]
def progress(done, total, rel):
    if done > total // 3:
        Path(ready).touch()
        time.sleep(60)
run_offload(Path(source), [Path(a), Path(b)], CardName.parse("ABC261012A01"), "VIDEO",
            progress=progress, allow_same_volume=True, chunk_size=256 * 1024)
"""


def test_killed_mid_copy_is_flagged_and_recopied_under_same_reel(tmp_path, drives):
    card = write_card(tmp_path / "A001", {f"CLIP/C{i:04d}.MP4": os.urandom(2_000_000) for i in range(6)})
    ready = tmp_path / "ready"
    proc = subprocess.Popen([sys.executable, "-c", KILL_SCRIPT, str(card), str(drives[0]), str(drives[1]), str(ready)])
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert proc.poll() is None, "copy process exited early"
            assert time.monotonic() < deadline, "copy never started"
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=10)

    card_dirs = [d / "10122026/VIDEO/ABC261012A01" for d in drives]
    assert all(read_marker(cd).status == "COPYING" for cd in card_dirs)

    # A plain rerun refuses to overwrite.
    with pytest.raises(DestinationExistsError):
        run_offload(card, drives, A01, "VIDEO", allow_same_volume=True)

    # The recopy flow keeps A01 and moves the partial copy aside.
    items = find_incomplete_all(drives, "ABC", date(2026, 10, 12), "A")["ABC261012A01"]
    for item, d in zip(items, drives):
        quarantine(item, d)
    result = run_offload(card, drives, A01, "VIDEO", allow_same_volume=True)
    assert result.passed
    assert all(len(list((d / "_INCOMPLETE").iterdir())) == 1 for d in drives)
