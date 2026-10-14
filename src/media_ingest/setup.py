"""`mit init` (set up or edit a show's project.json) and `mit config` (check one).

`mit init FOLDER` walks through every setting with defaults, so Return moves
things along: show, header fields, crew, operators, cameras (with quick picks
and LUTs), default drives and proxies. It shows a summary, validates the
result exactly as every other command does, and only then saves.

  - New show: starts from suggestions, or from an earlier show with --from.
  - Existing project.json in FOLDER: edits it, current values as defaults; the
    old file is kept as project.json.previous.
  - A chosen LUT is copied into the show's luts/ folder, so the show folder is
    self-contained.
"""
from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    CAMERA_ID_RE,
    MEDIA_TYPES,
    PROXY_CODECS,
    SHOW_CODE_RE,
    ConfigError,
    ProjectConfig,
    config_from_dict,
    load_config,
)
from .fileutil import atomic_write_text
from .proxies import check_lut

PREVIOUS_SUFFIX = ".previous"
VOLUMES = Path("/Volumes")
SUGGESTED_FIELDS = ["Season", "Location", "Leg"]
SUGGESTED_CREW = ["Producer", "DP", "DIT"]

# Quick picks: (model, media type, lut_gamma to suggest for Sony S-Log3 LUTs)
CAMERA_PRESETS: List[Tuple[str, str, Optional[str]]] = [
    ("Sony FX6", "VIDEO", "s-log3-cine"),
    ("Sony FX3", "VIDEO", "s-log3-cine"),
    ("Sony FX9", "VIDEO", "s-log3-cine"),
    ("Sony FX30", "VIDEO", "s-log3-cine"),
    ("Canon C80", "VIDEO", None),
    ("Canon C70", "VIDEO", None),
    ("Canon C300 Mark III", "VIDEO", None),
    ("Drone", "VIDEO", None),
    ("Timelapse", "VIDEO", None),
    ("Stills camera", "STILLS", None),
    ("Sound Devices MixPre", "AUDIO", None),
]


class Cancelled(Exception):
    pass


# --- small prompt helpers ----------------------------------------------------

def _ask(p, prompt: str, default: str = "") -> str:
    """Return the answer, or the default on Return (which may be empty)."""
    suffix = f" [{default}]" if default else ""
    answer = p.input_fn(f"{prompt}{suffix}: ").strip()
    return answer if answer else default


def _yes(p, prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = p.input_fn(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def clean_path(text: str) -> str:
    """A path typed or dragged into Terminal: strip quotes and backslash escapes."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return re.sub(r"\\(.)", r"\1", text)


def _pairs(p, title: str, current: List[Tuple[str, str]], suggested: List[str], key: str) -> List[Tuple[str, str]]:
    """Walk label/value pairs: Return keeps, type replaces, '-' removes; then add more."""
    p.say(f"\n{title} (Return keeps, type to replace, '-' removes; Return on an empty one skips it):")
    rows = list(current) if current else [(label, "") for label in suggested]
    out = []
    for label, value in rows:
        answer = _ask(p, f"  {label}", value)
        if answer == "-" or not answer:
            continue
        out.append((label, answer))
    while True:
        label = _ask(p, f"  Add a {key} (or Return to finish)")
        if not label:
            return out
        value = _ask(p, f"  {label}")
        if value:
            out.append((label, value))


# --- the walkthrough -----------------------------------------------------------

def run_init(args, p) -> int:
    from .interactive import EXIT_FAIL, EXIT_PASS
    folder = Path(clean_path(args.folder or ".")).expanduser().resolve()
    target = folder / "project.json"
    editing = target.exists()
    if editing and args.from_project:
        raise ConfigError(f"{target} already exists. Run `mit init` there to edit it (without --from).")

    base = _starting_point(target if editing else None, args.from_project)
    source_dir = (target.parent if editing else
                  _project_file(args.from_project).parent if args.from_project else None)
    if editing:
        p.say(f"Editing {target} (current values are the defaults)")
    elif args.from_project:
        p.say(f"New show in {folder}, starting from {_project_file(args.from_project)}")
    else:
        p.say(f"New show in {folder}")

    data = _walk(p, base, folder, source_dir, args)
    lut_copies = data.pop("_lut_copies")
    try:
        config = config_from_dict(data)
    except ConfigError as e:
        p.say(f"\nThese settings aren't valid, so nothing was saved:\n{e}")
        return EXIT_FAIL
    config.base_dir = folder

    p.say("\n" + summary(config, target, pending={dest for _, dest in lut_copies}))
    if not _yes(p, f"Save {target}?", True):
        p.say("Not saved.")
        return EXIT_FAIL
    folder.mkdir(parents=True, exist_ok=True)
    for src, dest in lut_copies:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
    if editing:
        os.replace(target, target.with_name(target.name + PREVIOUS_SUFFIX))
    atomic_write_text(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    p.say(f"Saved {target}" + (f" (the old version is {target.name}{PREVIOUS_SUFFIX})" if editing else ""))
    p.say(f"Next: cd into {folder} and run `mit ingest`.")
    return EXIT_PASS


def _project_file(path: str) -> Path:
    path = Path(clean_path(path)).expanduser()
    return path / "project.json" if path.is_dir() else path


def _starting_point(existing: Optional[Path], from_project: Optional[str]) -> Dict[str, Any]:
    source = existing or (_project_file(from_project) if from_project else None)
    if source is None:
        return {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{source} not found") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"Can't read {source}: {e}") from None
    return data if isinstance(data, dict) else {}


def _walk(p, base: Dict[str, Any], folder: Path, source_dir: Optional[Path], args) -> Dict[str, Any]:
    data: Dict[str, Any] = {"_lut_copies": []}

    p.say("\nShow")
    while True:
        code = _ask(p, "  Show code (3 letters, used in every card name)", str(base.get("show_code", ""))).upper()
        if SHOW_CODE_RE.match(code):
            break
        p.say("  It must be exactly 3 letters, e.g. ABC")
    data["show_code"] = code
    while True:
        name = _ask(p, "  Project name", str(base.get("project_name", "")))
        if name:
            break
    data["project_name"] = name

    fields = [(str(k), str(v)) for k, v in (base.get("header_fields") or {}).items()]
    data["header_fields"] = dict(_pairs(p, "Header fields for the daily card log", fields, SUGGESTED_FIELDS, "field"))
    crew = [(c.get("role", ""), c.get("name", "")) for c in base.get("crew", []) if isinstance(c, dict)]
    data["crew"] = [{"role": r, "name": n} for r, n in _pairs(p, "Crew", crew, SUGGESTED_CREW, "role")]

    dit = next((c["name"] for c in data["crew"] if c["role"].upper() == "DIT"), "")
    current_ops = ", ".join(base.get("operators") or ([dit] if dit else []))
    while True:
        ops = [o.strip() for o in _ask(p, "\nOperators (DIT names, comma-separated)", current_ops).split(",") if o.strip()]
        if ops:
            break
        p.say("  At least one operator is needed")
    data["operators"] = ops

    data["cameras"] = _cameras(p, base.get("cameras") or {}, folder, source_dir, data["_lut_copies"], args)
    data["default_destinations"] = _destinations(p, base.get("default_destinations") or [])
    proxies = dict(base.get("proxies") or {})
    p.say("\nProxies (optional; off means none are ever made unless you run `mit proxies`)")
    proxies["auto"] = _yes(p, "  Make proxies automatically after every offload?", bool(proxies.get("auto", False)))
    while True:
        codec = _ask(p, "  Proxy codec (prores_proxy or h264)", proxies.get("codec", "prores_proxy"))
        if codec in PROXY_CODECS:
            break
    proxies["codec"] = codec
    data["proxies"] = proxies
    _bring_luts(data, folder, source_dir, data["_lut_copies"])
    return data


def _bring_luts(data: Dict[str, Any], folder: Path, source_dir: Optional[Path], lut_copies) -> None:
    """LUTs the settings refer to (e.g. carried over with --from) are copied into this show's folder too."""
    if source_dir is None or source_dir.resolve() == folder.resolve():
        return
    pending = {dest for _, dest in lut_copies}
    entries = [c for c in data["cameras"].values() if isinstance(c, dict)] + [data.get("proxies", {})]
    for entry in entries:
        lut = entry.get("lut")
        if not lut or Path(lut).is_absolute():
            continue
        src, dest = source_dir / lut, folder / lut
        if not src.is_file() or dest in pending or (dest.exists() and filecmp.cmp(src, dest, shallow=False)):
            continue  # a missing LUT shows up in the summary, and `mit config` flags it
        lut_copies.append((src, dest))
        pending.add(dest)


def _camera_label(cam: Dict[str, Any]) -> str:
    bits = [cam.get("model", "")]
    if cam.get("description"):
        bits.append(cam["description"])
    return ", ".join(b for b in bits if b)


def _cameras(p, current: Dict[str, Any], folder: Path, source_dir: Optional[Path], lut_copies, args) -> Dict[str, Any]:
    cams: Dict[str, Dict[str, Any]] = {}
    for cam_id, spec in current.items():
        cams[str(cam_id).upper()] = {"model": spec} if isinstance(spec, str) else dict(spec)
    p.say("\nCameras")
    p.say("  Quick picks for the model: " + "  ".join(f"{n}) {m}" for n, (m, _, _) in enumerate(CAMERA_PRESETS, 1)))
    while True:
        if cams:
            p.say("  " + "   ".join(f"{i}: {_camera_label(c)}" for i, c in cams.items()))
        answer = _ask(p, "  Camera ID to add or change ('-ID' removes, Return when done)").upper()
        if not answer:
            if cams:
                return cams
            p.say("  Add at least one camera")
            continue
        if answer.startswith("-"):
            if cams.pop(answer[1:], None) is None:
                p.say(f"  There's no camera {answer[1:]}")
            continue
        if not CAMERA_ID_RE.match(answer):
            p.say("  A camera ID is 1-3 letters or digits, e.g. A, B, DR1, S")
            continue
        cams[answer] = _camera(p, answer, cams.get(answer, {}), folder, source_dir, lut_copies, args)


def _camera(p, cam_id: str, cam: Dict[str, Any], folder: Path, source_dir, lut_copies, args) -> Dict[str, Any]:
    preset = None
    while True:
        answer = _ask(p, f"  Camera {cam_id} model (number or name)", cam.get("model", ""))
        if answer.isdigit() and 1 <= int(answer) <= len(CAMERA_PRESETS):
            preset = CAMERA_PRESETS[int(answer) - 1]
            answer = preset[0]
        else:
            preset = next((pr for pr in CAMERA_PRESETS if pr[0].lower() == answer.lower()), None)
        if answer:
            break
    out: Dict[str, Any] = {"model": answer}
    for key, label in (("description", "Description (optional, e.g. Timelapse, Gimbal)"),
                       ("shooter", "Shooter (optional)")):
        value = _ask(p, f"  {label}", cam.get(key, ""))
        if value and value != "-":
            out[key] = value
    default_mt = cam.get("media_type") or (preset[1] if preset else "VIDEO")
    while True:
        mt = _ask(p, "  Media type (VIDEO, AUDIO, STILLS)", default_mt).upper()
        mt = next((m for m in MEDIA_TYPES if mt and m.startswith(mt)), "")
        if mt:
            break
    out["media_type"] = mt
    if mt == "VIDEO":
        lut = _choose_lut(p, cam, folder, source_dir, lut_copies, args)
        if lut:
            out["lut"] = lut
            sony = "sony" in out["model"].lower()
            suggest = cam.get("lut_gamma") or _gamma_from_name(lut) or (preset[2] if preset else None) or ""
            if sony:
                gamma = _ask(p, "  Which capture gamma is this LUT for? Other clips won't get it "
                                "('-' = every clip)", suggest)
                if gamma and gamma != "-":
                    out["lut_gamma"] = gamma.lower()
            else:
                p.say("  (This camera's clips don't record their gamma, so the LUT goes on every clip.)")
    return out


def _gamma_from_name(name: str) -> Optional[str]:
    upper = Path(name).name.upper()
    if "SG3C" in upper or "SGAMUT3CINE" in upper or "S-GAMUT3.CINE" in upper:
        return "s-log3-cine"
    if "SL3" in upper or "SLOG3" in upper:
        return "s-log3"
    return None


def _lut_candidates(folder: Path, source_dir: Optional[Path], extra: Optional[str]) -> List[Path]:
    places = [folder / "luts", folder]
    if source_dir:
        places += [source_dir / "luts", source_dir]
    if extra:
        places.append(Path(clean_path(extra)).expanduser())
    places.append(Path.cwd())
    found: List[Path] = []
    for place in places:
        if not place.is_dir():
            continue
        for path in sorted(place.glob("*.cube")) + sorted(place.glob("*/*.cube")):
            if path.resolve() not in {f.resolve() for f in found}:
                found.append(path)
    return found[:20]


def _choose_lut(p, cam, folder: Path, source_dir, lut_copies, args) -> Optional[str]:
    current = cam.get("lut", "")
    candidates = _lut_candidates(folder, source_dir, getattr(args, "luts", None))
    if candidates:
        p.say("  LUTs found: " + "  ".join(f"{n}) {c.name}" for n, c in enumerate(candidates, 1)))
    while True:
        answer = _ask(p, "  Proxy LUT (number, or drag a .cube file in; '-' none)", current or "-")
        if answer == "-":
            return None
        if answer == current and current:
            return current
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            src = candidates[int(answer) - 1]
        else:
            src = Path(clean_path(answer)).expanduser()
            if not src.is_absolute():
                src = folder / src if (folder / src).exists() else (source_dir or folder) / src
        problem = check_lut(src)
        if problem:
            p.say(f"  {problem}")
            continue
        dest = folder / "luts" / src.name
        if src.resolve() != dest.resolve():
            if dest.exists() and not filecmp.cmp(src, dest, shallow=False):
                p.say(f"  A different {src.name} is already in {folder / 'luts'}; rename one of them first")
                continue
            lut_copies.append((src, dest))
        return f"luts/{src.name}"


def _destinations(p, current: List[str]) -> List[str]:
    volumes = sorted(v.name for v in VOLUMES.iterdir()
                     if v.is_dir() and v.name != "Macintosh HD") if VOLUMES.is_dir() else []
    p.say("\nDefault destination drives (offered by `mit ingest`; you can change them per card)")
    if volumes:
        p.say("  Connected now: " + "  ".join(f"{n}) {v}" for n, v in enumerate(volumes, 1)))
    answer = _ask(p, "  Drive names or numbers, comma-separated ('-' none)", ", ".join(current))
    if answer in ("", "-"):
        return []
    names = []
    for part in (a.strip() for a in answer.split(",")):
        if part.isdigit() and 1 <= int(part) <= len(volumes):
            part = volumes[int(part) - 1]
        if part and part not in names:
            names.append(part)
    return names


# --- `mit config` ------------------------------------------------------------------

def summary(config: ProjectConfig, path: Path, pending=frozenset()) -> str:
    """Readable summary. pending: LUT files that will be copied into place on save."""
    lines = [f"project.json: {path}",
             f"Show:        {config.show_code} · {config.project_name}"]
    if config.header_fields:
        lines.append("Header:      " + " · ".join(f"{k} {v}" for k, v in config.header_fields.items()))
    if config.crew:
        lines.append("Crew:        " + " · ".join(f"{m.role} {m.name}" for m in config.crew))
    lines.append("Operators:   " + ", ".join(config.operators))
    lines.append("Cameras:")
    for cam in config.cameras.values():
        extra = ", ".join(x for x in (cam.description, cam.shooter) if x)
        line = f"  {cam.id:<4} {cam.model}" + (f" ({extra})" if extra else "") + f"  {cam.media_type or 'VIDEO?'}"
        lut, gamma = config.lut_for(cam.id)
        if lut is not None and cam.media_type in (None, "VIDEO"):
            state = "ok" if lut in pending or check_lut(lut) is None else "MISSING OR UNREADABLE"
            line += f"  LUT {lut.name}" + (f" for {gamma}" if gamma else " for every clip") + f" [{state}]"
        lines.append(line)
    drives = config.default_destinations
    if drives:
        lines.append("Drives:      " + ", ".join(
            f"{d} ({'connected' if (VOLUMES / d).is_dir() else 'not connected'})" for d in drives))
    else:
        lines.append("Drives:      none set (asked for at each offload)")
    px = config.proxies
    lines.append(f"Proxies:     {'automatic after each offload' if px.auto else 'off (only with mit proxies)'}"
                 f" · {'ProRes Proxy' if px.codec == 'prores_proxy' else 'H.264'} · fit {px.max_width}x{px.max_height}")
    return "\n".join(lines)


def run_config(args, p) -> int:
    from .interactive import EXIT_FAIL, EXIT_PASS
    path = Path(args.config).expanduser()
    config = load_config(path)
    p.say(summary(config, path.resolve()))
    problems = [cam.id for cam in config.cameras.values()
                if config.lut_for(cam.id)[0] is not None and check_lut(config.lut_for(cam.id)[0])]
    if problems:
        p.say(f"\nLUT problem for camera(s) {', '.join(problems)}: proxies for them would not be made.")
        return EXIT_FAIL
    p.say("\nproject.json is valid.")
    return EXIT_PASS
