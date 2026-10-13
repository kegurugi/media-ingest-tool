"""Which physical disk(s) a volume really lives on (macOS).

Two volumes can share one physical disk: two partitions on a drive, or two
APFS volumes in one container. Copies on them would both be lost if that drive
failed, so the separate-drives check compares physical disks, not volumes.

  - A plain partition (exFAT, HFS+, ...): the whole disk it's on.
  - An APFS volume: its container's physical store(s) -> their whole disks.
    (The container itself, e.g. disk3, is a virtual disk.)
  - A disk image: the physical disk(s) the image file lives on.

Everything comes from `diskutil info -plist` and `hdiutil info -plist`, which
only read. If a volume's disk can't be determined (e.g. a network share), the
result is marked unknown.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional

# runner(args) -> parsed plist dict, or None if the command failed
Runner = Callable[[List[str]], Optional[Dict[str, Any]]]

MAX_DEPTH = 4  # disk image inside a disk image inside ...


def _run_plist(args: List[str]) -> Optional[Dict[str, Any]]:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return plistlib.loads(proc.stdout)
    except Exception:  # noqa: BLE001 - any parse problem means "unknown"
        return None


@dataclass(frozen=True)
class DiskIdentity:
    volume: Path
    disks: FrozenSet[str] = field(default_factory=frozenset)  # e.g. {"disk6"}
    description: str = ""  # e.g. "disk6 · USB · WD Game Drive"
    reason: str = ""  # why the disk is unknown

    @property
    def known(self) -> bool:
        return bool(self.disks)


def mount_point(path: Path) -> Path:
    path = Path(path).resolve()
    while not os.path.ismount(path) and path != path.parent:
        path = path.parent
    return path


def identify(volume: Path, run: Runner = _run_plist) -> DiskIdentity:
    """The physical disk(s) holding volume (a mount point or any folder on it)."""
    volume = Path(volume)
    mount = mount_point(volume)
    info = run(["diskutil", "info", "-plist", str(mount)])
    if not info or not info.get("DeviceIdentifier"):
        return DiskIdentity(volume, reason="not a local disk the Mac can identify (a network share?)")
    images: Optional[Dict[str, str]] = None

    def image_map() -> Dict[str, str]:
        nonlocal images
        if images is None:
            images = {}
            data = run(["hdiutil", "info", "-plist"]) or {}
            for im in data.get("images", []):
                for entity in im.get("system-entities", []):
                    dev = str(entity.get("dev-entry", "")).replace("/dev/", "")
                    if dev:
                        images[dev] = im.get("image-path", "")
        return images

    def whole_disks(info: Dict[str, Any], depth: int) -> Optional[FrozenSet[str]]:
        stores = info.get("APFSPhysicalStores") or []
        if stores:
            wholes = set()
            for s in stores:
                store = run(["diskutil", "info", "-plist", s.get("APFSPhysicalStore", "")])
                if not store or not store.get("ParentWholeDisk"):
                    return None
                wholes.add(store["ParentWholeDisk"])
        elif info.get("ParentWholeDisk"):
            wholes = {info["ParentWholeDisk"]}
        else:
            return None
        resolved = set()
        for whole in wholes:
            disk = run(["diskutil", "info", "-plist", whole]) or {}
            if disk.get("BusProtocol") == "Disk Image":
                image_path = image_map().get(whole)
                if not image_path or depth >= MAX_DEPTH:
                    return None
                host = run(["diskutil", "info", "-plist", str(mount_point(Path(image_path)))])
                inner = whole_disks(host, depth + 1) if host else None
                if not inner:
                    return None
                resolved |= inner
            else:
                resolved.add(whole)
        return frozenset(resolved)

    disks = whole_disks(info, 0)
    if not disks:
        return DiskIdentity(volume, reason="its physical disk could not be determined")
    return DiskIdentity(volume, disks, _describe(disks, run))


def _describe(disks: FrozenSet[str], run: Runner) -> str:
    parts = []
    for d in sorted(disks):
        info = run(["diskutil", "info", "-plist", d]) or {}
        bits = [d]
        if info.get("Internal"):
            bits.append("internal")
        elif info.get("BusProtocol"):
            bits.append(str(info["BusProtocol"]))
        name = (info.get("MediaName") or "").strip()
        if name:
            bits.append(name)
        parts.append(" · ".join(bits))
    return " + ".join(parts)
