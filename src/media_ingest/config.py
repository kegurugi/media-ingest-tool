"""Project configuration (project.json): load, validate and save.

All validation problems are collected and reported together, so the operator
can fix the file in one pass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .errors import ConfigError
from .fileutil import atomic_write_text

MEDIA_TYPES = ("VIDEO", "AUDIO", "STILLS")

SHOW_CODE_RE = re.compile(r"^[A-Z]{3}$")
CAMERA_ID_RE = re.compile(r"^[A-Z0-9]{1,3}$")

_TOP_LEVEL_KEYS = {
    "show_code",
    "project_name",
    "cameras",
    "operators",
    "default_destinations",
    "header_fields",
    "crew",
}
_CAMERA_KEYS = {"model", "description", "shooter", "media_type"}


@dataclass(frozen=True)
class Camera:
    id: str
    model: str
    description: str = ""
    shooter: str = ""
    # Expected media type. Used when a card holds no video (e.g. a timelapse
    # camera configured as VIDEO), and to warn about likely wrong-camera picks.
    media_type: Optional[str] = None


@dataclass(frozen=True)
class CrewMember:
    role: str
    name: str


@dataclass
class ProjectConfig:
    show_code: str
    project_name: str
    cameras: Dict[str, Camera]
    operators: List[str]
    default_destinations: List[str] = field(default_factory=list)
    header_fields: Dict[str, str] = field(default_factory=dict)
    crew: List[CrewMember] = field(default_factory=list)

    def camera(self, cam_id: str) -> Camera:
        try:
            return self.cameras[cam_id.strip().upper()]
        except KeyError:
            known = ", ".join(self.cameras)
            raise ConfigError(f"Unknown camera ID {cam_id!r}. Configured cameras: {known}") from None


def load_config(path: Union[str, Path]) -> ProjectConfig:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Project config not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"Cannot read project config {path}: {e}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None
    try:
        return config_from_dict(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def save_config(config: ProjectConfig, path: Union[str, Path]) -> None:
    atomic_write_text(path, json.dumps(config_to_dict(config), indent=2, ensure_ascii=False) + "\n")


def config_from_dict(data: Any) -> ProjectConfig:
    if not isinstance(data, dict):
        raise ConfigError("project config must be a JSON object")

    errors: List[str] = []

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"unknown key(s): {', '.join(unknown)}")

    show_code = _required_str(data, "show_code", errors).upper()
    if show_code and not SHOW_CODE_RE.match(show_code):
        errors.append(f"show_code must be exactly 3 letters, got {data['show_code']!r}")

    project_name = _required_str(data, "project_name", errors)
    cameras = _parse_cameras(data.get("cameras"), errors)
    operators = _parse_str_list(data.get("operators"), "operators", errors, required=True)
    destinations = _parse_str_list(
        data.get("default_destinations", []), "default_destinations", errors, required=False
    )
    for name in destinations:
        if "/" in name or ":" in name:
            errors.append(f"default_destinations: {name!r} is not a valid volume name")
    header_fields = _parse_header_fields(data.get("header_fields", {}), errors)
    crew = _parse_crew(data.get("crew", []), errors)

    if errors:
        raise ConfigError("invalid project config:\n  - " + "\n  - ".join(errors))

    return ProjectConfig(
        show_code=show_code,
        project_name=project_name,
        cameras=cameras,
        operators=operators,
        default_destinations=destinations,
        header_fields=header_fields,
        crew=crew,
    )


def config_to_dict(config: ProjectConfig) -> Dict[str, Any]:
    cameras: Dict[str, Any] = {}
    for cam in config.cameras.values():
        entry: Dict[str, Any] = {"model": cam.model}
        if cam.description:
            entry["description"] = cam.description
        if cam.shooter:
            entry["shooter"] = cam.shooter
        if cam.media_type:
            entry["media_type"] = cam.media_type
        cameras[cam.id] = entry
    return {
        "show_code": config.show_code,
        "project_name": config.project_name,
        "header_fields": dict(config.header_fields),
        "crew": [{"role": m.role, "name": m.name} for m in config.crew],
        "cameras": cameras,
        "operators": list(config.operators),
        "default_destinations": list(config.default_destinations),
    }


def _required_str(data: Dict[str, Any], key: str, errors: List[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} is required and must be a non-empty string")
        return ""
    return value.strip()


def _parse_cameras(raw: Any, errors: List[str]) -> Dict[str, Camera]:
    if not isinstance(raw, dict) or not raw:
        errors.append("cameras is required and must map camera IDs to camera details")
        return {}
    cameras: Dict[str, Camera] = {}
    for raw_id, spec in raw.items():
        cam_id = str(raw_id).strip().upper()
        where = f"cameras[{raw_id!r}]"
        if not CAMERA_ID_RE.match(cam_id):
            errors.append(f"{where}: camera ID must be 1-3 letters or digits")
            continue
        if cam_id in cameras:
            errors.append(f"{where}: duplicate camera ID {cam_id!r}")
            continue
        if isinstance(spec, str):
            spec = {"model": spec}
        if not isinstance(spec, dict):
            errors.append(f"{where}: must be a model name or an object")
            continue
        unknown = sorted(set(spec) - _CAMERA_KEYS)
        if unknown:
            errors.append(f"{where}: unknown key(s): {', '.join(unknown)}")
        model = spec.get("model")
        if not isinstance(model, str) or not model.strip():
            errors.append(f"{where}: model is required")
            continue
        optional = {}
        for key in ("description", "shooter"):
            value = spec.get(key, "")
            if not isinstance(value, str):
                errors.append(f"{where}: {key} must be a string")
                value = ""
            optional[key] = value.strip()
        media_type = spec.get("media_type")
        if media_type is not None:
            media_type = str(media_type).strip().upper()
            if media_type not in MEDIA_TYPES:
                errors.append(f"{where}: media_type must be one of {', '.join(MEDIA_TYPES)}")
                media_type = None
        cameras[cam_id] = Camera(id=cam_id, model=model.strip(), media_type=media_type, **optional)
    return cameras


def _parse_str_list(raw: Any, key: str, errors: List[str], required: bool) -> List[str]:
    if not isinstance(raw, list) or (required and not raw):
        errors.append(f"{key} must be a {'non-empty ' if required else ''}list of names")
        return []
    values: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key}: every entry must be a non-empty string")
            continue
        if item.strip() in values:
            errors.append(f"{key}: duplicate entry {item.strip()!r}")
            continue
        values.append(item.strip())
    return values


def _parse_header_fields(raw: Any, errors: List[str]) -> Dict[str, str]:
    if not isinstance(raw, dict):
        errors.append("header_fields must be an object of label: value pairs")
        return {}
    fields: Dict[str, str] = {}
    for key, value in raw.items():
        if not str(key).strip():
            errors.append("header_fields: labels must not be empty")
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            errors.append(f"header_fields[{key!r}]: value must be text or a number")
            continue
        fields[str(key).strip()] = str(value).strip()
    return fields


def _parse_crew(raw: Any, errors: List[str]) -> List[CrewMember]:
    if not isinstance(raw, list):
        errors.append('crew must be a list of {"role": ..., "name": ...} entries')
        return []
    crew: List[CrewMember] = []
    for i, entry in enumerate(raw):
        role = entry.get("role") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(role, str) or not role.strip() or not isinstance(name, str) or not name.strip():
            errors.append(f"crew[{i}]: needs a non-empty role and name")
            continue
        crew.append(CrewMember(role=role.strip(), name=name.strip()))
    return crew
