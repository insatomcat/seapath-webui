# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Self trust, and the file it has to share with the ISO.

The test that matters most in this file is the one that starts from an
`authorized_keys` carrying the site key baked into the image at build time, and
asserts it is still there, untouched, after provisioning and after revocation.
Clobbering it locks out every conventional Ansible control machine, and nobody
finds out until the day they need one.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app.trust import authorized_keys
from app.trust.authorized_keys import AuthorizedKey, MissingAccount
from app.trust.keys import ensure_key_pair
from app.trust.service import TrustService

# What srv_fai_config/scripts/SEAPATH_COMMON/40-networking writes into the
# account at image build time, as `ainsl /home/ansible/.ssh/authorized_keys
# "$ansiblekey"`.
SITE_KEY = (
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7site+key+from+the+iso "
    "ansible@control-machine"
)


@pytest.fixture
def ssh_home(tmp_path: Path) -> Path:
    """The `ansible` account's .ssh, as the ISO leaves it."""
    home = tmp_path / "home/ansible/.ssh"
    home.mkdir(parents=True)
    authorized = home / "authorized_keys"
    authorized.write_text(SITE_KEY + "\n")
    authorized.chmod(0o600)
    return home


@pytest.fixture
def service(tmp_path: Path, ssh_home: Path) -> TrustService:
    return TrustService(
        ssh_dir=tmp_path / "state/ssh",
        authorized_keys_file=ssh_home / "authorized_keys",
    )


def test_the_site_key_survives_provisioning_and_revocation(
    service: TrustService, ssh_home: Path
) -> None:
    authorized = ssh_home / "authorized_keys"

    relation, _ = service.ensure_self_trust("node1", ["192.168.200.121"])
    after_install = authorized.read_text()
    service.revoke(relation.comment)
    after_revocation = authorized.read_text()

    assert SITE_KEY in after_install
    assert after_install.splitlines()[0] == SITE_KEY
    # Back to exactly the file the ISO produced, byte for byte.
    assert after_revocation == SITE_KEY + "\n"


def test_the_installed_line_byte_for_byte(
    service: TrustService, ssh_home: Path
) -> None:
    service.ensure_self_trust("node1", ["192.168.200.121"])
    line = (ssh_home / "authorized_keys").read_text().splitlines()[1]

    public_key = service.self_key().public_key

    # from= sorted so the line is stable between two starts, loopback always
    # present because a connection a node makes to itself may leave from there,
    # and `pty` after `restrict` because the console connects over this
    # relation. Without it sshd refuses the terminal and the console closes as
    # it opens. A run needs none: the ISO sets `Defaults:ansible !requiretty`
    # in sudoers.
    assert line == (
        'from="127.0.0.1,192.168.200.121,::1",restrict,pty '
        f"{public_key} seapath-webui:node1->node1"
    )


def test_there_is_no_command_restriction_and_that_is_deliberate(
    service: TrustService, ssh_home: Path
) -> None:
    service.ensure_self_trust("node1", ["192.168.200.121"])
    line = (ssh_home / "authorized_keys").read_text().splitlines()[1]

    # The sudoers rule the ISO ships grants NOPASSWD:EXEC:SETENV: /bin/sh,
    # which is arbitrary root by construction. A command= here would look like
    # a limit and be none.
    assert "command=" not in line
    assert "restrict" in line


def test_a_peer_relation_gets_no_terminal(ssh_home: Path) -> None:
    # The console only ever connects to this machine, so the exception stops
    # at the relation a node has with itself. A key that carries runs to
    # another node keeps `restrict` whole.
    line = AuthorizedKey(
        comment="seapath-webui:node1->node2",
        public_key="ssh-ed25519 AAAAkey",
        from_addresses=("192.168.200.122",),
    ).render()

    assert line == (
        'from="192.168.200.122",restrict ssh-ed25519 AAAAkey '
        "seapath-webui:node1->node2"
    )


def test_provisioning_twice_does_not_rewrite_the_file(
    service: TrustService, ssh_home: Path
) -> None:
    authorized = ssh_home / "authorized_keys"
    service.ensure_self_trust("node1", ["192.168.200.121"])
    before = authorized.stat().st_mtime_ns

    _, changed = service.ensure_self_trust("node1", ["192.168.200.121"])

    assert changed is False
    assert authorized.stat().st_mtime_ns == before


def test_a_changed_administration_address_repairs_the_restriction(
    service: TrustService, ssh_home: Path
) -> None:
    service.ensure_self_trust("node1", ["192.168.200.121"])

    # seapath_setup_network.yaml can move the administration address, and a
    # from= naming the old one authorises nothing.
    _, changed = service.ensure_self_trust("node1", ["192.168.200.130"])
    content = (ssh_home / "authorized_keys").read_text()

    assert changed is True
    assert "192.168.200.130" in content
    assert "192.168.200.121" not in content
    assert len(content.splitlines()) == 2


def test_a_renamed_node_does_not_leave_an_orphan_line(
    service: TrustService, ssh_home: Path
) -> None:
    service.ensure_self_trust("node1", ["192.168.200.121"])

    service.ensure_self_trust("node-renamed", ["192.168.200.121"])
    lines = (ssh_home / "authorized_keys").read_text().splitlines()

    # The old line authorises the same key under a name nobody will recognise.
    assert len(lines) == 2
    assert lines[0] == SITE_KEY
    assert lines[1].endswith("seapath-webui:node-renamed->node-renamed")


def test_revocation_removes_exactly_one_relation(ssh_home: Path) -> None:
    authorized = ssh_home / "authorized_keys"
    for name in ("node2", "node3"):
        authorized_keys.install(
            authorized,
            AuthorizedKey(
                comment=f"seapath-webui:node1->{name}",
                public_key=f"ssh-ed25519 AAAAkeyfor{name}",
                from_addresses=("192.168.200.122",),
            ),
        )

    authorized_keys.remove(authorized, "seapath-webui:node1->node2")
    remaining = authorized.read_text().splitlines()

    assert len(remaining) == 2
    assert remaining[0] == SITE_KEY
    assert remaining[1].endswith("seapath-webui:node1->node3")


def test_a_site_key_whose_comment_looks_like_ours_is_still_not_touched(
    ssh_home: Path,
) -> None:
    # Only the prefix identifies our lines, and only on a whole line basis.
    authorized = ssh_home / "authorized_keys"
    decoy = "ssh-rsa AAAAdecoy notseapath-webui:node1->node1"
    authorized.write_text(SITE_KEY + "\n" + decoy + "\n")

    authorized_keys.remove(authorized, "seapath-webui:node1->node1")

    assert authorized.read_text() == SITE_KEY + "\n" + decoy + "\n"


def test_a_comment_and_a_blank_line_are_preserved(ssh_home: Path) -> None:
    authorized = ssh_home / "authorized_keys"
    authorized.write_text(f"# the site key, do not remove\n\n{SITE_KEY}\n")

    authorized_keys.install(
        authorized,
        AuthorizedKey(comment="seapath-webui:node1->node1", public_key="ssh-ed25519 A"),
    )
    lines = authorized.read_text().splitlines()

    assert lines[0] == "# the site key, do not remove"
    assert lines[1] == ""
    assert lines[2] == SITE_KEY


def test_the_file_keeps_its_mode_and_owner(
    service: TrustService, ssh_home: Path
) -> None:
    authorized = ssh_home / "authorized_keys"
    before = authorized.stat()

    service.ensure_self_trust("node1", ["192.168.200.121"])
    after = authorized.stat()

    assert stat.S_IMODE(after.st_mode) == 0o600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_a_missing_ansible_account_is_refused_rather_than_created(
    tmp_path: Path,
) -> None:
    service = TrustService(
        ssh_dir=tmp_path / "state/ssh",
        authorized_keys_file=tmp_path / "nowhere/.ssh/authorized_keys",
    )

    # Inventing a user with privileges nobody reviewed is not a recovery.
    with pytest.raises(MissingAccount, match="does not create accounts"):
        service.ensure_self_trust("node1", ["192.168.200.121"])


def test_the_private_key_is_generated_once_and_kept_private(tmp_path: Path) -> None:
    first = ensure_key_pair(tmp_path / "ssh", "id_ed25519_self")
    second = ensure_key_pair(tmp_path / "ssh", "id_ed25519_self")

    # Regenerating would leave the installed line authorising nothing.
    assert first.public_key == second.public_key
    assert first.public_key.startswith("ssh-ed25519 ")
    assert first.fingerprint.startswith("SHA256:")
    assert stat.S_IMODE(first.private_key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ssh").stat().st_mode) == 0o700


def test_the_trust_view_reports_what_sshd_would_accept(
    service: TrustService, ssh_home: Path
) -> None:
    service.ensure_self_trust("node1", ["192.168.200.121"])
    assert service.relations("node1")[0].installed is True

    # Someone edited the file by hand. The view must not keep claiming the
    # relation exists.
    (ssh_home / "authorized_keys").write_text(SITE_KEY + "\n")

    assert service.relations("node1")[0].installed is False
