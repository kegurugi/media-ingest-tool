import json

import pytest

from media_ingest.config import (
    Camera,
    CrewMember,
    config_from_dict,
    load_config,
    save_config,
)
from media_ingest.errors import ConfigError
from conftest import EXAMPLE_CONFIG


def test_example_config_loads():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.show_code == "ABC"
    assert list(cfg.cameras) == ["A", "B", "D", "DR1", "T", "S"]
    assert cfg.cameras["D"] == Camera(
        id="D", model="Timelapse", description="Timelapse", shooter="Shooter Name", media_type="VIDEO"
    )
    assert cfg.header_fields == {"Season": "12", "Location": "Base Camp", "Leg": "1"}
    assert cfg.crew[2] == CrewMember(role="DIT", name="DIT Name")
    assert cfg.default_destinations == ["SHOW01_DRV01A", "SHOW01_DRV01B"]


def test_camera_lookup_is_case_insensitive(example_config_dict):
    cfg = config_from_dict(example_config_dict)
    assert cfg.camera("dr1").model == "Drone 1"


def test_unknown_camera(example_config_dict):
    cfg = config_from_dict(example_config_dict)
    with pytest.raises(ConfigError, match="Unknown camera ID 'Z'"):
        cfg.camera("Z")


def test_camera_string_shorthand(example_config_dict):
    example_config_dict["cameras"] = {"a": "Sony FX6"}
    cfg = config_from_dict(example_config_dict)
    assert cfg.cameras == {"A": Camera(id="A", model="Sony FX6")}


def test_codes_are_uppercased(example_config_dict):
    example_config_dict["show_code"] = "abc"
    example_config_dict["cameras"] = {"dr1": {"model": "Drone", "media_type": "video"}}
    cfg = config_from_dict(example_config_dict)
    assert cfg.show_code == "ABC"
    assert cfg.cameras["DR1"].media_type == "VIDEO"


def test_optional_sections_default_empty(example_config_dict):
    for key in ("header_fields", "crew", "default_destinations"):
        del example_config_dict[key]
    cfg = config_from_dict(example_config_dict)
    assert cfg.header_fields == {} and cfg.crew == [] and cfg.default_destinations == []


def _set(key, value):
    def change(d):
        d[key] = value
    return change


def _delete(key):
    def change(d):
        del d[key]
    return change


def _camera(cam_id, spec):
    def change(d):
        d["cameras"][cam_id] = spec
    return change


@pytest.mark.parametrize(
    "change, message",
    [
        (_set("show_code", "AB"), "show_code"),
        (_set("show_code", "AB1"), "show_code"),
        (_set("show_code", 123), "show_code"),
        (_delete("project_name"), "project_name"),
        (_set("camers", {}), "unknown key"),
        (_set("cameras", {}), "cameras"),
        (_camera("ABCD", "Model"), "1-3 letters or digits"),
        (_camera("A-", "Model"), "1-3 letters or digits"),
        (_camera("a", "Model"), "duplicate camera ID"),
        (_camera("X", {"description": "no model"}), "model is required"),
        (_camera("X", {"model": "M", "media_type": "FILM"}), "media_type"),
        (_camera("X", {"model": "M", "lens": "50mm"}), "unknown key"),
        (_camera("X", 42), "model name or an object"),
        (_set("operators", []), "operators"),
        (_set("operators", ["Name", ""]), "operators"),
        (_set("default_destinations", ["A", "A"]), "duplicate"),
        (_set("default_destinations", ["Bad/Name"]), "valid volume name"),
        (_set("header_fields", ["Season"]), "header_fields"),
        (_set("header_fields", {"Wet": True}), "header_fields"),
        (_set("crew", [{"role": "DP"}]), "crew[0]"),
    ],
)
def test_invalid_config(example_config_dict, change, message):
    change(example_config_dict)
    with pytest.raises(ConfigError, match=message.replace("[", r"\[")):
        config_from_dict(example_config_dict)


def test_all_errors_reported_together(example_config_dict):
    example_config_dict["show_code"] = "TOOLONG"
    example_config_dict["operators"] = []
    with pytest.raises(ConfigError) as exc:
        config_from_dict(example_config_dict)
    assert "show_code" in str(exc.value) and "operators" in str(exc.value)


def test_not_an_object():
    with pytest.raises(ConfigError, match="JSON object"):
        config_from_dict(["show_code"])


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "project.json")


def test_invalid_json_reports_line(tmp_path):
    path = tmp_path / "project.json"
    path.write_text('{\n  "show_code": "ABC"\n  "project_name": "X"\n}\n')  # missing comma
    with pytest.raises(ConfigError, match="line 3"):
        load_config(path)


def test_errors_name_the_file(tmp_path):
    path = tmp_path / "project.json"
    path.write_text(json.dumps({"show_code": "AB"}))
    with pytest.raises(ConfigError, match="project.json"):
        load_config(path)


def test_save_and_reload_round_trip(tmp_path):
    original = load_config(EXAMPLE_CONFIG)
    path = tmp_path / "project.json"
    save_config(original, path)
    assert load_config(path) == original
    assert [p.name for p in tmp_path.iterdir()] == ["project.json"]
