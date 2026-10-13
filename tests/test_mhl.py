"""ASC MHL manifests, checked against the ASC's reference tool (ascmhl)."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE_CARD, SMALL_CHUNK
from media_ingest import offload as offload_mod
from media_ingest.diskio import hash_file
from media_ingest.errors import DestinationExistsError
from media_ingest.mhl import c4_id, write_mhl
from media_ingest.naming import CardName
from media_ingest.offload import JobInfo, run_offload
from media_ingest.records import read_card_record
from media_ingest.spotcheck import check_card

ASCMHL = Path(sys.executable).parent / "ascmhl"
needs_ascmhl = pytest.mark.skipif(not ASCMHL.exists(), reason="needs the ascmhl reference tool (pip install ascmhl)")
CLIP1 = "PRIVATE/M4ROOT/CLIP/C0001.MP4"


def offload(source, drives, **kw):
    return run_offload(source, drives, CardName.parse("ABC261012A01"), "VIDEO", allow_same_volume=True,
                       chunk_size=SMALL_CHUNK, job=JobInfo(show_code="ABC", camera_id="A", operator="Dwayne Fowler"), **kw)


@pytest.fixture
def card_dirs(sample_card, drives):
    r = offload(sample_card, drives)
    assert r.passed and len(r.mhl_paths) == 2
    return r.card_dirs


def ascmhl(*args):
    return subprocess.run([str(ASCMHL), *map(str, args)], capture_output=True, text=True)


def test_written_in_every_card_folder(card_dirs, drives):
    for card_dir in card_dirs:
        folder = card_dir / "ascmhl"
        (mhl,) = folder.glob("0001_ABC261012A01_*Z.mhl")
        assert (folder / "ascmhl_chain.xml").exists()
        text = mhl.read_text()
        assert "<process>transfer</process>" in text and '<author role="DIT">Dwayne Fowler</author>' in text
        assert '<tool version="0.1.0">Media Ingest Tool</tool>' in text
        assert text.count("<hash>") == 5 and text.count('action="original"') == 5
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    assert record["destinations"][0]["ascmhl"].startswith("ascmhl/0001_ABC261012A01_")
    html = (drives[0] / "_REPORTS/10122026/ABC261012A01_report.html").read_text()
    assert "ASC MHL" in html and "in the card folder on 2 drives" in html


def test_c4_matches_the_reference():
    try:
        from ascmhl.hasher import C4
    except ImportError:
        pytest.skip("ascmhl not installed")
    for data in (b"", b"hello", bytes(range(256)) * 100):
        assert c4_id(data) == C4.hash_data(data)
    assert len(c4_id(b"x")) == 90


@needs_ascmhl
def test_reference_tool_verifies_a_clean_copy(card_dirs, tmp_path):
    copy = tmp_path / "copy" / card_dirs[1].name
    shutil.copytree(card_dirs[1], copy, copy_function=shutil.copy2)
    r = ascmhl("create", copy, "-h", "xxh64")
    assert r.returncode == 0, r.stdout + r.stderr
    (second,) = (copy / "ascmhl").glob("0002_*.mhl")
    assert second.read_text().count('action="verified"') == 5


@needs_ascmhl
def test_reference_tool_catches_one_changed_byte(card_dirs, tmp_path):
    copy = tmp_path / "copy" / card_dirs[0].name
    shutil.copytree(card_dirs[0], copy, copy_function=shutil.copy2)
    data = bytearray((copy / CLIP1).read_bytes())
    data[500] ^= 1
    (copy / CLIP1).write_bytes(bytes(data))
    r = ascmhl("create", copy, "-h", "xxh64")
    assert r.returncode != 0 and "hash mismatch" in r.stdout + r.stderr and CLIP1 in r.stdout + r.stderr


@needs_ascmhl
def test_added_files_are_ignored_by_the_reference_tool(card_dirs):
    folder = card_dirs[0]
    assert (folder / "ABC261012A01_report.html").exists()
    (folder / ".DS_Store").write_bytes(b"finder")
    (folder / "PRIVATE/._C0001.MP4").write_bytes(b"appledouble")
    r = ascmhl("diff", folder)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_ascmhl
def test_directory_and_root_hashes_match_the_reference(sample_card, tmp_path):
    ours = tmp_path / "ours" / "ABC261012A01"
    ref = tmp_path / "ref" / "ABC261012A01"
    shutil.copytree(sample_card, ours, ignore=shutil.ignore_patterns(".*", "._*"))
    shutil.copytree(ours, ref, copy_function=shutil.copy2)
    files = {str(p.relative_to(ours)): hash_file(p)[0] for p in ours.rglob("*") if p.is_file()}
    dirs = [str(p.relative_to(ours)) for p in ours.rglob("*") if p.is_dir()]
    write_mhl(ours, "ABC261012A01", files, dirs)
    assert ascmhl("create", ref, "-h", "xxh64").returncode == 0

    def hashes(folder):
        text = next((folder / "ascmhl").glob("*.mhl")).read_text()
        root = re.findall(r"<(content|structure)>\s*<xxh64[^>]*>(\w+)<", text.split("<hashes>")[0])
        dirs = re.findall(r"<directoryhash>\s*<path[^>]*>([^<]+)</path>\s*<content>\s*<xxh64[^>]*>(\w+)<\s*/xxh64>"
                          r"\s*</content>\s*<structure>\s*<xxh64[^>]*>(\w+)<", text)
        return root, sorted(dirs)

    assert hashes(ours) == hashes(ref)


def test_failed_card_gets_no_manifest(sample_card, drives, monkeypatch):
    real = offload_mod.copy_card

    def corrupt(listing, card_dirs, **kw):
        r = real(listing, card_dirs, **kw)
        (card_dirs[1] / CLIP1).write_bytes(b"x")
        return r

    monkeypatch.setattr(offload_mod, "copy_card", corrupt)
    r = offload(sample_card, drives)
    assert not r.passed and r.mhl_paths == []
    assert not any((d / "ascmhl").exists() for d in r.card_dirs)


def test_mit_verify_expects_the_manifest(card_dirs):
    check = check_card(card_dirs[0])
    assert check.passed and check.warnings == []


def test_never_overwrites_an_existing_history(card_dirs):
    with pytest.raises(DestinationExistsError):
        write_mhl(card_dirs[0], "ABC261012A01", {CLIP1: "0" * 16}, [])


def test_copy_all_clutter_is_not_listed(sample_card, drives):
    r = offload(sample_card, drives, copy_all=True)
    text = r.mhl_paths[0].read_text()
    assert ".DS_Store</path>" not in text and "._C0001.MP4" not in text
    assert text.count("<hash>") == 5


# --- `mit mhl`: manifests for cards offloaded before manifests existed ---------------

from media_ingest.cli import main


@pytest.fixture
def old_cards(card_dirs, tmp_path):
    """Cards as if offloaded before manifests existed: move the manifests out of the way."""
    for i, d in enumerate(card_dirs):
        (d / "ascmhl").rename(tmp_path / f"old_mhl_{i}")
    return card_dirs


def test_mit_mhl_writes_a_manifest_from_the_log(old_cards, drives, capsys):
    assert main(["mhl", str(old_cards[0])]) == 0
    assert "WROTE    ABC261012A01" in capsys.readouterr().out
    (mhl,) = (old_cards[0] / "ascmhl").glob("0001_*.mhl")
    text = mhl.read_text()
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    assert record["offload_finished"][:16] in text  # hashes dated when they were verified
    assert '<author role="DIT">Dwayne Fowler</author>' in text and text.count("<hash>") == 5
    assert not (old_cards[1] / "ascmhl").exists()  # only the folder asked for


@needs_ascmhl
def test_mit_mhl_manifest_verifies_with_the_reference_tool(old_cards, tmp_path):
    assert main(["mhl", "--drive", str(old_cards[1].parents[2])]) == 0
    copy = tmp_path / "copy" / old_cards[1].name
    shutil.copytree(old_cards[1], copy, copy_function=shutil.copy2)
    r = ascmhl("create", copy, "-h", "xxh64")
    assert r.returncode == 0, r.stdout + r.stderr


def test_mit_mhl_skips_cards_that_already_have_one(card_dirs, capsys):
    before = sorted((card_dirs[0] / "ascmhl").iterdir())
    assert main(["mhl", str(card_dirs[0])]) == 0
    assert "skipped  ABC261012A01  (already has an ASC MHL manifest)" in capsys.readouterr().out
    assert sorted((card_dirs[0] / "ascmhl").iterdir()) == before


def test_mit_mhl_refuses_a_changed_card(old_cards, capsys):
    with open(old_cards[0] / CLIP1, "r+b") as f:
        f.truncate(100)
    assert main(["mhl", str(old_cards[0])]) == 1
    assert f"{CLIP1} is missing or a different size" in capsys.readouterr().out
    assert not (old_cards[0] / "ascmhl").exists()


def test_mit_mhl_verify_option_rehashes_first(old_cards, capsys):
    data = bytearray((old_cards[0] / CLIP1).read_bytes())
    data[10] ^= 1  # same size, different content: only a re-hash notices
    (old_cards[0] / CLIP1).write_bytes(bytes(data))
    assert main(["mhl", str(old_cards[0])]) == 0  # quick mode trusts the logged hashes
    (old_cards[0] / "ascmhl").rename(old_cards[0].parent / "again")
    assert main(["mhl", "--verify", str(old_cards[0])]) == 1
    out = capsys.readouterr().out
    assert "re-verification FAILED" in out and f"MISMATCH {CLIP1}" in out
    assert not (old_cards[0] / "ascmhl").exists()


def test_mit_mhl_refuses_failed_cards(sample_card, drives, monkeypatch, capsys):
    real = offload_mod.copy_card
    monkeypatch.setattr(offload_mod, "copy_card",
                        lambda l, d, **kw: (real(l, d, **kw), (d[1] / CLIP1).write_bytes(b"x"))[0])
    r = offload(sample_card, drives)
    from media_ingest.incomplete import MARKER_NAME
    (r.card_dirs[0] / MARKER_NAME).unlink()  # even without the marker, the log says FAIL
    assert main(["mhl", str(r.card_dirs[0])]) == 1
    assert "its offload was FAIL" in capsys.readouterr().out
