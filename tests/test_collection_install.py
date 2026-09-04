# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Installing a collection on the node, which is how a fix reaches a site.

The real `ansible-galaxy` runs here, against a hand written archive and a
temporary directory. Nothing reaches a galaxy server: the install is given a
local file and `--no-deps`, which is the whole point on a machine that has no
route to one.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from app.runs import catalogue
from app.runs.install import (
    LOCK_ID,
    CollectionInstaller,
    InstallFailed,
    RefusedArchive,
    inspect,
)
from app.runs.store import RunLocked, RunStore
from tests.fakes import build_collection_archive, write_fake_collection


@pytest.fixture
def image(tmp_path: Path) -> Path:
    """The collection the image ships, with a dependency beside it."""
    root = write_fake_collection(tmp_path / "image", contents="---\n# the image's\n")
    dependency = root / "ansible_collections/community/general"
    dependency.mkdir(parents=True)
    # A real one, because ansible-galaxy reads every collection already under
    # the path it installs into and refuses the whole install on one it cannot
    # make sense of.
    (dependency / "MANIFEST.json").write_text(
        json.dumps(
            {
                "collection_info": {
                    "namespace": "community",
                    "name": "general",
                    "version": "8.0.0",
                }
            }
        )
    )
    (dependency / "FILES.json").write_text(json.dumps({"files": [], "format": 1}))
    return root


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


@pytest.fixture
def installer(tmp_path: Path, image: Path, store: RunStore) -> CollectionInstaller:
    return CollectionInstaller(site_dir=tmp_path / "site", image_dir=image, store=store)


def test_the_archive_says_which_collection_it_holds(tmp_path: Path) -> None:
    info = inspect(build_collection_archive(tmp_path, version="2.0.1"))

    assert info.collection == "seapath.ansible"
    assert info.version == "2.0.1"
    assert len(info.digest) == 64


def test_an_archive_of_another_collection_is_refused(tmp_path: Path) -> None:
    # Installing community.general here would leave the node with no SEAPATH
    # playbook at all, and the operator would find out at the next apply.
    archive = build_collection_archive(tmp_path, namespace="community", name="general")

    with pytest.raises(RefusedArchive) as error:
        inspect(archive)

    assert "community.general" in str(error.value)


def test_something_that_is_not_an_archive_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "notes.txt"
    plain.write_text("not a collection")

    with pytest.raises(RefusedArchive) as error:
        inspect(plain)

    assert "gzipped tar" in str(error.value)


def test_an_archive_with_no_manifest_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "README.md").write_text("hello")
    archive = tmp_path / "anything.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload / "README.md", arcname="README.md")

    with pytest.raises(RefusedArchive) as error:
        inspect(archive)

    assert "MANIFEST.json" in str(error.value)


def test_a_member_writing_outside_the_directory_is_refused(tmp_path: Path) -> None:
    # The archive arrives over HTTP. This is the mistake shape, and it is
    # cheap to refuse before ansible-galaxy is handed the file.
    escaping = tmp_path / "escaping.tar.gz"
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "collection_info": {
                    "namespace": "seapath",
                    "name": "ansible",
                    "version": "9",
                }
            }
        )
    )
    with tarfile.open(escaping, "w:gz") as tar:
        tar.add(manifest, arcname="MANIFEST.json")
        tar.add(manifest, arcname="../../etc/seapath/webui/stolen.json")

    with pytest.raises(RefusedArchive) as error:
        inspect(escaping)

    assert "outside" in str(error.value)


def test_installing_makes_the_node_run_the_uploaded_collection(
    tmp_path: Path, image: Path, installer: CollectionInstaller
) -> None:
    archive = build_collection_archive(tmp_path, version="2.0.1")
    before = catalogue.identity(image)

    info = installer.install(archive)

    assert info.version == "2.0.1"
    assert installer.installed()
    # The whole point: the root a run resolves is now the site's.
    assert catalogue.select_root(installer.site_dir, image) == installer.site_dir
    assert catalogue.identity(installer.site_dir) != before
    assert catalogue.identity(installer.site_dir).startswith("2.0.1+")


def test_the_installed_tree_carries_the_dependencies_the_image_ships(
    tmp_path: Path, installer: CollectionInstaller
) -> None:
    # A site tree holding only seapath.ansible would refuse every run with
    # `couldn't resolve module/action 'community.general.modprobe'`, because
    # the root that wins, wins whole.
    installer.install(build_collection_archive(tmp_path))

    assert (
        installer.site_dir / "ansible_collections/community/general/MANIFEST.json"
    ).is_file()


def test_installing_twice_does_not_accumulate(
    tmp_path: Path, installer: CollectionInstaller
) -> None:
    # Each install seeds from the image again, so what a previous archive left
    # behind and this one does not carry is gone.
    first = build_collection_archive(
        tmp_path / "first", entries=["seapath_setup_main", "cluster_setup_ha"]
    )
    second = build_collection_archive(
        tmp_path / "second", entries=["seapath_setup_main"]
    )
    (tmp_path / "first").mkdir(exist_ok=True)

    installer.install(first)
    installer.install(second)

    playbooks = installer.site_dir / "ansible_collections/seapath/ansible/playbooks"
    assert (playbooks / "seapath_setup_main.yaml").is_file()
    assert not (playbooks / "cluster_setup_ha.yaml").exists()


def test_a_failed_install_leaves_the_node_running_what_it_ran(
    tmp_path: Path, image: Path, store: RunStore
) -> None:
    # A node whose collection is half replaced cannot converge, and cannot be
    # repaired by converging. The live tree is only ever replaced by a rename.
    def refusing(argv: list[str], cwd: Path) -> str:
        raise InstallFailed("ansible-galaxy said no")

    site = tmp_path / "site"
    CollectionInstaller(site_dir=site, image_dir=image, store=store).install(
        build_collection_archive(tmp_path / "first", version="2.0.1")
    )
    installed = catalogue.identity(site)

    with pytest.raises(InstallFailed):
        CollectionInstaller(
            site_dir=site, image_dir=image, store=store, runner=refusing
        ).install(build_collection_archive(tmp_path / "second", version="9.9.9"))

    assert catalogue.identity(site) == installed
    assert installed.startswith("2.0.1+")


def test_the_exact_ansible_galaxy_invocation(
    tmp_path: Path, image: Path, store: RunStore
) -> None:
    # The one command this module runs. `--no-deps` is what keeps a substation
    # with no route to a galaxy server from waiting on a timeout, and the
    # dependencies come from the seed instead.
    seen: list[list[str]] = []

    def capture(argv: list[str], cwd: Path) -> str:
        seen.append(argv)
        write_fake_collection(Path(argv[argv.index("--collections-path") + 1]))
        return ""

    installer = CollectionInstaller(
        site_dir=tmp_path / "site", image_dir=image, store=store, runner=capture
    )
    archive = build_collection_archive(tmp_path)

    installer.install(archive)

    staging = tmp_path / ".site.installing"
    assert seen == [
        [
            "ansible-galaxy",
            "collection",
            "install",
            str(archive),
            "--collections-path",
            str(staging),
            "--force",
            "--no-deps",
        ]
    ]


def test_a_run_that_is_going_refuses_the_install(
    tmp_path: Path, installer: CollectionInstaller, store: RunStore
) -> None:
    # Replacing the tree under a convergence breaks it in the middle of a
    # substation hypervisor: a staged mirror is symlinks into that tree.
    store.acquire("20260904-abcdef")

    with pytest.raises(RunLocked) as error:
        installer.install(build_collection_archive(tmp_path))

    assert "20260904-abcdef" in str(error.value)
    assert not installer.installed()


def test_an_install_that_is_going_names_itself_to_a_run(
    tmp_path: Path, installer: CollectionInstaller, store: RunStore
) -> None:
    # The other direction. A refusal saying "Run collection-install is already
    # going" names nothing an operator can look up.
    store.acquire(LOCK_ID, "A collection installation")

    with pytest.raises(RunLocked) as error:
        store.acquire("20260904-abcdef")

    assert "A collection installation is already going" in str(error.value)


def test_the_lock_is_released_when_the_install_is_over(
    tmp_path: Path, installer: CollectionInstaller, store: RunStore
) -> None:
    installer.install(build_collection_archive(tmp_path))

    assert not store.locked()


def test_a_lock_left_by_a_service_that_died_is_cleared_at_the_next_start(
    store: RunStore,
) -> None:
    # Nothing can legitimately hold it at a start, and a node refusing every
    # run until someone deletes a file is worse than the concurrency it
    # guards against.
    store.acquire(LOCK_ID, "A collection installation")

    store.reconcile()

    assert not store.locked()


def test_removing_falls_back_to_the_collection_the_image_ships(
    tmp_path: Path, image: Path, installer: CollectionInstaller
) -> None:
    # The undo, and the reason installing on a live node is safe to attempt.
    installer.install(build_collection_archive(tmp_path))

    assert installer.remove() is True
    assert not installer.installed()
    assert catalogue.select_root(installer.site_dir, image) == image


def test_removing_nothing_says_so(installer: CollectionInstaller) -> None:
    assert installer.remove() is False


# The endpoint. What an administrator does through the UI, and what the audit
# trail is left holding afterwards.


def test_the_api_says_which_collection_the_node_runs(signed_in) -> None:
    body = signed_in.get("/api/v1/collection").json()

    assert body["source"] == "image"
    assert body["site_installed"] is False
    assert body["image_version"] == "test"
    assert body["version"].startswith("2.0.0+")


def test_installing_through_the_api_changes_what_the_next_run_executes(
    signed_in, tmp_path: Path
) -> None:
    archive = build_collection_archive(tmp_path / "upload", version="2.0.1")

    response = signed_in.put("/api/v1/collection", content=archive.read_bytes())

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "site"
    assert response.json()["version"].startswith("2.0.1+")
    # The image's version is still reported, because a node running neither is
    # a node whose two halves an operator has to be able to compare.
    assert response.json()["image_version"] == "test"

    # And the service reads the new tree with no restart.
    assert signed_in.get("/api/v1/collection").json()["source"] == "site"


def test_an_install_leaves_a_commit_in_the_audit_trail(
    signed_in, tmp_path: Path
) -> None:
    # The desired state did not move and the code that applies it did. The
    # inventory is where this service answers "who changed what, and when", so
    # it is where an operator looks for this too.
    archive = build_collection_archive(tmp_path / "upload", version="2.0.1")

    signed_in.put("/api/v1/collection", content=archive.read_bytes())

    history = signed_in.get("/api/v1/inventory/history").json()
    assert "Install the seapath.ansible collection 2.0.1" in history[0]["message"]
    assert history[0]["author"] == "admin"


def test_the_api_refuses_an_archive_that_is_not_this_collection(
    signed_in, tmp_path: Path
) -> None:
    archive = build_collection_archive(
        tmp_path / "upload", namespace="community", name="general"
    )

    response = signed_in.put("/api/v1/collection", content=archive.read_bytes())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_archive"


def test_the_api_refuses_an_empty_body(signed_in) -> None:
    response = signed_in.put("/api/v1/collection", content=b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_archive"


def test_removing_through_the_api_falls_back_to_the_image(
    signed_in, tmp_path: Path
) -> None:
    archive = build_collection_archive(tmp_path / "upload", version="2.0.1")
    signed_in.put("/api/v1/collection", content=archive.read_bytes())

    response = signed_in.delete("/api/v1/collection")

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "image"
    assert response.json()["version"].startswith("2.0.0+")


def test_removing_nothing_is_refused_by_the_api(signed_in) -> None:
    response = signed_in.delete("/api/v1/collection")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_site_collection"


def test_a_viewer_may_read_the_collection_and_not_replace_it(
    signed_in_viewer, tmp_path: Path
) -> None:
    # Which code a run executed is half of what a viewer is here to read.
    # Replacing it is an apply in everything but name.
    assert signed_in_viewer.get("/api/v1/collection").status_code == 200

    archive = build_collection_archive(tmp_path / "upload")
    response = signed_in_viewer.put("/api/v1/collection", content=archive.read_bytes())

    assert response.status_code == 403


def test_an_installed_collection_decides_which_playbooks_are_offered(
    signed_in, tmp_path: Path
) -> None:
    # The product claim of D23, end to end: the list an operator sees is the
    # collection the node is running, and it changed without a restart.
    archive = build_collection_archive(
        tmp_path / "upload", version="2.0.1", entries=["seapath_setup_main"]
    )

    signed_in.put("/api/v1/collection", content=archive.read_bytes())

    playbooks = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }
    assert playbooks["seapath_setup_main"]["available"] is True
    assert playbooks["cluster_setup_ha"]["available"] is False
    assert any(
        "2.0.1" in reason for reason in playbooks["cluster_setup_ha"]["unmet"]
    ), playbooks["cluster_setup_ha"]["unmet"]
