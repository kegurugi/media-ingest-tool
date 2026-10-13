"""Propose VIDEO, AUDIO or STILLS for a card. The operator always decides.

Rules:
  1. Any video files on the card -> VIDEO. Losing video in post is the worst case.
  2. No video -> the camera's configured media_type, if set (e.g. a timelapse
     camera is VIDEO even though its card holds only stills).
  3. Otherwise detect: only audio -> AUDIO, only stills -> STILLS, else no proposal.

A card is never split: the media type only picks which folder it goes in.
Scratch audio embedded in video files doesn't count as audio, because
detection looks at files, not streams.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import List, Optional, Union

from .config import MEDIA_TYPES, Camera
from .errors import IngestError

VIDEO_EXTENSIONS = frozenset(
    {".mxf", ".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".crm", ".r3d", ".braw", ".ari", ".insv"}
)
AUDIO_EXTENSIONS = frozenset({".wav", ".bwf", ".aif", ".aiff", ".flac", ".mp3"})
STILLS_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff",
     ".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".orf", ".rw2"}
)

# Camera folder structures that mark a video card. Images inside these trees
# are thumbnails, not stills, and are not counted as stills.
VIDEO_STRUCTURES = ("PRIVATE/M4ROOT", "PRIVATE/XDROOT", "PRIVATE/AVCHD", "XDROOT", "CONTENTS")


@dataclass
class CardScan:
    video: int = 0
    audio: int = 0
    stills: int = 0
    other: int = 0
    structures: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.video or self.audio or self.stills)

    def summary(self) -> str:
        parts = [f"{self.video:,} video clips", f"{self.audio:,} audio files", f"{self.stills:,} stills"]
        if self.structures:
            parts.append(f"structure: {', '.join(self.structures)}")
        return ", ".join(parts)


@dataclass
class MediaTypeProposal:
    media_type: Optional[str]  # None: no proposal, the operator must pick
    reason: str
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)  # show loudly


def scan_card(root: Union[str, Path]) -> CardScan:
    """Count media files on a card by extension.

    Only lists directories; never opens, writes or modifies anything on the card.
    Hidden files and folders (._*, .Spotlight-V100, .Trashes, ...) are ignored.
    """
    root = Path(root)
    if not root.is_dir():
        raise IngestError(f"Source card {root} not found or not a folder")

    scan = CardScan(structures=[s for s in VIDEO_STRUCTURES if (root / s).is_dir()])

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = PurePosixPath(*Path(dirpath).relative_to(root).parts)
        for name in filenames:
            if name.startswith("."):
                continue
            kind = classify_file(str(rel_dir / name))
            if kind in (VIDEO, PROXY):
                scan.video += 1
            elif kind == AUDIO:
                scan.audio += 1
            elif kind == STILL:
                scan.stills += 1
            else:
                scan.other += 1
    return scan


VIDEO, PROXY, AUDIO, STILL = "video", "proxy", "audio", "still"

_VIDEO_STRUCTURE_PARTS = [PurePosixPath(s).parts for s in VIDEO_STRUCTURES]


def classify_file(rel_path: str) -> Optional[str]:
    """What a file on a card is: video, proxy, audio, still, or None (sidecars, system files).

    Images inside camera video structures are thumbnails, not stills. Sony's
    low-res proxies (M4ROOT/SUB) are PROXY, so they aren't listed as separate clips.
    """
    parts = tuple(p.upper() for p in PurePosixPath(rel_path).parts)
    ext = os.path.splitext(rel_path)[1].lower()
    in_video_tree = any(parts[: len(s)] == s for s in _VIDEO_STRUCTURE_PARTS)
    if ext in VIDEO_EXTENSIONS:
        if "M4ROOT" in parts and "SUB" in parts:
            return PROXY
        return VIDEO
    if ext in AUDIO_EXTENSIONS:
        return AUDIO
    if ext in STILLS_EXTENSIONS and not in_video_tree:
        return STILL
    return None


def propose_media_type(scan: CardScan, camera: Optional[Camera] = None) -> MediaTypeProposal:
    configured = camera.media_type if camera else None
    cam_label = _camera_label(camera) if camera else ""

    if scan.video:
        proposal = MediaTypeProposal("VIDEO", f"card contains video ({scan.summary()})")
        if configured == "AUDIO":
            proposal.warnings.append(
                f"{cam_label} is an AUDIO camera but this card contains video. "
                "Check the camera ID and that the right card is in the reader."
            )
        elif configured and configured != "VIDEO":
            proposal.notes.append(f"{cam_label} is configured as {configured}, but video always goes to VIDEO")
        return proposal

    if configured:
        proposal = MediaTypeProposal(configured, f"{cam_label} is configured as {configured} ({scan.summary()})")
        if scan.is_empty:
            proposal.notes.append("no recognised media files on this card")
        elif configured == "AUDIO" and not scan.audio:
            proposal.notes.append("no audio files found on this AUDIO camera's card")
        elif configured == "STILLS" and not scan.stills:
            proposal.notes.append("no stills found on this STILLS camera's card")
        return proposal

    if scan.audio and not scan.stills:
        return MediaTypeProposal("AUDIO", f"card contains only audio ({scan.summary()})")
    if scan.stills and not scan.audio:
        return MediaTypeProposal("STILLS", f"card contains only stills ({scan.summary()})")
    if scan.is_empty:
        return MediaTypeProposal(None, "no recognised media files on this card")
    return MediaTypeProposal(None, f"card contains both audio and stills ({scan.summary()})")


def choice_warnings(scan: CardScan, chosen: str) -> List[str]:
    """Loud warnings about the operator's final choice of media type."""
    if chosen not in MEDIA_TYPES:
        raise ValueError(f"media type must be one of {', '.join(MEDIA_TYPES)}, got {chosen!r}")
    if scan.video and chosen != "VIDEO":
        return [
            f"This card contains {scan.video:,} video clip(s) but is being filed under {chosen}. "
            "Video outside VIDEO may be missed in post."
        ]
    return []


def _camera_label(camera: Camera) -> str:
    label = f"camera {camera.id}"
    if camera.description:
        label += f" ({camera.description})"
    return label
