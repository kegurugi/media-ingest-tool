"""Per-clip metadata from exiftool and ffprobe.

Fields: filename, relative path, size, xxHash64, duration, start timecode,
codec, resolution, frame rate, creation date/time (plus camera model and, for
audio, sample rate and channels).

Rules:
  - A field nothing provides stays blank. Values are never guessed.
  - Every value records where it came from (`sources`).
  - Dates are kept exactly as the camera wrote them. MP4/MOV container times
    are labelled UTC, but many cameras write local clock time there, so they
    are NOT converted; the source note says so.
  - exiftool and ffprobe only read files.

Sony XML sidecars (<clip>M01.XML, next to the clip in M4ROOT/CLIP or
XDROOT/Clip) are the most trusted source for the recording time and camera:
they carry local time with its UTC offset. The FX6's MXF container time is
UTC, so without the sidecar a clip's date could be a day off. (Canon's C80 MP4
sidecar holds nothing the clip itself doesn't, so it isn't needed.)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import timecode as tc
from .mediatype import AUDIO, STILL, VIDEO, classify_file
from .sources import SourceFile

EXIFTOOL_BATCH = 500
FFPROBE_WORKERS = 4
TOOL_TIMEOUT = 300

# Tags requested from exiftool (asking for specific tags is much faster than all).
EXIF_TAGS = [
    "DateTimeOriginal", "OffsetTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate",
    "Make", "Model", "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
    "Duration", "VideoFrameRate", "TimeReference", "FileType", "Originator",
]

SRC_EXIF = "exiftool"
SRC_FFPROBE = "ffprobe"
SRC_IXML = "BWF iXML"
SRC_SONY_XML = "Sony XML"


@dataclass
class ClipMetadata:
    rel_path: str
    filename: str
    kind: str  # video, audio or still
    size: int
    xxh64: str = ""
    duration: Optional[float] = None  # seconds
    start_timecode: str = ""
    end_timecode: str = ""  # start + duration: the out point, as DaVinci Resolve shows it
    codec: str = ""
    resolution: str = ""
    frame_rate: str = ""
    created: str = ""  # ISO 8601 as written by the camera, with offset only if the camera gave one
    camera_model: str = ""
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    sources: Dict[str, str] = field(default_factory=dict)  # field -> where the value came from
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolStatus:
    exiftool: Optional[str]
    ffprobe: Optional[str]

    @property
    def missing(self) -> List[str]:
        return [name for name in ("exiftool", "ffprobe") if getattr(self, name) is None]


def find_tools() -> ToolStatus:
    return ToolStatus(exiftool=shutil.which("exiftool"), ffprobe=shutil.which("ffprobe"))


def select_clips(files: Iterable[SourceFile]) -> List[Tuple[SourceFile, str]]:
    """Files that count as clips, with their kind. Proxies and sidecars are left out."""
    clips = []
    for f in files:
        kind = classify_file(f.rel_path)
        if kind in (VIDEO, AUDIO, STILL):
            clips.append((f, kind))
    return clips


# --- exiftool ---------------------------------------------------------------

def read_exif(root: Path, rel_paths: List[str], exiftool: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Run exiftool over files under root. Returns rel_path -> {tag: value}.

    Group prefixes (e.g. 'Track1:') are dropped; the first value of each tag wins.
    Missing exiftool, or a file it can't read, just gives no tags.
    """
    exiftool = exiftool or shutil.which("exiftool")
    if not exiftool or not rel_paths:
        return {}
    root = Path(root)
    results: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(rel_paths), EXIFTOOL_BATCH):
        batch = rel_paths[start:start + EXIFTOOL_BATCH]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as argfile:
            argfile.write("\n".join(str(root / rel) for rel in batch) + "\n")
        try:
            proc = subprocess.run(
                [exiftool, "-json", "-G1", "-n", "-api", "largefilesupport=1", "-charset", "filename=utf8",
                 *[f"-{t}" for t in EXIF_TAGS], "-@", argfile.name],
                capture_output=True, text=True, timeout=TOOL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        finally:
            os.unlink(argfile.name)  # our own temp file, not media
        try:
            entries = json.loads(proc.stdout or "[]")
        except ValueError:
            continue
        by_path = {str(root / rel): rel for rel in batch}
        for entry in entries:
            rel = by_path.get(entry.get("SourceFile", ""))
            if rel is None:
                continue
            tags: Dict[str, Any] = {}
            for key, value in entry.items():
                name = key.split(":")[-1]
                if name != "SourceFile" and name not in tags:
                    tags[name] = value
            results[rel] = tags
    return results


_EXIF_DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_OFFSET = re.compile(r"^[+-]\d{2}:\d{2}$")


def _exif_datetime(value: Any, offset: Any = None) -> Optional[str]:
    """'2026:10:12 23:10:00' (+ optional offset) -> ISO string; None for empty/zero dates."""
    m = _EXIF_DATE.match(str(value or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    if y == "0000" or mo == "00" or d == "00":
        return None
    try:
        datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    iso = f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    if tz and tz != "Z":
        iso += tz
    elif offset and _OFFSET.match(str(offset)):
        iso += str(offset)
    return iso


def created_from_exif(tags: Dict[str, Any], kind: str) -> Tuple[Optional[str], str]:
    """Best creation time from exiftool tags, and a note on its source."""
    original = _exif_datetime(tags.get("DateTimeOriginal"), tags.get("OffsetTimeOriginal"))
    if original:
        label = "BWF origination date/time" if kind == AUDIO else "DateTimeOriginal"
        return original, f"{SRC_EXIF} {label}"
    for tag in ("CreateDate", "MediaCreateDate", "TrackCreateDate"):
        value = _exif_datetime(tags.get(tag))
        if value:
            return value, f"{SRC_EXIF} {tag} (container time, kept as written, not converted from UTC)"
    return None, ""


def camera_model(tags: Dict[str, Any]) -> str:
    """'SONY' + 'ILCE-7SM3' -> 'SONY ILCE-7SM3'; 'Canon' + 'Canon EOS C80' -> 'Canon EOS C80'."""
    make, model = str(tags.get("Make") or "").strip(), str(tags.get("Model") or "").strip()
    if make and model.lower().startswith(make.lower()):
        return model
    return " ".join(p for p in (make, model) if p)


def clip_datetimes(exif: Dict[str, Dict[str, Any]], clips: List[Tuple[SourceFile, str]],
                   root: Optional[Path] = None) -> List[datetime]:
    """Clip creation times in the camera's local time, for proposing the shoot date.

    With root, Sony XML sidecars are read first (local time with its offset).
    """
    times = []
    for f, kind in clips:
        created = None
        if root is not None and kind == VIDEO:
            created = read_sony_xml(Path(root) / f.rel_path).get("created")
        if not created:
            created, _ = created_from_exif(exif.get(f.rel_path, {}), kind)
        if created:
            times.append(datetime.fromisoformat(created[:19]))
    return times


# --- Sony XML sidecars --------------------------------------------------------

def sony_sidecar(clip_path: Path) -> Optional[Path]:
    """C0001.MP4 -> C0001M01.XML; A002C001_2610059N.MXF -> A002C001_2610059NM01.XML."""
    clip_path = Path(clip_path)
    for suffix in (".XML", ".xml"):
        candidate = clip_path.with_name(f"{clip_path.stem}M01{suffix}")
        if candidate.is_file():
            return candidate
    return None


def sony_ltc(value: str) -> Tuple[str, bool]:
    """Sony LtcChange value (BCD bytes, frames first) -> ('HH:MM:SS:FF', drop_frame)."""
    raw = bytes.fromhex(value.strip())
    if len(raw) != 4:
        raise ValueError(f"not a Sony LTC value: {value!r}")
    ff, ss, mm, hh = raw
    drop = bool(ff & 0x40)

    def bcd(byte: int, mask: int) -> int:
        byte &= mask
        tens, ones = byte >> 4, byte & 0x0F
        if ones > 9:
            raise ValueError(f"not BCD: {value!r}")
        return tens * 10 + ones

    hh_, mm_, ss_, ff_ = bcd(hh, 0x3F), bcd(mm, 0x7F), bcd(ss, 0x7F), bcd(ff, 0x3F)
    return f"{hh_:02d}:{mm_:02d}:{ss_:02d}{';' if drop else ':'}{ff_:02d}", drop


def read_sony_xml(clip_path: Path) -> Dict[str, str]:
    """Fields from a clip's Sony NonRealTimeMeta sidecar: created, camera_model, start_timecode.

    Returns {} if there's no sidecar or it can't be read. Namespaces are ignored.
    """
    sidecar = sony_sidecar(clip_path)
    if sidecar is None:
        return {}
    try:
        root = ET.parse(sidecar).getroot()
    except (OSError, ET.ParseError):
        return {}
    if _local(root.tag) != "NonRealTimeMeta":
        return {}
    found: Dict[str, str] = {"sidecar": sidecar.name}
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "CreationDate" and "created" not in found:
            value = (el.get("value") or "").strip()
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                found["created"] = value.replace("Z", "+00:00")
            except ValueError:
                pass
        elif tag == "Device" and "camera_model" not in found:
            maker, model = (el.get("manufacturer") or "").strip(), (el.get("modelName") or "").strip()
            name = model if maker and model.lower().startswith(maker.lower()) else " ".join(p for p in (maker, model) if p)
            if name:
                found["camera_model"] = name
            if el.get("serialNo"):
                found["camera_serial"] = el.get("serialNo").strip()
        elif tag == "LtcChange" and "start_timecode" not in found and el.get("frameCount") == "0":
            try:
                found["start_timecode"] = sony_ltc(el.get("value") or "")[0]
            except ValueError:
                pass
    return found


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# --- ffprobe ----------------------------------------------------------------

def run_ffprobe(path: Path, ffprobe: Optional[str] = None) -> Optional[Dict[str, Any]]:
    ffprobe = ffprobe or shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT,
        )
        return json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _probe_timecode(probe: Dict[str, Any]) -> str:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("tags", {}).get("timecode"):
            return stream["tags"]["timecode"]
    for stream in probe.get("streams", []):
        if stream.get("tags", {}).get("timecode"):
            return stream["tags"]["timecode"]
    return probe.get("format", {}).get("tags", {}).get("timecode", "")


def _float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


# --- BWF iXML ---------------------------------------------------------------

def read_ixml(path: Path, max_chunk: int = 1024 * 1024) -> Optional[ET.Element]:
    """The iXML chunk of a WAV/BWF file, parsed; None if absent or unreadable."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
                return None
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    return None
                cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if cid == b"iXML":
                    if size > max_chunk:
                        return None
                    data = f.read(size).rstrip(b"\0")
                    return ET.fromstring(data)
                if size == 0xFFFFFFFF:  # RF64 data chunk; iXML after it isn't reachable without ds64
                    return None
                f.seek(size + (size & 1), os.SEEK_CUR)
    except (OSError, ET.ParseError):
        return None


def _ixml_text(root: ET.Element, *paths: str) -> str:
    for p in paths:
        node = root.find(p)
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return ""


# --- assembling a clip ------------------------------------------------------

def extract_clip(
    card_dir: Path, f: SourceFile, kind: str, xxh64: str, exif_tags: Dict[str, Any], tools: ToolStatus
) -> ClipMetadata:
    clip = ClipMetadata(
        rel_path=f.rel_path, filename=f.rel_path.rsplit("/", 1)[-1], kind=kind, size=f.size, xxh64=xxh64
    )
    path = Path(card_dir) / f.rel_path

    def put(name: str, value: Any, source: str) -> None:
        if value in (None, "") or getattr(clip, name) not in (None, ""):
            return
        setattr(clip, name, value)
        clip.sources[name] = source

    sony = read_sony_xml(path) if kind == VIDEO else {}
    if sony:
        put("created", sony.get("created"), f"{SRC_SONY_XML} CreationDate ({sony['sidecar']})")
        model = sony.get("camera_model", "")
        if model and sony.get("camera_serial"):
            model += f" (S/N {sony['camera_serial']})"
        put("camera_model", model, f"{SRC_SONY_XML} Device")
    created, created_src = created_from_exif(exif_tags, kind)
    put("created", created, created_src)
    put("camera_model", camera_model(exif_tags), SRC_EXIF)

    if kind == STILL:
        width = exif_tags.get("ExifImageWidth") or exif_tags.get("ImageWidth")
        height = exif_tags.get("ExifImageHeight") or exif_tags.get("ImageHeight")
        if width and height:
            put("resolution", f"{width}x{height}", SRC_EXIF)
        put("codec", exif_tags.get("FileType"), SRC_EXIF)
        return clip

    probe = run_ffprobe(path, tools.ffprobe) if tools.ffprobe else None
    if probe:
        streams = probe.get("streams", [])
        main = next((s for s in streams if s.get("codec_type") == ("video" if kind == VIDEO else "audio")), None)
        if main:
            codec = main.get("codec_name", "")
            if codec and main.get("profile") and kind == VIDEO:
                codec += f" ({main['profile']})"
            put("codec", codec, SRC_FFPROBE)
            if kind == VIDEO:
                if main.get("width") and main.get("height"):
                    put("resolution", f"{main['width']}x{main['height']}", SRC_FFPROBE)
                rate = tc.parse_rate(main.get("r_frame_rate")) or tc.parse_rate(main.get("avg_frame_rate"))
                put("frame_rate", tc.format_rate(rate), SRC_FFPROBE)
            else:
                put("sample_rate", int(main["sample_rate"]) if main.get("sample_rate") else None, SRC_FFPROBE)
                put("channels", main.get("channels"), SRC_FFPROBE)
        put("duration", _float(probe.get("format", {}).get("duration")), SRC_FFPROBE)
        if kind == VIDEO:
            put("start_timecode", _probe_timecode(probe), SRC_FFPROBE)
    if sony.get("start_timecode"):
        if not clip.start_timecode:
            put("start_timecode", sony["start_timecode"], f"{SRC_SONY_XML} LtcChangeTable")
        elif sony["start_timecode"] != clip.start_timecode:
            clip.notes.append(f"Start TC in the clip ({clip.start_timecode}) differs from its Sony XML "
                              f"({sony['start_timecode']})")
    elif tools.ffprobe:
        clip.notes.append("ffprobe could not read this file")

    # exiftool fallbacks
    put("duration", _float(exif_tags.get("Duration")), SRC_EXIF)
    if kind == VIDEO:
        width, height = exif_tags.get("ImageWidth"), exif_tags.get("ImageHeight")
        if width and height:
            put("resolution", f"{width}x{height}", SRC_EXIF)
        put("frame_rate", tc.format_rate(tc.parse_rate(exif_tags.get("VideoFrameRate"))), SRC_EXIF)

    if kind == AUDIO:
        _audio_timecode(clip, path, exif_tags)
    end = tc.end_timecode(clip.start_timecode, clip.duration, clip.frame_rate)
    if end:
        clip.end_timecode = end
        clip.sources["end_timecode"] = "computed: start TC + duration"
    return clip


def _audio_timecode(clip: ClipMetadata, path: Path, exif_tags: Dict[str, Any]) -> None:
    """BWF timecode = time reference (samples since midnight) at the recorder's timecode rate.

    The rate must come from the file's iXML; without it the field stays blank.
    """
    time_ref = exif_tags.get("TimeReference")
    if time_ref in (None, ""):
        return
    ixml = read_ixml(path)
    rate_text = flag = ""
    sample_rate = clip.sample_rate
    if ixml is not None:
        rate_text = _ixml_text(ixml, "SPEED/TIMECODE_RATE")
        flag = _ixml_text(ixml, "SPEED/TIMECODE_FLAG").upper()
        ixml_sr = _ixml_text(ixml, "SPEED/TIMESTAMP_SAMPLE_RATE", "SPEED/FILE_SAMPLE_RATE")
        if ixml_sr.isdigit():
            sample_rate = int(ixml_sr)
    rate = tc.parse_rate(rate_text)
    if rate is None or not sample_rate:
        clip.notes.append("BWF time reference present, but no timecode rate in iXML; timecode left blank")
        return
    try:
        clip.start_timecode = tc.samples_to_timecode(int(time_ref), sample_rate, rate, drop_frame=flag == "DF")
    except ValueError as e:
        clip.notes.append(f"BWF timecode not computed: {e}")
        return
    clip.sources["start_timecode"] = f"{SRC_IXML} rate {tc.format_rate(rate)} {flag or 'NDF'} + {SRC_EXIF} TimeReference"
    if not clip.frame_rate:
        clip.frame_rate = tc.format_rate(rate)
        clip.sources["frame_rate"] = f"{SRC_IXML} timecode rate"


def extract_card(
    card_dir: Path,
    files: Iterable[SourceFile],
    hashes: Dict[str, str],
    exif: Optional[Dict[str, Dict[str, Any]]] = None,
    tools: Optional[ToolStatus] = None,
) -> List[ClipMetadata]:
    """Metadata for every clip in a (verified) card folder, in card order."""
    tools = tools or find_tools()
    clips = select_clips(files)
    if exif is None:
        exif = read_exif(card_dir, [f.rel_path for f, _ in clips], tools.exiftool)
    with ThreadPoolExecutor(max_workers=FFPROBE_WORKERS) as pool:
        futures = [
            pool.submit(extract_clip, card_dir, f, kind, hashes.get(f.rel_path, ""), exif.get(f.rel_path, {}), tools)
            for f, kind in clips
        ]
        return [fut.result() for fut in futures]
