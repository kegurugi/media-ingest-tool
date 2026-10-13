from datetime import date
from pathlib import Path

import pytest

from conftest import SMALL_CHUNK, write_card
from media_ingest import interactive
from media_ingest.cli import main
from media_ingest.errors import NamingError
from media_ingest.incomplete import MARKER_NAME
from media_ingest.interactive import parse_media_type, parse_shoot_date, resolve_destination
from media_ingest.naming import CardName
from media_ingest.offload import run_offload


@pytest.mark.parametrize("text", ["10122026", "10/12/2026", "2026-10-12", " 10122026 "])
def test_parse_shoot_date(text):
    assert parse_shoot_date(text) == date(2026, 10, 12)


@pytest.mark.parametrize("text", ["", "13012026", "261012", "12-10-2026", "02302026"])
def test_parse_shoot_date_rejects(text):
    with pytest.raises(NamingError):
        parse_shoot_date(text)


def test_resolve_destination():
    assert resolve_destination("SHOW01_DRV01A") == Path("/Volumes/SHOW01_DRV01A")
    assert resolve_destination("/tmp/x") == Path("/tmp/x")


def test_parse_media_type():
    assert parse_media_type("v") == "VIDEO"
    assert parse_media_type("stills") == "STILLS"
    assert parse_media_type("x") is None and parse_media_type("") is None


@pytest.fixture
def ingest(config_file, drives, monkeypatch, capsys):
    """Run `mit ingest` with scripted answers. Returns (exit code, stdout+stderr, prompts asked)."""
    monkeypatch.setattr(interactive, "run_offload", _small_chunks(interactive.run_offload))

    def run(source, answers, camera="A", extra=()):
        prompts = []
        answers = list(answers)

        def fake_input(prompt):
            prompts.append(prompt)
            return answers.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        argv = ["--config", str(config_file), "ingest", "--source", str(source),
                "--dest", str(drives[0]), "--dest", str(drives[1]),
                "--camera", camera, "--date", "10122026", "--allow-same-volume", *extra]
        code = main(argv)
        out = capsys.readouterr()
        assert not answers, f"unused answers: {answers}"
        return code, out.out + out.err, prompts

    return run


def _small_chunks(fn):
    def wrapped(*a, **kw):
        kw["chunk_size"] = SMALL_CHUNK
        return fn(*a, **kw)
    return wrapped


def test_ingest_pass(ingest, sample_card, drives):
    # card name OK (default yes), media type (default VIDEO), start offload
    code, output, prompts = ingest(sample_card, ["", "", "y"])
    assert code == 0, output
    assert "Card name ABC261012A01" in prompts[0]
    assert "[VIDEO]" in prompts[1]
    assert "PASS  ABC261012A01" in output
    for d in drives:
        assert (d / "10122026/VIDEO/ABC261012A01/PRIVATE/M4ROOT/CLIP/C0001.MP4").exists()


def test_three_cards_numbered_in_sequence(ingest, sample_card, drives):
    for _ in range(3):
        assert ingest(sample_card, ["", "", "y"])[0] == 0
    assert sorted(p.name for p in (drives[0] / "10122026/VIDEO").iterdir()) == [
        "ABC261012A01", "ABC261012A02", "ABC261012A03"]


def test_cancel_copies_nothing(ingest, sample_card, drives):
    code, output, _ = ingest(sample_card, ["", "", "n"])
    assert code == 1 and "Nothing was copied" in output
    assert [list(d.iterdir()) for d in drives] == [[], []]


def test_operator_overrides_reel(ingest, sample_card, drives):
    code, output, _ = ingest(sample_card, ["n", "5", "", "y"])
    assert code == 0, output
    assert (drives[0] / "10122026/VIDEO/ABC261012A05").is_dir()


def test_video_filed_as_stills_needs_confirmation(ingest, sample_card, drives):
    code, output, _ = ingest(sample_card, ["", "STILLS", "n", "VIDEO", "y"])
    assert code == 0
    assert "being filed under STILLS" in output
    assert (drives[0] / "10122026/VIDEO/ABC261012A01").is_dir()


def test_timelapse_stills_proposed_as_video(ingest, tmp_path, drives):
    card = write_card(tmp_path / "TL", {f"DCIM/100MSDCF/DSC{i:05d}.ARW": b"raw" for i in range(20)})
    code, output, prompts = ingest(card, ["", "", "y"], camera="D")
    assert code == 0, output
    assert "[VIDEO]" in prompts[1] and "Timelapse" in output
    assert (drives[0] / "10122026/VIDEO/ABC261012D01").is_dir()


def test_recopy_keeps_reel(ingest, sample_card, drives):
    def interrupt(done, total, rel):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_offload(sample_card, drives, CardName.parse("ABC261012A01"), "VIDEO",
                    progress=interrupt, allow_same_volume=True)

    # same card? yes; media type (default VIDEO from the incomplete folder); start
    code, output, prompts = ingest(sample_card, ["y", "", "y"])
    assert code == 0, output
    assert "ABC261012A01 (VIDEO) is INCOMPLETE" in output
    assert "Recopy it as ABC261012A01" in prompts[0]
    assert "PASS  ABC261012A01" in output
    for d in drives:
        assert not (d / "10122026/VIDEO/ABC261012A01" / MARKER_NAME).exists()
        assert len(list((d / "_INCOMPLETE").iterdir())) == 1


def test_declining_recopy_uses_next_reel(ingest, sample_card, drives):
    def interrupt(done, total, rel):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_offload(sample_card, drives, CardName.parse("ABC261012A01"), "VIDEO",
                    progress=interrupt, allow_same_volume=True)
    code, output, prompts = ingest(sample_card, ["n", "", "", "y"])
    assert code == 0
    assert "Incomplete card(s) stay flagged: ABC261012A01" in output
    assert (drives[0] / "10122026/VIDEO/ABC261012A02").is_dir()


def test_failed_card_exits_non_zero_with_loud_warning(ingest, sample_card, drives, monkeypatch):
    from media_ingest import offload
    real = offload.copy_card

    def corrupting(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[1] / "PRIVATE/M4ROOT/MEDIAPRO.XML").write_bytes(b"<MediaProfilX/>")
        return r

    monkeypatch.setattr(offload, "copy_card", corrupting)
    code, output, _ = ingest(sample_card, ["", "", "y"])
    assert code == 1
    assert "!!  CARD FAILED VERIFICATION" in output
    assert "MISMATCH PRIVATE/M4ROOT/MEDIAPRO.XML" in output


def test_same_drive_refused_without_test_flag(config_file, sample_card, drives, capsys):
    code = main(["--config", str(config_file), "ingest", "--source", str(sample_card),
                 "--dest", str(drives[0]), "--dest", str(drives[1])])
    assert code == 2
    assert "same drive" in capsys.readouterr().err
