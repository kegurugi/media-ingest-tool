import os

import pytest

from media_ingest.config import Camera
from media_ingest.errors import IngestError
from media_ingest.mediatype import CardScan, choice_warnings, propose_media_type, scan_card

FX6 = Camera(id="A", model="Sony FX6", media_type="VIDEO")
TIMELAPSE = Camera(id="D", model="Timelapse", description="Timelapse", media_type="VIDEO")
STILLS_CAM = Camera(id="T", model="Stills", media_type="STILLS")
MIXPRE = Camera(id="S", model="Sound Devices MixPre", media_type="AUDIO")
UNTYPED = Camera(id="X", model="Unknown")


def card(root, *files):
    for f in files:
        path = root / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return root


@pytest.fixture
def sony(tmp_path):
    return card(
        tmp_path / "sony",
        "PRIVATE/M4ROOT/CLIP/C0001.MP4",
        "PRIVATE/M4ROOT/CLIP/C0001M01.XML",
        "PRIVATE/M4ROOT/CLIP/C0002.MP4",
        "PRIVATE/M4ROOT/THMBNL/C0001T01.JPG",
        "PRIVATE/M4ROOT/MEDIAPRO.XML",
    )


@pytest.fixture
def canon(tmp_path):
    return card(
        tmp_path / "canon",
        "CONTENTS/CLIPS001/A001C001_261012AB_CANON.MXF",
        "CONTENTS/CLIPS001/A001C001_261012AB_CANON.XML",
        "CONTENTS/THMBNL/A001C001.JPG",
    )


@pytest.fixture
def timelapse(tmp_path):
    return card(tmp_path / "tl", *(f"DCIM/100MSDCF/DSC{i:05d}.ARW" for i in range(50)))


def test_sony_card(sony):
    scan = scan_card(sony)
    assert (scan.video, scan.audio, scan.stills) == (2, 0, 0)  # thumbnail is not a still
    assert scan.structures == ["PRIVATE/M4ROOT"]
    assert propose_media_type(scan, FX6).media_type == "VIDEO"


def test_canon_card(canon):
    scan = scan_card(canon)
    assert (scan.video, scan.stills) == (1, 0)
    assert scan.structures == ["CONTENTS"]
    assert propose_media_type(scan).media_type == "VIDEO"


def test_audio_card(tmp_path):
    scan = scan_card(card(tmp_path / "s", "261012_001.WAV", "261012_002.WAV", "SETTINGS.TXT"))
    assert (scan.audio, scan.other) == (2, 1)
    assert propose_media_type(scan).media_type == "AUDIO"
    p = propose_media_type(scan, MIXPRE)
    assert p.media_type == "AUDIO" and not p.notes and not p.warnings


def test_stills_card_without_config(tmp_path):
    scan = scan_card(card(tmp_path / "t", "DCIM/100MSDCF/DSC00001.JPG", "DCIM/100MSDCF/DSC00001.ARW"))
    assert scan.stills == 2
    assert propose_media_type(scan).media_type == "STILLS"


def test_hybrid_card_goes_to_video(tmp_path):
    scan = scan_card(
        card(tmp_path / "h", "PRIVATE/M4ROOT/CLIP/C0001.MP4", "DCIM/100MSDCF/DSC00001.JPG")
    )
    assert (scan.video, scan.stills) == (1, 1)  # DCIM stills are counted
    assert propose_media_type(scan).media_type == "VIDEO"


def test_video_on_stills_camera_still_goes_to_video(tmp_path):
    scan = scan_card(card(tmp_path / "t", "DCIM/100CANON/MVI_0001.MP4", "DCIM/100CANON/IMG_0001.CR3"))
    p = propose_media_type(scan, STILLS_CAM)
    assert p.media_type == "VIDEO"
    assert any("configured as STILLS" in n for n in p.notes)
    assert not p.warnings


def test_timelapse_stills_go_to_video_via_camera_config(timelapse):
    scan = scan_card(timelapse)
    assert (scan.video, scan.stills) == (0, 50)
    p = propose_media_type(scan, TIMELAPSE)
    assert p.media_type == "VIDEO"
    assert "Timelapse" in p.reason and "50 stills" in p.reason
    assert not p.warnings


def test_timelapse_without_config_proposes_stills(timelapse):
    assert propose_media_type(scan_card(timelapse), UNTYPED).media_type == "STILLS"


def test_video_on_audio_camera_warns_loudly(sony):
    p = propose_media_type(scan_card(sony), MIXPRE)
    assert p.media_type == "VIDEO"
    assert p.warnings and "AUDIO camera" in p.warnings[0]


def test_configured_type_with_no_matching_files_gets_a_note(timelapse):
    p = propose_media_type(scan_card(timelapse), MIXPRE)
    assert p.media_type == "AUDIO"
    assert any("no audio files" in n for n in p.notes)


def test_audio_and_stills_without_config_is_undecided(tmp_path):
    scan = scan_card(card(tmp_path / "m", "A.WAV", "B.JPG"))
    assert propose_media_type(scan).media_type is None


def test_empty_card_is_undecided(tmp_path):
    scan = scan_card(card(tmp_path / "e", "PRIVATE/M4ROOT/MEDIAPRO.XML"))
    assert scan.is_empty
    assert propose_media_type(scan).media_type is None


def test_hidden_files_ignored(tmp_path):
    scan = scan_card(
        card(tmp_path / "x", "._C0001.MP4", ".Spotlight-V100/Store/x.jpg", ".Trashes/501/clip.mp4", "A.WAV")
    )
    assert (scan.video, scan.audio, scan.stills, scan.other) == (0, 1, 0, 0)


def test_extensions_case_insensitive(tmp_path):
    scan = scan_card(card(tmp_path / "x", "clip.mxf", "CLIP2.MXF", "Clip3.Mov"))
    assert scan.video == 3


def test_scan_never_writes_to_card(sony):
    """Scanning works on a read-only card, so it cannot be writing to it."""
    paths = [sony, *sony.rglob("*")]
    for p in paths:
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    try:
        before = sorted((str(p), p.stat().st_mtime_ns) for p in paths)
        scan_card(sony)
        assert sorted((str(p), p.stat().st_mtime_ns) for p in paths) == before
    finally:
        for p in paths:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)


def test_missing_card(tmp_path):
    with pytest.raises(IngestError):
        scan_card(tmp_path / "nope")


def test_summary():
    assert CardScan(video=42, stills=1800).summary() == "42 video clips, 0 audio files, 1,800 stills"


def test_choice_warnings():
    video_card = CardScan(video=3, stills=10)
    assert choice_warnings(video_card, "VIDEO") == []
    assert "3 video clip(s)" in choice_warnings(video_card, "STILLS")[0]
    assert choice_warnings(CardScan(stills=500), "VIDEO") == []  # timelapse filed as video
    with pytest.raises(ValueError):
        choice_warnings(video_card, "FILM")
