# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The key an operator hands the service so one node can drive the others.

This is the shortest honest path from a single node to a cluster: the ISO
already installed a site public key in the `ansible` account of every machine,
and the operator holds the private half. It is also the most dangerous thing
this service stores, since that key is root on every machine that trusts it, so
the tests below are as much about what never happens as about what does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.trust import known_hosts, site_key


@pytest.fixture
def key_pair(tmp_path: Path) -> Path:
    path = tmp_path / "site_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True
    )
    return path


def test_the_fingerprint_is_the_one_ssh_keygen_prints(key_pair: Path) -> None:
    # What an operator compares against the machine they took the key from. A
    # fingerprint computed differently here would be worse than none.
    described = site_key.install(key_pair.parent / "ssh", key_pair.read_text())

    printed = subprocess.run(
        ["ssh-keygen", "-lf", str(key_pair.with_suffix(".pub"))],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert described.fingerprint == printed[1]


def test_the_key_is_stored_readable_by_nobody_else(key_pair: Path) -> None:
    ssh_dir = key_pair.parent / "ssh"

    site_key.install(ssh_dir, key_pair.read_text())

    stored = site_key.private_key_file(ssh_dir)
    assert oct(stored.stat().st_mode)[-3:] == "600"
    assert oct(ssh_dir.stat().st_mode)[-3:] == "700"
    assert stored.read_text().strip() == key_pair.read_text().strip()


def test_a_key_with_a_passphrase_is_refused(tmp_path: Path) -> None:
    # Nothing here can type a passphrase during a run, and storing it next to
    # the key it protects would be a decision dressed as a feature.
    path = tmp_path / "protected"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(path)],
        check=True,
    )

    with pytest.raises(site_key.InvalidKey, match="passphrase"):
        site_key.install(tmp_path / "ssh", path.read_text())


def test_the_public_half_is_refused_with_a_message_that_helps(
    key_pair: Path,
) -> None:
    with pytest.raises(site_key.InvalidKey, match="private half"):
        site_key.install(
            key_pair.parent / "ssh", key_pair.with_suffix(".pub").read_text()
        )


def test_the_api_never_gives_the_material_back(
    signed_in: TestClient, key_pair: Path
) -> None:
    material = key_pair.read_text()

    installed = signed_in.put(
        "/api/v1/trust/site-key", json={"material": material}
    ).json()
    state = signed_in.get("/api/v1/trust/site-key").json()

    assert installed["fingerprint"].startswith("SHA256:")
    assert state == installed
    # The one assertion that matters here.
    for payload in (installed, state):
        assert "PRIVATE KEY" not in str(payload)
        assert "material" not in payload


def test_a_viewer_cannot_install_a_key(
    signed_in_viewer: TestClient, key_pair: Path
) -> None:
    response = signed_in_viewer.put(
        "/api/v1/trust/site-key", json={"material": key_pair.read_text()}
    )

    assert response.status_code == 403


def test_removing_the_key_leaves_nothing_behind(
    signed_in: TestClient, settings: Settings, key_pair: Path
) -> None:
    signed_in.put("/api/v1/trust/site-key", json={"material": key_pair.read_text()})

    assert signed_in.delete("/api/v1/trust/site-key").status_code == 204

    assert not site_key.private_key_file(settings.ssh_dir).exists()
    assert not (settings.ssh_dir / "id_site.pub").exists()
    assert signed_in.get("/api/v1/trust/site-key").json()["installed"] is False


def test_a_run_offers_the_site_key_when_there_is_one(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    signed_in.put("/api/v1/trust/site-key", json={"material": key_pair.read_text()})

    signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})

    request = run_adapter.requests[-1]
    assert request.extra_key_files == (settings.site_private_key_file,)


def test_a_run_offers_only_this_node_s_key_when_there_is_none(
    signed_in: TestClient, run_adapter
) -> None:
    signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})

    assert run_adapter.requests[-1].extra_key_files == ()


# The host keys of the machines this node is about to drive.


def test_accepted_host_keys_reach_the_file_ssh_reads(
    signed_in: TestClient, settings: Settings
) -> None:
    entry = {
        "address": "10.132.159.61",
        "key_type": "ssh-ed25519",
        "key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIpeerkey",
        "fingerprint": "SHA256:whatever",
    }

    signed_in.post("/api/v1/trust/host-keys", json={"keys": [entry]})

    live = settings.known_hosts_file.read_text()
    assert "10.132.159.61 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIpeerkey" in live
    # And the local machine's own keys are still there.
    assert "seapath-machine ssh-ed25519" in live


def test_accepted_host_keys_survive_a_restart(
    settings: Settings, host_tree: Path
) -> None:
    # `known_hosts` is rewritten from the machine's own filesystem at every
    # start, so the peers have to live in a record of their own.
    known_hosts.accept_peers(
        settings.known_hosts_file, {"10.132.159.61": ["ssh-ed25519 AAAApeer"]}
    )

    known_hosts.ensure_local(
        settings.known_hosts_file, settings.ssh_config_dir, ["seapath-machine"]
    )

    assert "10.132.159.61 ssh-ed25519 AAAApeer" in settings.known_hosts_file.read_text()


def test_forgetting_a_host_removes_it_from_both_files(
    signed_in: TestClient, settings: Settings
) -> None:
    entry = {
        "address": "10.132.159.61",
        "key_type": "ssh-ed25519",
        "key": "ssh-ed25519 AAAApeer",
        "fingerprint": "SHA256:whatever",
    }
    signed_in.post("/api/v1/trust/host-keys", json={"keys": [entry]})

    assert signed_in.delete("/api/v1/trust/host-keys/10.132.159.61").status_code == 204

    assert "10.132.159.61" not in settings.known_hosts_file.read_text()
    assert signed_in.get("/api/v1/trust/host-keys").json() == []


def test_several_host_keys_are_accepted_in_one_go(
    signed_in: TestClient, settings: Settings
) -> None:
    # Three machines is three fingerprints an operator checks in one sitting,
    # and asking them to click accept three times, reloading the list between
    # each, is how a step gets skipped.
    keys = [
        {
            "address": address,
            "key_type": "ssh-ed25519",
            "key": f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{suffix}",
            "fingerprint": "SHA256:unused-by-the-server",
        }
        for address, suffix in (
            ("10.132.159.60", "aaa"),
            ("10.132.159.61", "bbb"),
            ("10.132.159.62", "ccc"),
        )
    ]

    accepted = signed_in.post("/api/v1/trust/host-keys", json={"keys": keys}).json()

    assert [row["address"] for row in accepted] == [
        "10.132.159.60",
        "10.132.159.61",
        "10.132.159.62",
    ]
    assert all(row["accepted"] for row in accepted)
    # The fingerprint is computed here rather than trusted from the request, so
    # the list a page shows says what is actually stored.
    assert all(row["fingerprint"].startswith("SHA256:") for row in accepted)
    live = settings.known_hosts_file.read_text()
    assert all(row["address"] in live for row in accepted)


def test_an_accepted_key_is_reported_as_accepted_when_scanned_again(
    signed_in: TestClient, settings: Settings
) -> None:
    # What lets the page flip one row's state instead of replacing the list.
    known_hosts.accept_peers(
        settings.known_hosts_file, {"10.132.159.61": ["ssh-ed25519 AAAApeer"]}
    )

    listed = signed_in.get("/api/v1/trust/host-keys").json()

    assert listed[0]["accepted"] is True
    assert listed[0]["key_type"] == "ssh-ed25519"


def test_a_malformed_host_key_is_refused_rather_than_stored(
    signed_in: TestClient, settings: Settings
) -> None:
    # What lands in the record comes from a request body, and a listing that
    # cannot compute a fingerprint for its own contents is a page that fails to
    # open. Found by feeding the API a plausible looking key that was not one.
    response = signed_in.post(
        "/api/v1/trust/host-keys",
        json={
            "keys": [
                {
                    "address": "10.132.159.61",
                    "key_type": "ssh-ed25519",
                    "key": "ssh-ed25519 not-actually-base64",
                    "fingerprint": "SHA256:x",
                }
            ]
        },
    )

    assert response.status_code == 400
    assert signed_in.get("/api/v1/trust/host-keys").json() == []
    assert "10.132.159.61" not in settings.known_hosts_file.read_text()
