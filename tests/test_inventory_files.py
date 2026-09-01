# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The folder around the inventory: the files it names, versioned or not."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.artefacts import ArtefactStore
from app.inventory.files import UnsafePath, relative_path, resolve_within
from app.inventory.repository import InventoryRepository

# 1. The rules that keep a store inside its own directory.


@pytest.mark.parametrize(
    "value",
    [
        "/etc/shadow",
        "../../etc/shadow",
        "inventories_private/../../escape",
        ".git/config",
        "",
        "   ",
    ],
)
def test_a_path_that_leaves_the_folder_is_refused(value: str) -> None:
    with pytest.raises(UnsafePath):
        relative_path(value)


def test_a_symlink_already_in_the_tree_is_refused_too(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    (root / "private").mkdir(parents=True)
    (root / "out").symlink_to(tmp_path / "elsewhere")

    # The same escape with one more step in it: the path a caller writes is
    # innocent, and the link makes it land outside anyway.
    with pytest.raises(UnsafePath):
        resolve_within(root, "out/secret")
    assert resolve_within(root, "private/quadlet.network").name == "quadlet.network"


# 2. The repository, which versions everything it holds.


@pytest.fixture
def repository(tmp_path: Path) -> InventoryRepository:
    repository = InventoryRepository(tmp_path / "inventory")
    repository.initialise()
    return repository


def test_a_companion_file_is_a_commit_like_any_other_change(
    repository: InventoryRepository,
) -> None:
    commit = repository.write_file(
        "inventories_private/quadlet-macvlan.network",
        b"[Network]\n",
        message="files: add inventories_private/quadlet-macvlan.network",
        author="alice",
    )

    assert commit is not None
    assert commit.author == "alice"
    assert [entry.path for entry in repository.files()] == [
        "inventories_private/quadlet-macvlan.network"
    ]
    assert repository.read_file("inventories_private/quadlet-macvlan.network") == (
        b"[Network]\n"
    )


def test_writing_the_same_bytes_twice_creates_no_commit(
    repository: InventoryRepository,
) -> None:
    repository.write_file("files/a.conf", b"one\n", message="first", author="alice")

    assert (
        repository.write_file("files/a.conf", b"one\n", message="again", author="alice")
        is None
    )


def test_removing_a_file_takes_the_directory_it_emptied(
    repository: InventoryRepository,
) -> None:
    repository.write_file("files/a.conf", b"one\n", message="add", author="alice")

    commit = repository.delete_file("files/a.conf", message="remove", author="alice")

    assert commit is not None
    assert repository.files() == []
    # git records no empty directory, so leaving one would make this folder and
    # a fresh clone of it differ.
    assert not (repository.path / "files").exists()


def test_the_history_carries_a_file_change(repository: InventoryRepository) -> None:
    repository.commit(content="all: {}\n", message="inventory: seed", author="alice")
    repository.write_file(
        "files/a.conf", b"one\n", message="files: add files/a.conf", author="bob"
    )

    assert [commit.message for commit in repository.history()] == [
        "files: add files/a.conf",
        "inventory: seed",
    ]
    # The diff is of the folder rather than of the one file, because a commit
    # that replaced a quadlet changed what the next run pushes.
    assert "files/a.conf" in repository.diff(from_ref="HEAD~1", to_ref="HEAD")


def test_the_export_carries_the_whole_folder(
    repository: InventoryRepository,
) -> None:
    import tarfile
    from io import BytesIO

    repository.commit(content="all: {}\n", message="seed", author="alice")
    repository.write_file("files/a.conf", b"one\n", message="add", author="alice")

    with tarfile.open(fileobj=BytesIO(repository.export()), mode="r:gz") as archive:
        names = archive.getnames()

    assert "seapath-inventory/inventory.yaml" in names
    assert "seapath-inventory/files/a.conf" in names


# 3. The artefacts, which are the same files without the history.


def test_an_artefact_is_written_whole_or_not_at_all(tmp_path: Path) -> None:
    store = ArtefactStore(tmp_path / "artefacts")

    def failing():
        yield b"the first megabyte"
        raise OSError("the disk filled up")

    with pytest.raises(OSError, match="disk filled"):
        store.write("files/guest.qcow2", failing())

    # Half a VM image, pushed to three hypervisors, is what this prevents.
    assert store.files() == []
    assert list((tmp_path / "artefacts/files").iterdir()) == []


def test_an_artefact_replaces_the_previous_one(tmp_path: Path) -> None:
    store = ArtefactStore(tmp_path / "artefacts")
    store.write("files/guest.qcow2", [b"one"])

    stored = store.write("files/guest.qcow2", [b"two", b"three"])

    assert stored.size == 8
    assert store.file_path("files/guest.qcow2").read_bytes() == b"twothree"
    assert store.delete("files/guest.qcow2") is True
    assert store.delete("files/guest.qcow2") is False


# 4. The same, seen from the API.


def test_the_folder_lists_both_stores(signed_in: TestClient) -> None:
    signed_in.put(
        "/api/v1/inventory/files/inventories_private/quadlet.network",
        content=b"[Network]\n",
    )
    signed_in.put("/api/v1/inventory/artefacts/files/guest.qcow2", content=b"\x00" * 16)

    folder = signed_in.get("/api/v1/inventory/folder").json()

    assert [entry["path"] for entry in folder["files"]] == [
        "inventories_private/quadlet.network"
    ]
    assert [entry["path"] for entry in folder["artefacts"]] == ["files/guest.qcow2"]
    # The one is text and the other is not, read from the bytes rather than
    # from the extension: a site's `.conf` is text and its `.qcow2` is not,
    # whatever they are called.
    assert folder["files"][0]["text"] is True
    assert folder["artefacts"][0]["text"] is False
    assert folder["free_bytes"] > 0


def test_a_file_comes_back_byte_for_byte(signed_in: TestClient) -> None:
    signed_in.put("/api/v1/inventory/files/files/snmpd.conf", content=b"rocommunity\n")

    response = signed_in.get("/api/v1/inventory/files/files/snmpd.conf")

    assert response.status_code == 200
    assert response.content == b"rocommunity\n"


def test_the_inventory_itself_is_not_uploaded_here(signed_in: TestClient) -> None:
    response = signed_in.put(
        "/api/v1/inventory/files/inventory.yaml", content=b"all:\n"
    )

    # It is the one file here that is parsed, validated and checked against
    # Ansible before it is written, and this route does none of that.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "refused_file"


def test_a_file_too_large_is_pointed_at_the_artefacts(
    signed_in: TestClient, settings: Settings
) -> None:
    oversized = b"\x00" * (settings.max_inventory_file_bytes + 1)

    response = signed_in.put(
        "/api/v1/inventory/files/files/guest.qcow2", content=oversized
    )

    assert response.status_code == 413
    assert "artefacts" in response.json()["error"]["message"]
    # And the same bytes are taken by the store that exists for them.
    assert (
        signed_in.put(
            "/api/v1/inventory/artefacts/files/guest.qcow2", content=oversized
        ).status_code
        == 200
    )


def test_a_path_leaving_the_folder_is_refused_by_the_api(
    signed_in: TestClient, settings: Settings
) -> None:
    response = signed_in.put(
        "/api/v1/inventory/files/../../etc/cron.d/backdoor", content=b"* * * * * root\n"
    )

    assert response.status_code in (400, 404)
    assert not (settings.inventory_dir.parent.parent / "etc").exists()


def test_deleting_a_file_that_is_not_there_says_so(signed_in: TestClient) -> None:
    response = signed_in.delete("/api/v1/inventory/files/files/absent.conf")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_file"


def test_a_viewer_reads_the_folder_and_writes_nothing(
    signed_in_viewer: TestClient,
) -> None:
    assert signed_in_viewer.get("/api/v1/inventory/folder").status_code == 200
    assert (
        signed_in_viewer.put(
            "/api/v1/inventory/files/files/a.conf", content=b"x"
        ).status_code
        == 403
    )
    assert (
        signed_in_viewer.put(
            "/api/v1/inventory/artefacts/files/a.qcow2", content=b"x"
        ).status_code
        == 403
    )
