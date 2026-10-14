"""`mit init` and `mit config`: scripted walkthroughs."""
import json
from pathlib import Path

import pytest

from media_ingest import setup
from media_ingest.cli import main
from media_ingest.config import load_config
from media_ingest.setup import clean_path

CUBE = "TITLE \"test\"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"


@pytest.fixture(autouse=True)
def volumes(tmp_path, monkeypatch):
    vol = tmp_path / "Volumes"
    for name in ("DRV01A", "DRV01B", "Macintosh HD"):
        (vol / name).mkdir(parents=True)
    monkeypatch.setattr(setup, "VOLUMES", vol)
    return vol


@pytest.fixture
def luts(tmp_path):
    folder = tmp_path / "Sony LUTs"
    folder.mkdir()
    (folder / "SL3SG3Ctos709.cube").write_text(CUBE)
    (folder / "SL3SG3tos709.cube").write_text(CUBE.replace("test", "other"))
    (folder / "notalut.cube").write_text("hello")
    return folder


def run(monkeypatch, capsys, argv, answers):
    answers = list(answers)
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        assert answers, f"ran out of answers at: {prompt}"
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    code = main(argv)
    out = capsys.readouterr()
    assert not answers, f"unused answers: {answers}"
    return code, out.out + out.err, prompts


NEW_SHOW = [
    "abc", "Arctic Rivers",                          # show
    "12", "Base Camp", "1", "",                          # Season, Location, Leg, (no more)
    "Pat Lee", "Sam Ortiz", "Dwayne Fowler", "",     # Producer, DP, DIT, (no more)
    "",                                              # operators: the DIT
    "A", "1", "", "Sam Ortiz", "", "1", "",          # FX6, LUT 1 (S-Gamut3.Cine), gamma suggested
    "DR1", "8", "", "", "", "-",                     # drone, no LUT
    "S", "11", "", "", "",                           # MixPre: audio, no LUT question
    "",                                              # done with cameras
    "1, 2",                                          # drives DRV01A, DRV01B
    "", "",                                          # proxies: not automatic, ProRes Proxy
    "",                                              # save
]


def new_show(tmp_path, monkeypatch, capsys, luts):
    folder = tmp_path / "Arctic Rivers" / "ingest"
    return folder, run(monkeypatch, capsys, ["init", str(folder), "--luts", str(luts)], NEW_SHOW)


def test_new_show(tmp_path, monkeypatch, capsys, luts):
    folder, (code, out, prompts) = new_show(tmp_path, monkeypatch, capsys, luts)
    assert code == 0, out
    data = json.loads((folder / "project.json").read_text())
    assert data["show_code"] == "ABC" and data["project_name"] == "Arctic Rivers"
    assert data["header_fields"] == {"Season": "12", "Location": "Base Camp", "Leg": "1"}
    assert [c["role"] for c in data["crew"]] == ["Producer", "DP", "DIT"]
    assert data["operators"] == ["Dwayne Fowler"]
    assert data["cameras"]["A"] == {"model": "Sony FX6", "shooter": "Sam Ortiz", "media_type": "VIDEO",
                                    "lut": "luts/SL3SG3Ctos709.cube", "lut_gamma": "s-log3-cine"}
    assert data["cameras"]["DR1"] == {"model": "Drone", "media_type": "VIDEO"}
    assert data["cameras"]["S"] == {"model": "Sound Devices MixPre", "media_type": "AUDIO"}
    assert data["default_destinations"] == ["DRV01A", "DRV01B"]
    assert data["proxies"] == {"auto": False, "codec": "prores_proxy"}
    assert (folder / "luts/SL3SG3Ctos709.cube").read_text() == CUBE  # copied in
    config = load_config(folder / "project.json")
    assert config.lut_for("A") == (folder / "luts/SL3SG3Ctos709.cube", "s-log3-cine")
    assert "LUT SL3SG3Ctos709.cube for s-log3-cine [ok]" in out
    assert sum("Quick picks" in line for line in out.splitlines()) == 1
    assert not any("gamma" in p for p in prompts if "Camera S" in p)


def test_invalid_answers_are_asked_again(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "show"
    answers = ["AB1", "ABC", "Show", "", "", "", "", "", "", "", "", "Me",
               "ABCD", "A", "Canon C80", "", "", "FILM", "v", "-", "", "", "", "", ""]
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder)], answers)
    assert code == 0, out
    assert "It must be exactly 3 letters" in out and "A camera ID is 1-3 letters or digits" in out
    data = json.loads((folder / "project.json").read_text())
    assert data["cameras"] == {"A": {"model": "Canon C80", "media_type": "VIDEO"}}
    assert data["header_fields"] == {} and data["crew"] == [] and data["operators"] == ["Me"]


def test_lut_by_dragged_path_and_bad_luts(tmp_path, monkeypatch, capsys, luts):
    folder = tmp_path / "show"
    dragged = str(luts / "SL3SG3tos709.cube").replace(" ", "\\ ")  # how Terminal pastes a dragged file
    answers = ["ABC", "Show"] + [""] * 8 + ["Me",
               "A", "Sony FX3", "", "", "", str(luts / "missing.cube"), str(luts / "notalut.cube"), dragged, "",
               "", "-", "", "", ""]
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder)], answers)
    assert code == 0, out
    assert "can't read the LUT" in out and "isn't a 3D .cube LUT" in out
    cam = json.loads((folder / "project.json").read_text())["cameras"]["A"]
    assert cam["lut"] == "luts/SL3SG3tos709.cube" and cam["lut_gamma"] == "s-log3"  # suggested from the name


def test_non_sony_lut_applies_to_every_clip(tmp_path, monkeypatch, capsys, luts):
    folder = tmp_path / "show"
    answers = ["ABC", "Show"] + [""] * 8 + ["Me", "B", "5", "", "", "", "1", "", "", "", "", ""]
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder), "--luts", str(luts)], answers)
    assert code == 0, out
    assert "the LUT goes on every clip" in out
    assert "lut_gamma" not in json.loads((folder / "project.json").read_text())["cameras"]["B"]


def test_start_from_an_earlier_show(tmp_path, monkeypatch, capsys, luts):
    old, _ = new_show(tmp_path, monkeypatch, capsys, luts)
    new = tmp_path / "Season 13" / "ingest"
    # New code and location; Return keeps everything else (cameras kept as they were).
    answers = ["XYZ", "Arctic Rivers S13", "13", "Ridge Camp", "", "", "", "", "", "", "", "", "", "", "", ""]
    code, out, _ = run(monkeypatch, capsys, ["init", str(new), "--from", str(old)], answers)
    assert code == 0, out
    assert "starting from" in out
    data = json.loads((new / "project.json").read_text())
    assert data["show_code"] == "XYZ" and data["header_fields"] == {"Season": "13", "Location": "Ridge Camp", "Leg": "1"}
    assert data["cameras"]["A"]["lut"] == "luts/SL3SG3Ctos709.cube"
    assert (new / "luts/SL3SG3Ctos709.cube").exists()  # brought along from the old show
    assert load_config(new / "project.json").lut_for("A")[0] == new / "luts/SL3SG3Ctos709.cube"


def test_edit_existing_keeps_previous_version(tmp_path, monkeypatch, capsys, luts):
    folder, _ = new_show(tmp_path, monkeypatch, capsys, luts)
    before = (folder / "project.json").read_text()
    # Change Location; remove the drone; Return keeps the rest.
    answers = ["", "", "", "Ridge Camp", "", "", "", "", "", "", "", "-DR1", "", "", "", "", ""]
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder)], answers)
    assert code == 0, out
    assert "Editing" in out and "project.json.previous" in out
    assert (folder / "project.json.previous").read_text() == before
    data = json.loads((folder / "project.json").read_text())
    assert data["header_fields"]["Location"] == "Ridge Camp" and set(data["cameras"]) == {"A", "S"}
    assert data["cameras"]["A"]["lut"] == "luts/SL3SG3Ctos709.cube"


def test_not_saving_writes_nothing(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "show"
    answers = ["ABC", "Show"] + [""] * 8 + ["Me", "A", "1", "", "", "", "-", "", "", "", "", "n"]
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder)], answers)
    assert code == 1 and "Not saved." in out and not (folder / "project.json").exists()


def test_from_refused_when_editing(tmp_path, monkeypatch, capsys, luts):
    folder, _ = new_show(tmp_path, monkeypatch, capsys, luts)
    code, out, _ = run(monkeypatch, capsys, ["init", str(folder), "--from", str(folder)], [])
    assert code == 2 and "already exists" in out


def test_mit_config(tmp_path, monkeypatch, capsys, luts):
    folder, _ = new_show(tmp_path, monkeypatch, capsys, luts)
    assert main(["--config", str(folder / "project.json"), "config"]) == 0
    out = capsys.readouterr().out
    assert "project.json is valid." in out and "DRV01A (connected), DRV01B (connected)" in out
    (folder / "luts/SL3SG3Ctos709.cube").unlink()
    assert main(["--config", str(folder / "project.json"), "config"]) == 1
    assert "LUT problem for camera(s) A" in capsys.readouterr().out
    (folder / "project.json").write_text('{"show_code": "AB"}')
    assert main(["--config", str(folder / "project.json"), "config"]) == 2


@pytest.mark.parametrize("raw, clean", [
    ("/Volumes/My\\ Drive/LUTs/a.cube ", "/Volumes/My Drive/LUTs/a.cube"),
    ("'/Users/me/Sony LUTs/a.cube'", "/Users/me/Sony LUTs/a.cube"),
    ('"~/luts/a.cube"', "~/luts/a.cube"),
])
def test_clean_path(raw, clean):
    assert clean_path(raw) == clean
