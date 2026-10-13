"""Read-back verification: re-read every destination file and compare to the source hash.

A copy only counts as verified when the destination bytes, read back from the
drive, hash to the same xxHash64 as the source. Sizes alone never count.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from . import diskio
from .sources import is_macos_clutter

OK = "OK"
MISMATCH = "MISMATCH"  # hash differs
MISSING = "MISSING"
SIZE = "SIZE"  # size differs (hash not needed to know it's wrong)
ERROR = "ERROR"  # couldn't be read


@dataclass(frozen=True)
class ExpectedFile:
    rel_path: str
    size: int
    xxh64: str


@dataclass
class FileCheck:
    rel_path: str
    status: str
    expected: str
    actual: Optional[str] = None
    detail: str = ""


@dataclass
class DestinationVerification:
    card_dir: Path
    checks: List[FileCheck] = field(default_factory=list)
    missing_dirs: List[str] = field(default_factory=list)
    extra_files: List[str] = field(default_factory=list)
    # macOS clutter found in the folder (e.g. the ._ AppleDouble files macOS
    # writes next to every new file on exFAT). Not media, so not a failure.
    ignored_files: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            all(c.status == OK for c in self.checks)
            and not self.missing_dirs
            and not self.extra_files
        )

    @property
    def failures(self) -> List[FileCheck]:
        return [c for c in self.checks if c.status != OK]

    def statuses(self) -> Dict[str, str]:
        """rel_path -> status for every checked file."""
        return {c.rel_path: c.status for c in self.checks}


def verify_destination(
    card_dir: Path,
    expected: Sequence[ExpectedFile],
    expected_dirs: Iterable[str] = (),
    ignore: Iterable[str] = (),
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> DestinationVerification:
    """Re-hash every expected file in card_dir. Also flags missing folders and extra files.

    progress(bytes_done, bytes_total, rel_path) is called after each file.
    """
    card_dir = Path(card_dir)
    result = DestinationVerification(card_dir=card_dir)
    total = sum(e.size for e in expected)
    done = 0

    for exp in expected:
        done += exp.size
        if progress:
            progress(done, total, exp.rel_path)
        path = card_dir / exp.rel_path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            result.checks.append(FileCheck(exp.rel_path, MISSING, exp.xxh64))
            continue
        except OSError as e:
            result.checks.append(FileCheck(exp.rel_path, ERROR, exp.xxh64, detail=str(e)))
            continue
        if size != exp.size:
            result.checks.append(
                FileCheck(exp.rel_path, SIZE, exp.xxh64, detail=f"expected {exp.size} bytes, found {size}")
            )
            continue
        try:
            actual, _ = diskio.hash_file(path)
        except OSError as e:
            result.checks.append(FileCheck(exp.rel_path, ERROR, exp.xxh64, detail=str(e)))
            continue
        status = OK if actual == exp.xxh64 else MISMATCH
        result.checks.append(FileCheck(exp.rel_path, status, exp.xxh64, actual))

    result.missing_dirs = sorted(d for d in expected_dirs if not (card_dir / d).is_dir())

    known = {e.rel_path for e in expected} | set(ignore)
    for dirpath, _, filenames in os.walk(card_dir):
        rel_dir = PurePosixPath(*Path(dirpath).relative_to(card_dir).parts)
        for name in filenames:
            rel = str(rel_dir / name)
            if rel not in known:
                if any(is_macos_clutter(part) for part in PurePosixPath(rel).parts):
                    result.ignored_files.append(rel)
                else:
                    result.extra_files.append(rel)
    result.extra_files.sort()
    result.ignored_files.sort()
    return result


def verify_destinations(
    card_dirs: Sequence[Path],
    expected: Sequence[ExpectedFile],
    expected_dirs: Iterable[str] = (),
    ignore: Iterable[str] = (),
) -> List[DestinationVerification]:
    """Verify every destination in parallel (they're separate drives)."""
    expected_dirs = list(expected_dirs)
    ignore = list(ignore)
    with ThreadPoolExecutor(max_workers=max(1, len(card_dirs))) as pool:
        futures = [pool.submit(verify_destination, d, expected, expected_dirs, ignore) for d in card_dirs]
        return [f.result() for f in futures]


def expected_files(files: Iterable, hashes: Dict[str, str]) -> List[ExpectedFile]:
    """ExpectedFile list from SourceFile entries and the source hashes."""
    return [ExpectedFile(f.rel_path, f.size, hashes[f.rel_path]) for f in files]
