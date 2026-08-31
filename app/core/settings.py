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

    # Listen socket. The Ansible role binds this to the administration address.
    bind_address: str = "0.0.0.0"
    port: int = 8006

    # Service state. This directory and the inventory repository are the only
    # two places on the host the service ever writes, plus the `authorized_keys`
    # of the `ansible` account from M1 on.
    state_dir: Path = Path("/etc/seapath/webui")

    # Root of the host filesystem as seen from this process. Always "/" in the
    # container, a fixture tree in the tests.
    host_root: Path = Path("/")

    # The inventory repository, separate from the service state so it can be
    # backed up, cloned and exported on its own.
    inventory_dir: Path = Path("/etc/seapath/inventory")
    # Run artefacts, written as a run progresses.
    runs_dir: Path = Path("/var/lib/seapath-webui/runs")
    # Where the image installed the seapath.ansible collection.
    collections_path: Path = Path("/opt/ansible/collections")
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
