import json
from pathlib import Path

import pytest

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "project.example.json"


@pytest.fixture
def example_config_dict():
    return json.loads(EXAMPLE_CONFIG.read_text())


@pytest.fixture
def drives(tmp_path):
    """Two mounted destination drives."""
    a = tmp_path / "SHOW01_DRV01A"
    b = tmp_path / "SHOW01_DRV01B"
    a.mkdir()
    b.mkdir()
    return [a, b]


CLIP_A = bytes(range(256)) * 12_000  # ~3 MB, spans many copy chunks
CLIP_B = b"\x5a" * 1_500_001

SAMPLE_CARD = {
    "PRIVATE/M4ROOT/CLIP/C0001.MP4": CLIP_A,
    "PRIVATE/M4ROOT/CLIP/C0001M01.XML": b"<NonRealTimeMeta/>",
    "PRIVATE/M4ROOT/CLIP/C0002.MP4": CLIP_B,
    "PRIVATE/M4ROOT/MEDIAPRO.XML": b"<MediaProfile/>",
    "PRIVATE/M4ROOT/EMPTY.BIN": b"",
    "PRIVATE/M4ROOT/SUB/": None,  # empty folder
    ".Spotlight-V100/Store-V2/index": b"spotlight",
    ".Trashes/501/": None,
    "._C0001.MP4": b"appledouble",
    ".DS_Store": b"finder",
}
CLUTTER = {".Spotlight-V100/", ".Spotlight-V100/Store-V2/index", ".Trashes/", ".Trashes/501/", "._C0001.MP4", ".DS_Store"}
SMALL_CHUNK = 64 * 1024


def write_card(root, files):
    """Create a fake card. Keys ending in '/' are empty folders."""
    for rel, data in files.items():
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return root


@pytest.fixture
def sample_card(tmp_path):
    return write_card(tmp_path / "A001", SAMPLE_CARD)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "project.json"
    path.write_text(EXAMPLE_CONFIG.read_text())
    return path


def save_day_details(config_path, *dates):
    """Pre-save day details beside a project.json so `mit ingest` doesn't ask about them."""
    from datetime import date as _date
    from media_ingest.daydetails import DayDetails, project_days_dir, write_day
    for d in dates:
        write_day([], DayDetails(shoot_date=_date.fromisoformat(d).isoformat(),
                                 header_fields=[("Season", "12")], crew=[("DIT", "DIT Name")]),
                  project_days_dir(config_path))
