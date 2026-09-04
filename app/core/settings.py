# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Runtime settings, read from the environment.

Every path is configurable because the test suite must run on a laptop with no
SEAPATH machine anywhere: `host_root` in particular lets the read only adapter
be pointed at a recorded `/proc` and `/sys` tree instead of the real one.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEAPATH_WEBUI_",
        env_file=None,
        extra="ignore",
    )

    # Listen socket. `auto` binds the address of the interface carrying the
    # default route, which is the administration address the inventory calls
    # `ip_addr`. A substation hypervisor also sits on the networks that carry
    # sampled values and the storage traffic, and this UI has no business
    # answering on those, so the wildcard is never the default: a fresh ISO
    # resolves the address at start, and the Ansible role substitutes the one
    # the inventory holds once it exists.
    bind_address: str = "auto"
    port: int = 8006

    # Service state. This directory and the inventory repository are the only
    # two places on the host the service ever writes, plus the `authorized_keys`
    # of the `ansible` account from M1 on.
    state_dir: Path = Path("/etc/seapath/webui")

    # Root of the host filesystem as seen from this process. Always "/" in the
    # container, a fixture tree in the tests.
    host_root: Path = Path("/")
    # Where the host's whole /etc is readable. The quadlet mounts it there for
    # PAM, and the real time reading takes the tuned profile out of the same
    # mount rather than asking for one of its own. A source checkout and the
    # tests point it at their own tree.
    host_etc_root: Path = Path("/run/host/etc")

    # The inventory repository, separate from the service state so it can be
    # backed up, cloned and exported on its own. A folder rather than a file:
    # it holds `inventory.yaml` and the files that inventory names, which the
    # `upload_extra_files`, `iptables`, `syslog_ng_client` and VM roles all
    # take as ordinary variables.
    inventory_dir: Path = Path("/etc/seapath/inventory")
    # The large files the inventory names, kept out of git. A qcow2 in a
    # repository stays in its history forever, one copy per upload, and takes
    # the export and the clone with it. A run overlays this store under the
    # same root as the repository, so a path resolves the same either way.
    artefacts_dir: Path = Path("/var/lib/seapath-webui/artefacts")
    # Above this, a file is refused by the versioned folder and pointed at the
    # artefacts. Four megabytes takes every configuration file the roles read,
    # and no disk image.
    max_inventory_file_bytes: int = 4 * 1024 * 1024
    # Run artefacts, written as a run progresses.
    runs_dir: Path = Path("/var/lib/seapath-webui/runs")
    # Where the image installed the seapath.ansible collection, and what a
    # source checkout is pointed at.
    collections_path: Path = Path("/opt/ansible/collections")
    # The site's own collection, under the state volume the quadlet already
    # mounts, so a corrected playbook reaches a node without an image build.
    # It wins whole over the one the image ships, and the two are never
    # stacked: a run records one fingerprint, and a tree assembled from two
    # installs is one no CI has executed. Empty on a node nobody has updated,
    # which is the ordinary case. See D23 in docs/decisions.md.
    site_collections_dir: Path = Path("/var/lib/seapath-webui/collections")
    # The host's sshd configuration, read only, for its public host keys.
    ssh_config_dir: Path = Path("/etc/ssh")
    # The ssh client configuration this service writes for its own runs, so
    # that the ssh commands a role spawns itself are given the same keys as the
    # connection. Root's, and by its absolute path: ssh resolves `~/.ssh/config`
    # through the password database, not through HOME. Inside the container
    # this is the image's own /root, never a path on the host.
    client_ssh_config_file: Path = Path("/root/.ssh/config")

    # The account every Ansible connection targets, including the connection to
    # this very machine. It must match `ansible_user` in the inventory, and the
    # reference inventories say `ansible`. The service never creates it.
    ansible_user: str = "ansible"
    # Looked up with `getent` by the Ansible role and templated into the
    # quadlet, never hardcoded to /home/ansible on a real deployment.
    ansible_ssh_dir: Path = Path("/home/ansible/.ssh")

    # Unix groups granting each role. An account in none of them, and not root,
    # is authenticated but has no access at all.
    admin_group: str = "seapath-admin"
    operator_group: str = "seapath-operator"
    viewer_group: str = "seapath-viewer"

    # PAM service file shipped in the image, so the stack the service
    # authenticates against does not depend on what the host happens to install.
    pam_service: str = "seapath-webui"

    # Extra subject alternative names for the self signed certificate, comma
    # separated. The Ansible role fills this with `ip_addr` and, in a cluster,
    # `cluster_ip_addr`, because the inventory is where those addresses live.
    # It only affects how loudly the browser complains: what the operator
    # verifies, and what the trust exchange pins, is the fingerprint.
    tls_additional_sans: str = ""

    # The console. A terminal in the browser, on the machine this service runs
    # on, over the same ssh path a run uses. A site that wants no shell served
    # from here turns this off, which takes the endpoint away along with the
    # button.
    console_enabled: bool = True
    # The loopback rather than the administration address: the quadlet puts
    # this container in the host network namespace, so 127.0.0.1 is the host's
    # sshd; the self trust names the loopback in its `from=` clause; and
    # `known_hosts` records it at every start. An address change therefore
    # cannot break the console.
    console_target: str = "127.0.0.1"
    # Who may open one. The default is every authenticated account, which is
    # what a node local UI on a machine whose operators already have accounts
    # is for. Raise it to `operator` or `admin` on a site where reading the
    # node view and holding a shell on it are meant to be different rights:
    # the account the console reaches has passwordless sudo, so a console is
    # root on this machine whatever the role that opened it.
    console_min_role: str = "viewer"
    # Concurrent consoles. Enough for two operators and a forgotten tab, few
    # enough that a page reloading in a loop cannot exhaust the node's sshd.
    console_max_sessions: int = 4
    # A console with nobody typing into it is closed. 0 disables the timeout,
    # which is a decision a site can make and this service will not make for it.
    console_idle_timeout_seconds: int = 900

    # D6: the ISO must produce a machine reachable from a browser with no prior
    # Ansible run, so root is accepted as an administrator. Sites that harden
    # further can turn this off once another account exists.
    allow_root_login: bool = True

    session_ttl_seconds: int = 8 * 3600
    session_cookie_name: str = "seapath_session"
    csrf_cookie_name: str = "seapath_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    log_level: str = "INFO"

    # Baked into the image at build time by the Dockerfile. Which playbooks
    # exist and what they do is a property of this version, so a run is only
    # reproducible when it is recorded next to the inventory commit.
    collection_version: str = "unknown"

    # Development switch: serve the node view from the fake adapter so the UI
    # can be worked on without a SEAPATH machine. Never set on a real node, and
    # the service says so loudly at startup.
    use_fakes: bool = False

    @property
    def pki_dir(self) -> Path:
        return self.state_dir / "pki"

    @property
    def tls_cert_file(self) -> Path:
        return self.pki_dir / "server.crt"

    @property
    def tls_key_file(self) -> Path:
        return self.pki_dir / "server.key"

    @property
    def session_secret_file(self) -> Path:
        return self.state_dir / "session.secret"

    @property
    def ssh_dir(self) -> Path:
        return self.state_dir / "ssh"

    @property
    def authorized_keys_file(self) -> Path:
        return self.ansible_ssh_dir / "authorized_keys"

    @property
    def self_private_key_file(self) -> Path:
        return self.ssh_dir / "id_ed25519_self"

    @property
    def known_hosts_file(self) -> Path:
        return self.ssh_dir / "known_hosts"

    @property
    def site_private_key_file(self) -> Path:
        """The key an operator uploaded so this node can reach the others."""
        return self.ssh_dir / "id_site"


@lru_cache
def get_settings() -> Settings:
    return Settings()
