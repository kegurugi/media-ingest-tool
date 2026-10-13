"""ASC MHL v2 manifest for each verified card folder.

Written into <CARD>/ascmhl/ on every destination, the standard place where
ShotPut Pro, Silverstack, Hedge and the ASC's own `ascmhl` tool look for it:

  ascmhl/0001_<CARD>_<YYYY-MM-DD>_<HHMMSS>Z.mhl   the hash list (XML)
  ascmhl/ascmhl_chain.xml                        the chain, sealing each list with its C4 ID

It's built from the hashes the offload already computed and verified, so the
media isn't read again. Directory and root hashes follow the ASC algorithm:
a folder's content hash is xxHash64 over its children's hashes (sorted); its
structure hash also folds in each child's name.

Only cards that PASS get a manifest. Files the tool itself adds to a card
folder (job report copy, incomplete marker) and macOS clutter are listed as
ignore patterns, so `ascmhl verify` doesn't flag them.
"""
from __future__ import annotations

import fnmatch
import hashlib
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

import xxhash

from . import __version__
from .errors import DestinationExistsError
from .fileutil import atomic_write_bytes
from .incomplete import MARKER_NAME
from .sources import MACOS_CLUTTER

MHL_DIRNAME = "ascmhl"
CHAIN_NAME = "ascmhl_chain.xml"
NS = "urn:ASC:MHL:v2.0"
CHAIN_NS = "urn:ASC:MHL:DIRECTORY:v2.0"
TOOL_NAME = "Media Ingest Tool"

_C4_CHARSET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def c4_id(data: bytes) -> str:
    """C4 ID (SHA-512, base58, 90 characters) as used in ascmhl_chain.xml."""
    value = int.from_bytes(hashlib.sha512(data).digest(), "big")
    out = ""
    while value:
        value, mod = divmod(value, 58)
        out = _C4_CHARSET[mod] + out
    return "c4" + out.rjust(88, "1")


def ignore_patterns(card: str) -> List[str]:
    return [MHL_DIRNAME, f"{MHL_DIRNAME}/", ".DS_Store", "._*", *sorted(MACOS_CLUTTER - {".DS_Store"}),
            f"{card}_report.html", MARKER_NAME]


def _ignored(rel_path: str, patterns: Iterable[str]) -> bool:
    parts = PurePosixPath(rel_path).parts
    for pattern in patterns:
        bare = pattern.rstrip("/")
        if any(fnmatch.fnmatchcase(part, bare) for part in parts):
            return True
    return False


def _xxh64_of_list(hashes: List[str]) -> str:
    h = xxhash.xxh64()
    for value in sorted(hashes):
        h.update(bytes.fromhex(value))
    return h.hexdigest()


def directory_hashes(files: Dict[str, str], dirs: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    """(content hash, structure hash) for every folder and the root ("").

    files: rel_path -> xxh64 of every file in the manifest.
    """
    all_dirs = {""} | set(dirs)
    for path in files:
        parent = PurePosixPath(path).parent
        while str(parent) not in ("", "."):
            all_dirs.add(str(parent))
            parent = parent.parent
    children: Dict[str, List[Tuple[str, str, bool]]] = {d: [] for d in all_dirs}  # (name, rel, is_dir)
    for path in files:
        parent = str(PurePosixPath(path).parent)
        children["" if parent == "." else parent].append((PurePosixPath(path).name, path, False))
    for d in all_dirs - {""}:
        parent = str(PurePosixPath(d).parent)
        children["" if parent == "." else parent].append((PurePosixPath(d).name, d, True))

    result: Dict[str, Tuple[str, str]] = {}
    for d in sorted(all_dirs, key=lambda p: (-len(PurePosixPath(p).parts), p)):  # deepest first
        content, structure = [], []
        for name, rel, is_dir in children[d]:
            if is_dir:
                c, s = result[rel]
                content.append(c)
                structure.append(xxhash.xxh64(name.encode("utf-8") + bytes.fromhex(s)).hexdigest())
            else:
                content.append(files[rel])
                structure.append(xxhash.xxh64(name.encode("utf-8") + bytes.fromhex(files[rel])).hexdigest())
        result[d] = (_xxh64_of_list(content), _xxh64_of_list(structure))
    return result


def _local_iso(ts: float, micro: bool = False) -> str:
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.isoformat(timespec="microseconds" if micro else "seconds")


def write_mhl(card_dir: Path, card: str, files: Dict[str, str], dirs: Iterable[str],
              operator: str = "", now: Optional[datetime] = None) -> Path:
    """Write the first ASC MHL generation for a verified card folder. Returns the .mhl path."""
    card_dir = Path(card_dir)
    now = (now or datetime.now()).astimezone()
    patterns = ignore_patterns(card)
    files = {p: h for p, h in files.items() if not _ignored(p, patterns)}
    dirs = [d for d in dirs if not _ignored(d, patterns)]
    tree = directory_hashes(files, dirs)
    stamp = now.isoformat(timespec="microseconds")

    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}hashlist", {"version": "2.0"})
    creator = ET.SubElement(root, f"{{{NS}}}creatorinfo")
    ET.SubElement(creator, f"{{{NS}}}creationdate").text = now.isoformat(timespec="seconds")
    ET.SubElement(creator, f"{{{NS}}}hostname").text = socket.gethostname()
    ET.SubElement(creator, f"{{{NS}}}tool", {"version": __version__}).text = TOOL_NAME
    if operator:
        author = ET.SubElement(creator, f"{{{NS}}}author", {"role": "DIT"})
        author.text = operator
    process = ET.SubElement(root, f"{{{NS}}}processinfo")
    ET.SubElement(process, f"{{{NS}}}process").text = "transfer"
    roothash = ET.SubElement(process, f"{{{NS}}}roothash")
    for tag, value in (("content", tree[""][0]), ("structure", tree[""][1])):
        ET.SubElement(ET.SubElement(roothash, f"{{{NS}}}{tag}"), f"{{{NS}}}xxh64", {"hashdate": stamp}).text = value
    ignore = ET.SubElement(process, f"{{{NS}}}ignore")
    for pattern in patterns:
        ET.SubElement(ignore, f"{{{NS}}}pattern").text = pattern

    hashes = ET.SubElement(root, f"{{{NS}}}hashes")
    for path in sorted(set(files) | (set(tree) - {""}), key=_post_order_key):
        full = card_dir / path
        if path in files:
            entry = ET.SubElement(hashes, f"{{{NS}}}hash")
            ET.SubElement(entry, f"{{{NS}}}path", {"size": str(full.stat().st_size),
                                                     "lastmodificationdate": _local_iso(full.stat().st_mtime)}).text = path
            ET.SubElement(entry, f"{{{NS}}}xxh64", {"action": "original", "hashdate": stamp}).text = files[path]
        else:
            entry = ET.SubElement(hashes, f"{{{NS}}}directoryhash")
            ET.SubElement(entry, f"{{{NS}}}path", {"lastmodificationdate": _local_iso(full.stat().st_mtime)}).text = path
            for tag, value in zip(("content", "structure"), tree[path]):
                ET.SubElement(ET.SubElement(entry, f"{{{NS}}}{tag}"), f"{{{NS}}}xxh64", {"hashdate": stamp}).text = value

    folder = card_dir / MHL_DIRNAME
    if folder.exists():
        raise DestinationExistsError(f"{folder} already exists")
    folder.mkdir()
    utc = now.astimezone(timezone.utc)
    name = f"0001_{card_dir.name}_{utc:%Y-%m-%d_%H%M%S}Z.mhl"
    data = _xml_bytes(root)
    atomic_write_bytes(folder / name, data)

    ET.register_namespace("", CHAIN_NS)
    chain = ET.Element(f"{{{CHAIN_NS}}}ascmhldirectory")
    entry = ET.SubElement(chain, f"{{{CHAIN_NS}}}hashlist", {"sequencenr": "1"})
    ET.SubElement(entry, f"{{{CHAIN_NS}}}path").text = name
    ET.SubElement(entry, f"{{{CHAIN_NS}}}c4").text = c4_id(data)
    atomic_write_bytes(folder / CHAIN_NAME, _xml_bytes(chain))
    return folder / name


def _post_order_key(path: str):
    """Files and folders in the order the ASC reference tool writes them: children before their folder."""
    return tuple((0, part) for part in PurePosixPath(path).parts) + ((1, ""),)


def _xml_bytes(root: ET.Element) -> bytes:
    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")
