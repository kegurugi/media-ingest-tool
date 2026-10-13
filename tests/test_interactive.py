import json
from datetime import date
from pathlib import Path

import pytest

from conftest import SMALL_CHUNK, save_day_details, write_card
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
    # Day details already confirmed for these dates; tests for the day prompt use other dates.
    save_day_details(config_file, "2026-10-12", "2026-10-13")
    """Run `mit ingest` with scripted answers. Returns (exit code, stdout+stderr, prompts asked).

    The notes answer (blank by default) is inserted before the last answer, which
    is always the "Start offload?" confirmation.
    """
    monkeypatch.setattr(interactive, "run_offload", _small_chunks(interactive.run_offload))

    def run(source, answers, camera="A", extra=(), date="10122026", notes=""):
        prompts = []
        if "--notes" not in extra:
            answers = list(answers[:-1]) + [notes] + list(answers[-1:])

        def fake_input(prompt):
            prompts.append(prompt)
            return answers.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        argv = ["--config", str(config_file), "ingest", "--source", str(source),
                "--dest", str(drives[0]), "--dest", str(drives[1]),
                "--camera", camera, "--allow-same-volume", *extra]
        if date:
            argv += ["--date", date]
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
    assert "Copy:     00:00:" in output and "Verify:   00:00:" in output and "Total:    00:00:" in output
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


needs_ffmpeg = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")


def _clip(path, created):
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25", "-t", "1",
                    "-c:v", "libx264", "-metadata", f"creation_time={created}", str(path)], check=True)


@needs_ffmpeg
def test_shoot_date_proposed_from_clips(ingest, tmp_path, drives):
    card = tmp_path / "A001"
    _clip(card / "PRIVATE/M4ROOT/CLIP/C0001.MP4", "2026-10-12T08:01:00Z")
    _clip(card / "PRIVATE/M4ROOT/CLIP/C0002.MP4", "2026-10-12T18:44:00Z")
    code, output, prompts = ingest(card, date=None, answers=["", "", "", "y"])
    assert code == 0, output
    assert "Shoot date (MMDDYYYY) [10122026]" in prompts[0]
    assert "2026-10-12 08:01 to 2026-10-12 18:44" in output
    assert (drives[0] / "10122026/VIDEO/ABC261012A01").is_dir()


@needs_ffmpeg
def test_midnight_card_must_be_typed(ingest, tmp_path, drives):
    card = tmp_path / "A001"
    _clip(card / "C0001.MP4", "2026-10-12T23:50:00Z")
    _clip(card / "C0002.MP4", "2026-10-13T00:20:00Z")
    code, output, prompts = ingest(card, date=None, answers=["10122026", "", "", "y"])
    assert code == 0, output
    assert "more than one date: 10/12/2026, 10/13/2026" in output
    assert prompts[0] == "Shoot date (MMDDYYYY): "  # no default offered
    assert (drives[0] / "10122026/VIDEO/ABC261012A01").is_dir()


def test_card_without_dates_asks(ingest, sample_card, drives):
    code, output, prompts = ingest(sample_card, date=None, answers=["10132026", "", "", "y"])
    assert code == 0
    assert "No clip dates found" in output
    assert (drives[0] / "10132026/VIDEO/ABC261013A01").is_dir()


@needs_ffmpeg
def test_date_option_overrides_proposal(ingest, tmp_path):
    card = tmp_path / "A001"
    _clip(card / "C0001.MP4", "2026-10-11T22:00:00Z")
    code, output, _ = ingest(card, ["", "", "y"])
    assert code == 0
    assert "--date 10122026 differs from the earliest clip date 10112026" in output


@pytest.fixture
def two_operators(config_file):
    data = json.loads(config_file.read_text())
    data["operators"] = ["Alex", "Robin"]
    config_file.write_text(json.dumps(data))
    return config_file


def test_operator_chosen_by_number_and_notes_recorded(ingest, two_operators, sample_card, drives):
    code, output, prompts = ingest(sample_card, ["2", "", "", "y"], notes="Interviews; mag 2")
    assert code == 0, output
    assert "Operator (number or name)" in prompts[0] and "1) Alex, 2) Robin" in output
    record = json.loads((drives[0] / "_REPORTS/10122026/ABC261012A01_card.json").read_text())
    assert record["operator"] == "Robin" and record["notes"] == "Interviews; mag 2"
    assert "Report:   " in output and "_REPORTS/10122026/ABC261012A01_report.html" in output


def test_operator_and_notes_options(ingest, two_operators, sample_card, drives):
    code, output, prompts = ingest(sample_card, ["", "", "y"], extra=("--operator", "alex", "--notes", "Wides"))
    assert code == 0, output
    assert not any("Notes" in p or "Operator" in p for p in prompts)
    record = json.loads((drives[0] / "_REPORTS/10122026/ABC261012A01_card.json").read_text())
    assert (record["operator"], record["notes"]) == ("Alex", "Wides")


def test_unknown_operator(config_file, sample_card, drives, capsys):
    code = main(["--config", str(config_file), "ingest", "--source", str(sample_card), "--dest", str(drives[0]),
                 "--dest", str(drives[1]), "--camera", "A", "--operator", "Nobody", "--allow-same-volume"])
    assert code == 2 and "Operator 'Nobody' is not in project.json" in capsys.readouterr().err


def test_first_offload_of_a_new_day_confirms_details(ingest, sample_card, drives, config_file):
    # 10/10 is before any saved day: shown from project.json, accepted with Enter.
    code, output, prompts = ingest(sample_card, ["", "", "", "y"], date="10102026")
    assert code == 0, output
    assert "Day details for 10/10/2026 (project.json):" in output
    assert "Season: 12   Location: Base Camp   Leg: 1" in output
    assert prompts[0] == "Use these for 10/10/2026? [Y/n]: "
    assert (drives[0] / "_REPORTS/10102026/10102026_day.json").exists()
    assert f"Source:  {sample_card}" in output
    # A second card the same day doesn't ask again.
    code, output, prompts = ingest(sample_card, ["", "", "y"], date="10102026")
    assert code == 0 and not any("Use these" in p for p in prompts)


def test_new_day_carries_forward_and_can_be_edited(ingest, sample_card, drives, config_file):
    ingest(sample_card, ["", "", "", "y"], date="10102026")
    # n -> edit: Season keeps, Location replaced, Leg removed, add Unit=2; crew: keep all, add none.
    answers = ["n", "", "Ridge Camp", "-", "Unit", "2", "", "", "", "", "", "", "", "y"]
    code, output, prompts = ingest(sample_card, answers, date="10112026")
    assert code == 0, output
    assert "Day details for 10/11/2026 (carried forward from 10/10/2026):" in output
    data = __import__("json").loads((drives[0] / "_REPORTS/10112026/10112026_day.json").read_text())
    assert [(f["label"], f["value"]) for f in data["header_fields"]] == [("Season", "12"), ("Location", "Ridge Camp"), ("Unit", "2")]
    text = (drives[0] / "_REPORTS/10112026/10112026_daily_card_log.html").read_text()
    assert "Ridge Camp" in text and "Unit" in text
