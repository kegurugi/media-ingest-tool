"""Separate-drives check by physical disk."""
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import write_card
from media_ingest.errors import IngestError
from media_ingest.offload import check_physical_disks
from media_ingest.physdisk import DiskIdentity, identify

# diskutil/hdiutil answers modelled on real output from this project's test Mac.
INTERNAL = {
    "/": {"DeviceIdentifier": "disk3s1s1", "ParentWholeDisk": "disk3", "BusProtocol": "Apple Fabric",
          "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}]},
    "disk0s2": {"DeviceIdentifier": "disk0s2", "ParentWholeDisk": "disk0"},
    "disk0": {"DeviceIdentifier": "disk0", "ParentWholeDisk": "disk0", "Internal": True, "MediaName": "APPLE SSD"},
}


def runner(table):
    def run(args):
        key = "hdiutil" if args[0] == "hdiutil" else args[-1]
        return table.get(key)
    return run


def vol(path, **info):
    return {path: {"DeviceIdentifier": "x", **info}}


@pytest.fixture
def mounts(monkeypatch):
    """Pretend every path given is its own mount point."""
    monkeypatch.setattr("media_ingest.physdisk.mount_point", lambda p: Path(p))


def test_exfat_partition(mounts):
    run = runner({**vol("/Volumes/WD_BLACK", ParentWholeDisk="disk6"),
                  "disk6": {"BusProtocol": "USB", "MediaName": "Game Drive"}})
    i = identify(Path("/Volumes/WD_BLACK"), run)
    assert i.disks == {"disk6"} and i.description == "disk6 · USB · Game Drive"


def test_apfs_follows_container_to_physical_disk(mounts):
    i = identify(Path("/"), runner(INTERNAL))
    assert i.disks == {"disk0"} and i.description == "disk0 · internal · APPLE SSD"


def test_two_apfs_volumes_in_one_container_share_a_disk(mounts):
    table = {**vol("/Volumes/SSD_A", ParentWholeDisk="disk9", APFSPhysicalStores=[{"APFSPhysicalStore": "disk8s2"}]),
             **vol("/Volumes/SSD_B", ParentWholeDisk="disk9", APFSPhysicalStores=[{"APFSPhysicalStore": "disk8s2"}]),
             "disk8s2": {"ParentWholeDisk": "disk8"}, "disk8": {"BusProtocol": "USB"}}
    a, b = identify(Path("/Volumes/SSD_A"), runner(table)), identify(Path("/Volumes/SSD_B"), runner(table))
    assert a.disks == b.disks == {"disk8"}


def test_fusion_style_container_spans_two_disks(mounts):
    table = {**vol("/Volumes/F", APFSPhysicalStores=[{"APFSPhysicalStore": "disk0s2"}, {"APFSPhysicalStore": "disk1s2"}]),
             "disk0s2": {"ParentWholeDisk": "disk0"}, "disk1s2": {"ParentWholeDisk": "disk1"}, "disk0": {}, "disk1": {}}
    assert identify(Path("/Volumes/F"), runner(table)).disks == {"disk0", "disk1"}


def test_disk_image_resolves_to_the_disk_holding_the_file(mounts, tmp_path):
    image = tmp_path / "test.dmg"
    image.write_bytes(b"")
    table = {**INTERNAL, **vol("/Volumes/IMG", ParentWholeDisk="disk9", APFSPhysicalStores=[{"APFSPhysicalStore": "disk8s1"}]),
             "disk8s1": {"ParentWholeDisk": "disk8"}, "disk8": {"BusProtocol": "Disk Image"},
             "hdiutil": {"images": [{"image-path": str(image), "system-entities": [{"dev-entry": "/dev/disk8"}]}]},
             str(image): None}
    run = runner(table)
    # the image file sits on "/" (mount_point is patched to return the path itself, so map it)
    table[str(image)] = INTERNAL["/"]
    assert identify(Path("/Volumes/IMG"), run).disks == {"disk0"}


@pytest.mark.parametrize("table", [{}, vol("/Volumes/NAS"), vol("/Volumes/NAS", APFSPhysicalStores=[{"APFSPhysicalStore": "gone"}])])
def test_unknown(mounts, table):
    i = identify(Path("/Volumes/NAS"), runner(table))
    assert not i.known and i.reason


def ids(**disks):
    return lambda p: DiskIdentity(Path(p), frozenset(disks.get(Path(p).name, ())), f"{','.join(disks.get(Path(p).name, ()))} · USB",
                                  "" if disks.get(Path(p).name) else "network share?")


def test_separate_disks_pass(tmp_path):
    got = check_physical_disks(tmp_path / "card", [tmp_path / "DRV01A", tmp_path / "DRV01B"],
                               identify=ids(card=["disk7"], DRV01A=["disk5"], DRV01B=["disk6"]))
    assert [i.disks for i in got.values()] == [{"disk5"}, {"disk6"}]


def test_destinations_on_one_disk_refused(tmp_path):
    with pytest.raises(IngestError, match="DRV01A and DRV01B are on the same physical disk \\(disk6"):
        check_physical_disks(tmp_path / "card", [tmp_path / "DRV01A", tmp_path / "DRV01B"],
                             identify=ids(card=["disk7"], DRV01A=["disk6"], DRV01B=["disk6"]))


def test_source_on_a_destination_disk_refused(tmp_path):
    with pytest.raises(IngestError, match="source .* same physical disk as destination DRV01B"):
        check_physical_disks(tmp_path / "card", [tmp_path / "DRV01A", tmp_path / "DRV01B"],
                             identify=ids(card=["disk6"], DRV01A=["disk5"], DRV01B=["disk6"]))


def test_unknown_destination_needs_explicit_permission(tmp_path):
    drives = [tmp_path / "DRV01A", tmp_path / "NAS"]
    with pytest.raises(IngestError, match="--allow-unverified-drives"):
        check_physical_disks(tmp_path / "card", drives, identify=ids(card=["disk7"], DRV01A=["disk5"]))
    got = check_physical_disks(tmp_path / "card", drives, allow_unverified_drives=True,
                               identify=ids(card=["disk7"], DRV01A=["disk5"]))
    assert not got[tmp_path / "NAS"].known


def test_record_and_report_show_the_check(sample_card, drives, monkeypatch):
    from media_ingest import offload as offload_mod
    from media_ingest.naming import CardName
    from media_ingest.offload import run_offload
    from media_ingest.records import read_card_record
    r = run_offload(sample_card, drives, CardName.parse("ABC261012A01"), "VIDEO", allow_same_volume=True)
    record = read_card_record(drives[0] / "_REPORTS/10122026/ABC261012A01_card.json")
    assert record["separate_drives"] == "skipped (--allow-same-volume, testing only)"
    html = (drives[0] / "_REPORTS/10122026/ABC261012A01_report.html").read_text()
    assert "Separate drives" in html and "testing only" in html


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_real_apfs_container_with_two_volumes_is_refused(tmp_path):
    """The gap itself: two APFS volumes in one container (here inside a sparse disk image)."""
    import uuid
    image, a = tmp_path / "two.sparseimage", tmp_path / "MIT_A"
    b_name = f"MITTEST_{uuid.uuid4().hex[:8]}"
    a.mkdir()
    try:
        subprocess.run(["hdiutil", "create", "-quiet", "-size", "1g", "-type", "SPARSE", "-fs", "APFS",
                        "-volname", "MIT_A", "-o", str(tmp_path / "two")], check=True, capture_output=True)
        out = subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(a), str(image)],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"hdiutil unavailable: {e}")
    try:
        volume_dev = next(line.split()[0].replace("/dev/", "") for line in out.splitlines()
                          if line.strip().endswith(str(a)))
        container = volume_dev.rsplit("s", 1)[0]  # disk9s1 -> disk9
        # Safety: only ever add a volume to the container of the image this test created.
        info = subprocess.run(["diskutil", "info", container], capture_output=True, text=True).stdout
        assert "Disk Image" in info, f"{container} is not our disk image; refusing to touch it"
        subprocess.run(["diskutil", "apfs", "addVolume", container, "APFS", b_name],
                       check=True, capture_output=True)  # mounts at /Volumes/<name>
        b = Path("/Volumes") / b_name
        assert b.is_dir()
        card = write_card(tmp_path / "card", {"CLIP/C0001.MP4": b"x"})
        ia, ib = identify(a), identify(b)
        assert ia.known and ia.disks == ib.disks
        with pytest.raises(IngestError, match=f"MIT_A and {b_name} are on the same physical disk"):
            check_physical_disks(card, [a, b])
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(a)], capture_output=True)  # removes both volumes
    assert not (Path("/Volumes") / b_name).exists()
