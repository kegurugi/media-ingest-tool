"""List what to copy from a source card. Read-only: nothing on the card is opened or changed."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import List, Union

from .errors import SourceError

# Folders and files macOS creates on any mounted volume. The camera never wrote
# them, so they're skipped by default (copy_all=True copies them anyway).
MACOS_CLUTTER = frozenset(
    {
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        ".DS_Store",
        ".apdisk",
    }
)


def is_macos_clutter(name: str) -> bool:
    return name in MACOS_CLUTTER or name.startswith("._")


@dataclass(frozen=True)
class SourceFile:
    rel_path: str  # POSIX-style, relative to the card root
    size: int
    mtime_ns: int


@dataclass
class SourceListing:
    root: Path
    files: List[SourceFile] = field(default_factory=list)
    dirs: List[str] = field(default_factory=list)  # every folder, parents before children
    skipped: List[str] = field(default_factory=list)  # clutter left behind, folders end in "/"

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def list_source(root: Union[str, Path], copy_all: bool = False) -> SourceListing:
    """Walk the card and record every file and folder to copy.

    Empty folders are kept so the card structure is reproduced exactly.
    Symlinks, special files and unreadable files stop the offload before
    anything is copied, rather than being silently left out.
    """
    root = Path(root)
    if not root.is_dir():
        raise SourceError(f"Source card {root} not found or not a folder")

    listing = SourceListing(root=root)

    def walk_error(err: OSError) -> None:
        raise SourceError(f"Cannot read {err.filename} on the source card: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        current = Path(dirpath)
        rel_dir = PurePosixPath(*current.relative_to(root).parts)

        kept = []
        for name in sorted(dirnames):
            rel = str(rel_dir / name)
            if not copy_all and is_macos_clutter(name):
                listing.skipped.append(rel + "/")
                continue
            if (current / name).is_symlink():
                raise SourceError(f"Source card contains a symlink, which the tool won't follow: {rel}")
            kept.append(name)
            listing.dirs.append(rel)
        dirnames[:] = kept

        for name in sorted(filenames):
            rel = str(rel_dir / name)
            if not copy_all and is_macos_clutter(name):
                listing.skipped.append(rel)
                continue
            path = current / name
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise SourceError(f"Source card contains a symlink, which the tool won't follow: {rel}")
            if not stat.S_ISREG(st.st_mode):
                raise SourceError(f"Source card contains a special file that can't be copied: {rel}")
            if not os.access(path, os.R_OK):
                raise SourceError(f"Source file is not readable: {rel}")
            listing.files.append(SourceFile(rel_path=rel, size=st.st_size, mtime_ns=st.st_mtime_ns))

    return listing


def volume_name(path: Union[str, Path]) -> str:
    """Name of the volume holding path, e.g. 'A001' for /Volumes/A001/PRIVATE.

    For a folder on the boot disk (e.g. a test copy of a card), the folder's own name.
    """
    path = Path(path).resolve()
    if len(path.parts) > 2 and path.parts[1] == "Volumes":
        return path.parts[2]
    return path.name or str(path)
