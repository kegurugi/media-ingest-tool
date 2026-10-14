"""Proxies (optional): edit-friendly copies of each video clip, made with ffmpeg.

Only made by `mit proxies`, or after each offload when project.json sets
proxies.auto. Never for a card that didn't verify.

  - Written to <DRIVE>/<date>/PROXIES/<CARD>/<clip name>.mov
  - ProRes Proxy (default) or H.264, both in .mov, which keeps the timecode
    track and reel name (MP4 drops the reel name)
  - Fit within max_width x max_height (default 1920x1080); never upscaled
  - All audio tracks; start timecode and reel name (= card name) preserved
  - Optional LUT (.cube) burned in. For Sony clips, the clip's capture gamma
    (from its XML) must match the LUT's lut_gamma, or the LUT is skipped
  - Made once, from a verified copy, then copied with hash verification to the
    other destinations. Existing proxies are never overwritten.
  - A failed proxy is a warning, not a card FAIL: the media is still safe.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import diskio
from .config import ProjectConfig
from .copier import copy_card
from .errors import IngestError
from .incomplete import COPYING, read_marker
from .layout import card_record_path, date_dir
from .metadata import read_sony_xml
from .naming import CardName
from .sources import SourceFile, SourceListing
from .spotcheck import load_expected
from .verify import expected_files, verify_destination

PROXIES_DIRNAME = "PROXIES"
OK, FAILED, SKIPPED = "OK", "FAIL", "SKIPPED"

ENCODERS = {  # codec setting -> (hardware encoder, software encoder, extra args, audio codec args)
    "prores_proxy": ("prores_videotoolbox", "prores_ks", ["-profile:v", "proxy"], ["-c:a", "pcm_s16le"]),
    "h264": ("h264_videotoolbox", "libx264", [], ["-c:a", "aac", "-b:a", "192k"]),
}
H264_ARGS = {"h264_videotoolbox": ["-b:v", "10M"], "libx264": ["-crf", "23", "-preset", "fast"]}

ProgressFn = Callable[[int, int, str], None]  # (done units, total units, label)


@dataclass
class ProxyResult:
    clip: str  # rel path of the camera clip in the card folder
    proxy: Optional[Path] = None
    status: str = ""
    detail: str = ""
    lut: str = ""
    lut_note: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    size: int = 0
    xxh64: str = ""
    start_timecode: str = ""


@dataclass
class CardProxies:
    card: str
    drive: Path
    results: List[ProxyResult] = field(default_factory=list)
    copied_to: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    problem: str = ""  # nothing was made, and why
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problem and all(r.status in (OK, SKIPPED) for r in self.results)


def proxy_dir(drive: Path, card: CardName) -> Path:
    return date_dir(drive, card.shoot_date) / PROXIES_DIRNAME / str(card)


def available_encoders() -> set:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.split()[1] for line in out.splitlines() if len(line.split()) > 1}


def check_lut(path: Path) -> Optional[str]:
    """None if the .cube LUT is usable, else what's wrong with it."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(64 * 1024)
    except OSError as e:
        return f"can't read the LUT {path}: {e.strerror or e}"
    if path.suffix.lower() != ".cube" or "LUT_3D_SIZE" not in head:
        return f"{path} isn't a 3D .cube LUT"
    return None


def gamma_matches(clip_gamma: str, lut_gamma: str) -> bool:
    a, b = clip_gamma.lower().replace("_", "-"), lut_gamma.lower().replace("_", "-")
    return a == b or a.startswith(b + "-") or a.startswith(b)


def make_proxies(
    drive: Path,
    card: CardName,
    config: ProjectConfig,
    other_drives: List[Path] = (),
    progress: Optional[ProgressFn] = None,
    encoders: Optional[set] = None,
) -> CardProxies:
    """Make missing proxies for one verified card on `drive`, then copy them to `other_drives`."""
    t0 = time.monotonic()
    drive = Path(drive)
    out = CardProxies(card=str(card), drive=drive)
    expected = load_expected(drive, card)
    if expected.offload_result != "PASS":
        out.problem = f"the card's offload was {expected.offload_result or 'not verified'}; no proxies for unverified media"
        return out
    card_folder = drive / expected.card_path if expected.card_path else None
    if card_folder is None or not card_folder.is_dir():
        out.problem = "the card folder isn't on this drive"
        return out
    marker = read_marker(card_folder)
    if marker is not None and marker.status == COPYING:
        out.problem = "the card is still being offloaded, or that offload was interrupted"
        return out
    clips = _video_clips(drive, card)
    if not clips:
        out.problem = "no video clips on this card"
        return out
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        out.problem = "ffmpeg is not installed (brew install ffmpeg)"
        return out

    lut, lut_gamma = config.lut_for(card.cam)
    if lut is not None:
        trouble = check_lut(lut)
        if trouble:
            out.problem = f"LUT problem, so no proxies were made: {trouble}"
            return out

    settings = config.proxies
    encoders = available_encoders() if encoders is None else encoders
    target = proxy_dir(drive, card)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mit-lut-") as tmp:
        safe_lut = None
        if lut is not None:
            safe_lut = Path(tmp) / "lut.cube"  # a path with no characters ffmpeg's filter syntax cares about
            shutil.copyfile(lut, safe_lut)
        for i, clip in enumerate(clips, 1):
            result = ProxyResult(clip=clip["rel_path"], start_timecode=clip.get("start_timecode") or "")
            out.results.append(result)
            dest = target / (Path(clip["rel_path"]).stem + ".mov")
            result.proxy = dest
            if dest.exists():
                result.status, result.detail = SKIPPED, "a proxy already exists"
                _describe(result, dest)
                continue
            use_lut = safe_lut
            if safe_lut is not None:
                result.lut = lut.name
                gamma = read_sony_xml(card_folder / clip["rel_path"]).get("capture_gamma", "")
                if lut_gamma and gamma and not gamma_matches(gamma, lut_gamma):
                    use_lut, result.lut = None, ""
                    result.lut_note = f"clip is {gamma}, LUT is for {lut_gamma}: LUT not applied"
                elif lut_gamma and not gamma:
                    result.lut_note = f"clip's gamma unknown; LUT for {lut_gamma} applied"
            label = f"[{i}/{len(clips)}] {Path(clip['rel_path']).name}"
            _transcode(card_folder / clip["rel_path"], dest, clip, str(card), settings, use_lut, encoders,
                       result, progress, label)
    _copy_to_others(out, card, other_drives)
    out.elapsed = time.monotonic() - t0
    return out


def _video_clips(drive: Path, card: CardName) -> List[Dict[str, Any]]:
    try:
        record = json.loads(card_record_path(drive, card).read_text(encoding="utf-8"))
        return [c for c in record.get("clips", []) if c.get("kind") == "video"]
    except (OSError, ValueError):
        pass
    from .masterlog import connect
    import contextlib
    try:
        with contextlib.closing(connect(drive)) as conn:
            rows = conn.execute(
                """SELECT c.* FROM clips c JOIN offloads o ON o.id = c.offload_id
                   WHERE o.card = ? AND o.superseded = 0 AND c.kind = 'video' ORDER BY c.rel_path""",
                (str(card),)).fetchall()
            return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001 - no log, no clips
        return []


def _ffmpeg_args(src: Path, tmp_out: Path, clip: Dict[str, Any], reel: str, settings, lut: Optional[Path],
                 encoder: str) -> List[str]:
    vf = []
    if lut is not None:  # a LUT to Rec.709 makes the picture Rec.709; tag the frames so every encoder says so
        vf.append(f"lut3d=file={lut}")
        vf.append("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709")
    w, h = settings.max_width, settings.max_height
    vf.append(f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2")
    codec_key = "prores_proxy" if encoder.startswith("prores") else "h264"
    _, _, extra, audio = ENCODERS[codec_key]
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", ",".join(vf),
            "-c:v", encoder, *extra, *H264_ARGS.get(encoder, []), *audio]
    if clip.get("start_timecode"):
        args += ["-timecode", clip["start_timecode"]]
    args += ["-metadata:s:v:0", f"reel_name={reel}"]
    return args + ["-f", "mov", str(tmp_out)]


def _transcode(src: Path, dest: Path, clip: Dict[str, Any], reel: str, settings, lut: Optional[Path],
               encoders: set, result: ProxyResult, progress: Optional[ProgressFn], label: str) -> None:
    hardware, software, _, _ = ENCODERS[settings.codec]
    candidates = [e for e in (hardware, software) if e in encoders] or [software]
    tmp = dest.with_name(f".{dest.stem}.partial.mov")
    duration_us = int((clip.get("duration") or 0) * 1_000_000)
    error = ""
    for encoder in candidates:
        if tmp.exists():
            tmp.unlink()  # our own unfinished temp file
        try:
            proc = subprocess.Popen(_ffmpeg_args(src, tmp, clip, reel, settings, lut, encoder),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in proc.stdout:
                if progress and line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                    progress(min(int(line.strip()[12:]), duration_us or 1), duration_us or 1, label)
            stderr = proc.stderr.read()
            proc.wait()
        except BaseException:
            if "proc" in locals() and proc.poll() is None:
                proc.kill()
                proc.wait()
            if tmp.exists():
                tmp.unlink()
            raise
        if proc.returncode == 0 and tmp.exists():
            problem = _check_proxy(tmp, clip)
            if not problem:
                os.replace(tmp, dest)
                result.status = OK
                result.codec = encoder
                _describe(result, dest)
                return
            error = problem
        else:
            error = (stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
    if tmp.exists():
        tmp.unlink()
    result.status, result.detail = FAILED, error


def _probe(path: Path) -> Dict[str, Any]:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                              str(path)], capture_output=True, text=True, timeout=60).stdout
        return json.loads(out or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def _check_proxy(path: Path, clip: Dict[str, Any]) -> str:
    """'' if the new proxy looks right, else what's wrong."""
    info = _probe(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return "the proxy has no video"
    try:
        got = float(info.get("format", {}).get("duration", 0))
    except ValueError:
        got = 0
    want = clip.get("duration")
    if want and abs(got - want) > max(0.5, want * 0.01):
        return f"the proxy is {got:.2f}s long but the clip is {want:.2f}s"
    return ""


def _describe(result: ProxyResult, path: Path) -> None:
    info = _probe(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    result.width, result.height = video.get("width", 0), video.get("height", 0)
    if not result.codec:
        result.codec = video.get("codec_name", "")
    result.size = path.stat().st_size


def _copy_to_others(out: CardProxies, card: CardName, other_drives: List[Path]) -> None:
    source_dir = proxy_dir(out.drive, card)
    ready = [r for r in out.results if r.status in (OK, SKIPPED) and r.proxy is not None and r.proxy.exists()]
    if not ready:
        return
    for r in ready:
        r.xxh64 = diskio.hash_file(r.proxy)[0]
    for other in other_drives:
        other = Path(other)
        if not other.is_dir():
            out.warnings.append(f"{other.name} isn't connected; its proxies weren't copied. "
                                "Run `mit proxies` again when it is.")
            continue
        dest_dir = proxy_dir(other, card)
        dest_dir.mkdir(parents=True, exist_ok=True)
        missing = [r for r in ready if not (dest_dir / r.proxy.name).exists()]
        if not missing:
            out.copied_to.append(other.name)
            continue
        listing = SourceListing(root=source_dir, files=[
            SourceFile(rel_path=r.proxy.name, size=r.proxy.stat().st_size, mtime_ns=r.proxy.stat().st_mtime_ns)
            for r in missing])
        try:
            copied = copy_card(listing, [dest_dir])
            check = verify_destination(dest_dir, expected_files(listing.files, copied.hashes),
                                       ignore=[r.proxy.name for r in ready if r not in missing])
        except (IngestError, OSError) as e:
            out.warnings.append(f"Copying proxies to {other.name} failed: {e}")
            continue
        bad = [c for c in check.failures]
        if bad or any(copied.hashes[r.proxy.name] != r.xxh64 for r in missing):
            out.warnings.append(f"Proxy copies on {other.name} did NOT verify: "
                                + ", ".join(f"{c.status} {c.rel_path}" for c in bad))
            continue
        out.copied_to.append(other.name)


def log_rows(out: CardProxies, card: CardName, offload_uid: str, volume: str,
             when: Optional[datetime] = None) -> List[Dict[str, Any]]:
    when = when or datetime.now()
    return [{
        "card": str(card), "shoot_date": card.shoot_date.isoformat(), "offload_uid": offload_uid,
        "clip": r.clip, "proxy_path": str(r.proxy.relative_to(out.drive)) if r.proxy else "",
        "codec": r.codec, "width": r.width, "height": r.height, "size": r.size, "xxh64": r.xxh64,
        "start_timecode": r.start_timecode, "lut": r.lut, "lut_note": r.lut_note,
        "result": r.status, "detail": r.detail, "created_at": when.isoformat(timespec="seconds"), "volume": volume,
    } for r in out.results if r.status != SKIPPED]


def other_destinations(drive: Path, card: CardName) -> List[Path]:
    """The card's other destinations (from its card record), found next to this drive (e.g. in /Volumes)."""
    try:
        record = json.loads(card_record_path(drive, card).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(drive).parent / d["volume"] for d in record.get("destinations", [])
            if d.get("volume") and d["volume"] != Path(drive).name]


def run_for_card(drive: Path, card: CardName, config: ProjectConfig, other_drives: Optional[List[Path]] = None,
                 progress: Optional[ProgressFn] = None) -> CardProxies:
    """Make proxies, copy them to the other destinations, and record the results in each drive's log."""
    from . import masterlog, xlsx_export
    others = other_destinations(drive, card) if other_drives is None else list(other_drives)
    out = make_proxies(drive, card, config, others, progress)
    if out.problem or not any(r.status != SKIPPED for r in out.results):
        return out
    uid = load_expected(drive, card).offload_uid
    when = datetime.now()
    for d in [Path(drive)] + [o for o in others if o.name in out.copied_to]:
        try:
            masterlog.add_proxies(d, log_rows(out, card, uid, d.name, when))
            xlsx_export.export_xlsx(d)
        except Exception as e:  # noqa: BLE001 - recording is best effort; the proxies exist either way
            out.warnings.append(f"Proxies weren't recorded in the log on {d.name}: {e}")
    return out
