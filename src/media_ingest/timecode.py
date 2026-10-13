"""SMPTE timecode arithmetic (non-drop and drop-frame)."""
from __future__ import annotations

from fractions import Fraction
from typing import Optional, Union


def parse_rate(text: Union[str, int, float, None]) -> Optional[Fraction]:
    """'30000/1001', '25', '29.97' -> Fraction. None if missing or not a positive rate."""
    if text is None:
        return None
    try:
        rate = Fraction(str(text).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def format_rate(rate: Optional[Fraction]) -> str:
    """Display a frame rate: 25, 23.976, 29.97, 59.94."""
    if rate is None:
        return ""
    return f"{float(rate):.3f}".rstrip("0").rstrip(".")


def nominal_fps(rate: Fraction) -> int:
    """Timecode frame count base: 30 for 29.97, 24 for 23.976."""
    return round(rate)


def frames_to_timecode(frames: int, nominal: int, drop_frame: bool = False) -> str:
    """Convert a frame count to HH:MM:SS:FF (or HH:MM:SS;FF for drop-frame).

    Drop-frame applies to 29.97 (drops 2 frame numbers a minute) and 59.94
    (drops 4), except every tenth minute.
    """
    if frames < 0:
        raise ValueError("frame count must not be negative")
    if drop_frame:
        if nominal % 30:
            raise ValueError(f"drop-frame timecode needs 30 or 60 fps, got {nominal}")
        drop = nominal // 15
        per_10_min = nominal * 600 - drop * 9
        per_min = nominal * 60 - drop
        tens, rem = divmod(frames, per_10_min)
        frames += drop * 9 * tens
        if rem > drop:
            frames += drop * ((rem - drop) // per_min)
    ff = frames % nominal
    ss = frames // nominal % 60
    mm = frames // (nominal * 60) % 60
    hh = frames // (nominal * 3600) % 24
    sep = ";" if drop_frame else ":"
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


def samples_to_timecode(
    time_reference: int, sample_rate: int, rate: Fraction, drop_frame: bool = False
) -> str:
    """BWF time reference (samples since midnight) -> timecode at the given frame rate.

    Uses the frame that contains the first sample (rounded down), with exact
    fractions throughout.
    """
    frames = int(Fraction(time_reference) * rate / sample_rate)
    return frames_to_timecode(frames, nominal_fps(rate), drop_frame)
