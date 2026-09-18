# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The local volumes: what a machine's disks have room for, and a new one.

Two things carry the value here. The reading, because it decides which disk the
page offers, and offering the disk Ceph owns or one whose table would have to
be rewritten is offering to erase it. And the entry written into the
inventory, because that entry is the whole of what reaches the machine: the
role partitions exactly what it says.

The answer the machine gives is the one a machine installed from the ISO gives:
a 1 TB system disk with `vg1` on a 50 GiB partition and the rest unallocated,
a disk handed to Ceph, an empty disk, and one carrying an MBR table.
"""

from __future__ import annotations

import json

import yaml
from fastapi.testclient import TestClient

from app.inventory.resolve import resolve
from app.services.local_storage import (
    LocalVolume,
    check,
    entry_of,
    parse_reading,
    reading_command,
)
from tests.conftest import sign_in

CLUSTER = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      cluster_ip_addr: 192.168.55.1
      cluster_next_ip_addr: 192.168.55.2
      cluster_previous_ip_addr: 192.168.55.3
      br_rstp_priority: 12288
      ceph_osd_disks:
        - /dev/disk/by-path/pci-0000:00:17.0-ata-2
    elabo1:
      ansible_host: 192.168.200.126
      cluster_ip_addr: 192.168.55.2
      cluster_next_ip_addr: 192.168.55.3
      cluster_previous_ip_addr: 192.168.55.1
      br_rstp_priority: 16384
    elabo2:
      ansible_host: 192.168.200.127
      cluster_ip_addr: 192.168.55.3
      cluster_next_ip_addr: 192.168.55.1
      cluster_previous_ip_addr: 192.168.55.2
      br_rstp_priority: 16384
  children:
    cluster_machines:
      hosts:
        seapath-machine:
        elabo1:
        elabo2:
      vars:
        network_interface: eno1
        team0_0: eno2
        team0_1: eno3
        admin_user: admin
    hypervisors:
      children:
        cluster_machines:
"""

GIB = 1 << 30
TB = 1_000_204_886_016

_LSBLK = {
    "blockdevices": [
        {
            "name": "sda",
            "path": "/dev/sda",
            "type": "disk",
            "size": TB,
            "fstype": None,
            "mountpoint": None,
            "model": "SAMSUNG MZ7L31T9",
            "children": [
                {
                    "name": "sda1",
                    "path": "/dev/sda1",
                    "type": "part",
                    "size": 536870912,
                    "fstype": "vfat",
                    "mountpoint": "/boot/efi",
                },
                {
                    "name": "sda2",
                    "path": "/dev/sda2",
                    "type": "part",
                    "size": 50 * GIB,
                    "fstype": "LVM2_member",
                    "mountpoint": None,
                    "children": [
                        {
                            "name": "vg1-root",
                            "path": "/dev/mapper/vg1-root",
                            "type": "lvm",
                            "size": 15 * GIB,
                            "fstype": "ext4",
                            "mountpoint": "/",
                        },
                        {
                            "name": "vg1-varlog",
                            "path": "/dev/mapper/vg1-varlog",
                            "type": "lvm",
                            "size": 5 * GIB,
                            "fstype": "ext4",
                            "mountpoint": "/var/log",
                        },
                    ],
                },
            ],
        },
        {
            "name": "sdb",
            "path": "/dev/sdb",
            "type": "disk",
            "size": 500 * GIB,
            "fstype": "LVM2_member",
            "mountpoint": None,
            "model": "OSD",
            "children": [
                {
                    "name": "ceph--osd",
                    "path": "/dev/mapper/ceph--osd",
                    "type": "lvm",
                    "size": 500 * GIB,
                    "fstype": "ceph_bluestore",
                    "mountpoint": None,
                }
            ],
        },
        {
            "name": "sdc",
            "path": "/dev/sdc",
            "type": "disk",
            "size": 200 * GIB,
            "fstype": None,
            "mountpoint": None,
            "model": "EMPTY",
        },
        {
            "name": "nvme0n1",
            "path": "/dev/nvme0n1",
            "type": "disk",
            "size": 100 * GIB,
            "fstype": None,
            "mountpoint": None,
            "model": "OLD",
            "children": [
                {
                    "name": "nvme0n1p1",
                    "path": "/dev/nvme0n1p1",
                    "type": "part",
                    "size": 10 * GIB,
                    "fstype": "ext4",
                    "mountpoint": None,
                }
            ],
        },
        # An `nbd` slot nothing is attached to, which `lsblk` lists as a disk.
        {
            "name": "nbd0",
            "path": "/dev/nbd0",
            "type": "disk",
            "size": 0,
            "fstype": None,
            "mountpoint": None,
        },
    ]
}

# `parted -m print free`, as it prints it. The system disk has 50 GiB used and
# the rest free after its last partition, which is what the role takes.
_SYSTEM_END = 1048576 + 536870912 + 50 * GIB - 1
_PARTED = f"""@disk /dev/sda
BYT;
/dev/sda:{TB}B:scsi:512:512:gpt:ATA SAMSUNG MZ7L31T9:;
1:17408B:1048575B:1031168B:free;
1:1048576B:537919487B:536870912B:fat32::boot, esp;
2:537919488B:{_SYSTEM_END}B:{50 * GIB}B:::lvm;
1:{_SYSTEM_END + 1}B:{TB - 16896}B:{TB - 16896 - _SYSTEM_END}B:free;
@disk /dev/sdb
BYT;
/dev/sdb:{500 * GIB}B:scsi:512:512:unknown:ATA OSD:;
@disk /dev/sdc
BYT;
/dev/sdc:{200 * GIB}B:scsi:512:512:unknown:ATA EMPTY:;
@disk /dev/nvme0n1
BYT;
/dev/nvme0n1:{100 * GIB}B:nvme:512:512:msdos:OLD:;
1:1024B:1048575B:1047552B:free;
1:1048576B:{10 * GIB + 1048575}B:{10 * GIB}B:ext4::;
1:{10 * GIB + 1048576}B:{100 * GIB - 1}B:{90 * GIB - 1048576}B:free;
"""

_VGS = {"report": [{"vg": [{"vg_name": "vg1", "vg_size": str(50 * GIB),
                            "vg_free": str(29 * GIB)}]}]}  # fmt: skip

_LINKS = """/dev/disk/by-path/pci-0000:00:17.0-ata-1 /dev/sda
/dev/disk/by-path/pci-0000:00:17.0-ata-2 /dev/sdb
/dev/disk/by-path/pci-0000:00:17.0-ata-3 /dev/sdc
"""


def _answer(mounted: dict | None = None) -> str:
    tree = json.loads(json.dumps(_LSBLK))
    if mounted:
        tree["blockdevices"][0]["children"].append(mounted)
    return (
        "@@lsblk\n"
        + json.dumps(tree, indent=2)
        + "\n@@parted\n"
        + _PARTED
        + "@@vgs\n"
        + json.dumps(_VGS)
        + "\n@@links\n"
        + _LINKS
    )


def _import(client: TestClient, document: str = CLUSTER) -> None:
    response = client.post("/api/v1/inventory/import", json={"document": document})
    assert response.status_code == 200, response.text


def _read(client: TestClient, host: str | None = None) -> dict:
    path = "/api/v1/storage/local" + (f"?host={host}" if host else "")
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def _disks(reading: dict) -> dict[str, dict]:
    return {disk["device"]: disk for disk in reading["disks"]}


# Reading


def test_a_machine_is_asked_about_its_disks_as_root_over_one_connection(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in)
    remote_runner.answers = {"@@lsblk": _answer()}

    reading = _read(signed_in, "elabo1")

    asked = remote_runner.requests[0]
    assert asked.address == "192.168.200.126"
    assert asked.user == "ansible"
    # `parted` and `vgs` need root.
    assert asked.command == reading_command()
    assert asked.command.startswith("sudo -n /bin/sh -c ")
    assert reading["host"] == "elabo1"
    assert reading["read_at"]


def test_the_machine_serving_the_page_is_the_one_read_by_default(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in)
    remote_runner.answers = {"@@lsblk": _answer()}

    reading = _read(signed_in)

    assert reading["host"] == "seapath-machine"
    assert reading["hosts"] == ["elabo1", "elabo2", "seapath-machine"]


def test_the_system_disk_offers_what_the_iso_left_unallocated() -> None:
    """The case this exists for: 50 GiB used, the rest of 1 TB free."""
    disks, groups, mounts = parse_reading(_answer(), [])

    system = next(disk for disk in disks if disk.device == "/dev/sda")
    assert system.path == "/dev/disk/by-path/pci-0000:00:17.0-ata-1"
    assert system.table == "gpt"
    assert system.system is True
    assert system.usable is True
    assert system.free_bytes == TB - 16896 - _SYSTEM_END
    assert [(group.name, group.free_bytes) for group in groups] == [("vg1", 29 * GIB)]
    assert {"/", "/var/log", "/boot/efi"} <= mounts


def test_a_disk_given_to_ceph_is_never_offered(
    signed_in: TestClient, remote_runner
) -> None:
    """Named by path in `ceph_osd_disks`, recognised through the link."""
    _import(signed_in)
    remote_runner.answers = {"@@lsblk": _answer()}

    osd = _disks(_read(signed_in, "seapath-machine"))["/dev/sdb"]

    assert osd["ceph"] is True
    assert osd["usable"] is False
    assert "Ceph" in osd["reason"]


def test_an_empty_disk_is_offered_whole() -> None:
    disks, _, _ = parse_reading(_answer(), [])

    empty = next(disk for disk in disks if disk.device == "/dev/sdc")
    assert empty.table == "none"
    assert empty.usable is True
    assert empty.free_bytes == 200 * GIB


def test_a_disk_whose_table_the_role_would_rewrite_is_refused_with_why() -> None:
    disks, _, _ = parse_reading(_answer(), [])

    old = next(disk for disk in disks if disk.device == "/dev/nvme0n1")
    assert old.table == "msdos"
    assert old.usable is False
    assert "MBR" in old.reason


def test_a_disk_holding_a_file_system_of_its_own_is_refused() -> None:
    """What `parted` calls a `loop` label, and what Ceph's whole-disk PV is."""
    disks, _, _ = parse_reading(_answer(), [])

    osd = next(disk for disk in disks if disk.device == "/dev/sdb")
    assert osd.table == "whole"
    assert osd.usable is False


def test_an_empty_device_slot_is_not_a_disk() -> None:
    disks, _, _ = parse_reading(_answer(), [])

    assert "/dev/nbd0" not in {disk.device for disk in disks}


def test_a_machine_that_cannot_be_asked_says_what_ssh_answered(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in)
    remote_runner.refusal = "Permission denied (publickey)."

    reading = _read(signed_in, "elabo2")

    assert reading["disks"] == []
    assert "Permission denied (publickey)." in reading["note"]


def test_a_machine_outside_the_inventory_is_refused(signed_in: TestClient) -> None:
    _import(signed_in)

    response = signed_in.get("/api/v1/storage/local?host=somewhere")

    assert response.status_code == 404


# Creating


def _create(client: TestClient, host: str, **fields) -> object:
    body = {"name": "data", "disk": "/dev/disk/by-path/pci-0000:00:17.0-ata-1"}
    body.update(mountpoint="/data")
    body.update(fields)
    return client.post(f"/api/v1/storage/local/{host}/volumes", json=body)


def _volumes(client: TestClient, host: str) -> object:
    document = client.get("/api/v1/inventory/raw").text
    return resolve(document)[host].get("configure_local_storage_volumes")


def _played(client: TestClient, run_id: str) -> dict:
    root = (
        client.app.state.settings.runs_dir
        / run_id
        / "collections/ansible_collections/seapath/ansible/playbooks"
    )
    return yaml.safe_load((root / "local_volume.yaml").read_text())[0]


def test_a_volume_is_created_by_a_run_and_nothing_is_written(
    signed_in: TestClient,
) -> None:
    """A partition is made once, and the inventory does not keep an account of it.

    An entry left in the desired state would be a second description of the
    disk, true on the day it was written and free to drift afterwards. The run
    record keeps the play, the volume in it and who asked.
    """
    _import(signed_in)
    before = signed_in.get("/api/v1/inventory/raw").text

    response = _create(
        signed_in, "seapath-machine", lvm_vg="vgdata", lvm_lv="data", size="500G"
    )

    assert response.status_code == 200, response.text
    play = _played(signed_in, response.json()["run_id"])
    # One machine, named in the play, so a by-path disk reaches no other.
    assert play["hosts"] == "seapath-machine"
    assert play["become"] is True
    assert play["roles"] == ["seapath.ansible.configure_local_storage"]
    assert play["vars"] == {
        "configure_local_storage_volumes": [
            {
                "name": "data",
                "disk": "/dev/disk/by-path/pci-0000:00:17.0-ata-1",
                "size": "500G",
                "lvm": {"vg": "vgdata", "lv": "data"},
                "mountpoint": "/data",
            }
        ]
    }
    assert signed_in.get("/api/v1/inventory/raw").text == before


def test_a_volume_the_role_would_refuse_starts_no_run(
    signed_in: TestClient,
) -> None:
    _import(signed_in)

    response = _create(signed_in, "seapath-machine", mountpoint="/var")

    assert response.status_code == 400
    assert "belongs to the system" in response.json()["error"]["message"]
    assert signed_in.get("/api/v1/runs").json() == []


def test_creating_a_volume_is_an_administrator_s_act(client: TestClient) -> None:
    sign_in(client, "admin")
    _import(client)
    sign_in(client, "operator")

    assert _create(client, "seapath-machine").status_code == 403


def test_a_volume_on_a_machine_outside_the_inventory_is_refused(
    signed_in: TestClient,
) -> None:
    _import(signed_in)

    assert _create(signed_in, "somewhere").status_code == 400


# What the inventory may still declare: entries a site wrote, or an earlier
# version of this page. Each is read against the disk, and each can be removed.


def _declared(client: TestClient, *entries: dict) -> None:
    system_disk = "/dev/disk/by-path/pci-0000:00:17.0-ata-1"
    lines = "".join(
        f"        - {{name: {entry['name']}, "
        f"disk: {entry.get('disk', system_disk)}, "
        f"mountpoint: {entry['mountpoint']}}}\n"
        for entry in entries
    )
    _import(
        client,
        CLUSTER.replace(
            "    elabo1:\n      ansible_host: 192.168.200.126\n",
            "    elabo1:\n      ansible_host: 192.168.200.126\n"
            "      configure_local_storage_volumes:\n" + lines,
        ),
    )


def _partition(number: int, label: str, mountpoint: str | None) -> dict:
    return {
        "name": f"sda{number}",
        "path": f"/dev/sda{number}",
        "type": "part",
        "size": 300 * GIB,
        "fstype": "ext4",
        "mountpoint": mountpoint,
        "partlabel": label,
    }


def _states(client: TestClient, remote_runner, *partitions: dict) -> dict:
    tree = _answer()
    document = json.loads(tree.split("@@lsblk\n", 1)[1].split("\n@@parted", 1)[0])
    document["blockdevices"][0]["children"].extend(partitions)
    remote_runner.answers = {
        "@@lsblk": "@@lsblk\n"
        + json.dumps(document)
        + "\n@@parted\n"
        + tree.split("\n@@parted\n", 1)[1]
    }
    declared = _read(client, "elabo1")["declared"]
    return {item["name"]: item for item in declared}


def test_a_volume_is_mounted_when_its_own_partition_is(
    signed_in: TestClient, remote_runner
) -> None:
    """The role finds its partition again by GPT name, and so does the page."""
    _declared(signed_in, {"name": "data", "mountpoint": "/data"})

    data = _states(signed_in, remote_runner, _partition(3, "data", "/data"))["data"]

    assert data["state"] == "mounted"
    assert data["mounted"] is True


def test_another_file_system_on_the_mount_point_blocks_the_run(
    signed_in: TestClient, remote_runner
) -> None:
    """The role refuses the entry, and a convergence fails the host there.

    Every entry declared after it is then left undone, which is how a machine
    collected `/data2` and `/data3` without either being created. The page
    says so instead of calling `/data` mounted.
    """
    _declared(
        signed_in,
        {"name": "data", "mountpoint": "/data"},
        {"name": "data2", "mountpoint": "/data2"},
    )

    states = _states(signed_in, remote_runner, _partition(4, "", "/data"))

    assert states["data"]["state"] == "blocked"
    assert states["data"]["mounted"] is False
    assert "fails the machine there" in states["data"]["detail"]
    assert states["data2"]["state"] == "pending"


def test_a_partition_created_but_not_mounted_is_said(
    signed_in: TestClient, remote_runner
) -> None:
    _declared(signed_in, {"name": "data", "mountpoint": "/data"})

    data = _states(signed_in, remote_runner, _partition(3, "data", None))["data"]

    assert data["state"] == "created"
    assert "/dev/sda3" in data["detail"]


def test_a_disk_the_machine_does_not_have_blocks_the_run(
    signed_in: TestClient, remote_runner
) -> None:
    _declared(signed_in, {"name": "data", "disk": "/dev/sdz", "mountpoint": "/data"})

    data = _states(signed_in, remote_runner)["data"]

    assert data["state"] == "blocked"


def test_room_between_two_partitions_is_reported_and_not_offered() -> None:
    """The role only adds after the last partition, and a gap is not full.

    A disk whose third partition was deleted has hundreds of gigabytes between
    its second and fourth. Calling it full was untrue; offering the gap would
    be a judgement about the disk that belongs to whoever laid it out.
    """
    parted = f"""@disk /dev/sda
BYT;
/dev/sda:{TB}B:scsi:512:4096:gpt:ATA HFS960G3H2X069N:;
1:1048576B:537919487B:536870912B:fat32::boot, esp;
2:537919488B:{54 * GIB}B:{53 * GIB}B:::lvm;
1:{54 * GIB + 1}B:{350 * GIB}B:{296 * GIB}B:free;
4:{350 * GIB + 1}B:{TB - 16896}B:{TB - 16896 - 350 * GIB}B:::lvm;
"""
    answer = _answer().split("@@parted\n", 1)
    rest = answer[1].split("@@vgs\n", 1)[1]

    disks, _, _ = parse_reading(
        answer[0] + "@@parted\n" + parted + "@@vgs\n" + rest, []
    )

    system = next(disk for disk in disks if disk.device == "/dev/sda")
    assert system.usable is False
    assert system.gap_bytes == 296 * GIB
    assert "only ever adds one after the last" in system.reason
    assert "296 GiB" in system.reason


def test_an_entry_is_forgotten_and_the_others_kept(signed_in: TestClient) -> None:
    """The inventory only: the role never removes what it created."""
    _declared(
        signed_in,
        {"name": "data", "mountpoint": "/data"},
        {"name": "data2", "mountpoint": "/data2"},
    )

    response = signed_in.delete("/api/v1/storage/local/elabo1/volumes/data")

    assert response.status_code == 200, response.text
    assert "forget the local volume data of elabo1" in response.json()["message"]
    assert [entry["name"] for entry in _volumes(signed_in, "elabo1")] == ["data2"]


def test_forgetting_an_entry_nobody_declared_is_refused(
    signed_in: TestClient,
) -> None:
    _declared(signed_in, {"name": "data", "mountpoint": "/data"})

    response = signed_in.delete("/api/v1/storage/local/elabo1/volumes/other")

    assert response.status_code == 400
    assert _volumes(signed_in, "elabo1")[0]["name"] == "data"


def test_forgetting_an_entry_is_an_administrator_s_act(client: TestClient) -> None:
    sign_in(client, "admin")
    _declared(client, {"name": "data", "mountpoint": "/data"})
    sign_in(client, "operator")

    response = client.delete("/api/v1/storage/local/elabo1/volumes/data")

    assert response.status_code == 403


# The rules, each with the case it accepts and the one it refuses. They are the
# role's own, checked here so a refusal is a sentence rather than a failed run.


def _volume(**fields) -> LocalVolume:
    base = {"name": "data", "disk": "/dev/sda", "mountpoint": "/data"}
    base.update(fields)
    return LocalVolume(**base)


def _refused(**fields) -> str:
    try:
        check(_volume(**fields))
    except Exception as error:
        return str(error)
    raise AssertionError(f"{fields} was accepted")


def test_the_name_becomes_a_gpt_partition_name() -> None:
    check(_volume(name="backup-staging_1"))
    assert "36" in _refused(name="has a space")
    assert "36" in _refused(name="x" * 37)


def test_the_mount_point_is_an_absolute_path_of_its_own() -> None:
    check(_volume(mountpoint="/srv/backups"))
    assert "absolute path" in _refused(mountpoint="data")
    assert "absolute path" in _refused(mountpoint="/srv/../etc")
    assert "absolute path" in _refused(mountpoint="/")


def test_a_mount_point_the_system_lives_in_is_refused() -> None:
    check(_volume(mountpoint="/var/lib/backups"))
    assert "belongs to the system" in _refused(mountpoint="/var")
    assert "belongs to the system" in _refused(mountpoint="/var/lib")


def test_the_size_is_all_of_the_free_space_or_a_binary_amount() -> None:
    check(_volume(size="500G"))
    check(_volume(size="2T"))
    assert "100%" in _refused(size="50%")
    assert "100%" in _refused(size="500 GB")


def test_the_file_system_is_one_the_role_formats() -> None:
    check(_volume(fstype="xfs"))
    assert "ext4 or xfs" in _refused(fstype="btrfs")


def test_an_lvm_volume_names_its_group_and_its_volume() -> None:
    check(_volume(lvm_vg="vg1", lvm_lv="backup"))
    assert "both" in _refused(lvm_vg="vg1")
    assert "not a name LVM takes" in _refused(lvm_vg="-vg", lvm_lv="lv")


def test_the_disk_is_a_path_under_dev() -> None:
    check(_volume(disk="/dev/disk/by-path/pci-0000:00:17.0-ata-1"))
    assert "/dev" in _refused(disk="sda")


def test_the_entry_carries_what_the_role_would_not_default() -> None:
    """The file a human reads later holds the decision and nothing else."""
    assert entry_of(_volume()) == {
        "name": "data",
        "disk": "/dev/sda",
        "mountpoint": "/data",
    }


def test_the_playbook_that_partitions_is_a_reviewed_entry(
    signed_in: TestClient,
) -> None:
    catalogue = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }

    entry = catalogue["seapath_setup_local_storage"]["entry"]
    assert entry["reviewed"] is True
    assert entry["reboots"] == "no"
    assert "neither moved nor resized" in entry["disruption"]
