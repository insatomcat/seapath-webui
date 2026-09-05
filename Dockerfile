# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

# Python 3.11 to match what docs/deployment.md pins, and what the SEAPATH
# Debian images carry.
FROM python:3.11-slim AS builder

COPY requirements.txt .

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# The SEAPATH collection, built from the upstream repository by its own
# prepare.sh. Nothing is patched and no role is rewritten: what this image runs
# is what the SEAPATH CI tests.
FROM python:3.11-slim AS collection

ARG SEAPATH_ANSIBLE_REPOSITORY=https://github.com/seapath/ansible.git
# `seapathalloc` rather than `main`: two catalogue entries,
# `seapath_setup_prometheus_exporters` and `seapath_setup_deploy_seapath_alloc`,
# name playbooks that only exist on that branch. An image built from `main`
# reports both unavailable, which is correct and useless to a site that needs
# them. Override at build time for a site pinned elsewhere.
ARG SEAPATH_ANSIBLE_REF=seapathalloc

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# The head of that branch, resolved by the caller with `git ls-remote`. It is
# what makes this layer's cache key move when the branch moves: without it the
# clone command is identical from one build to the next, and a local build
# after a push happily reuses the collection of the previous one. It is also
# checked: a branch that moved between the resolution and the clone fails the
# build rather than shipping code nobody asked for.
ARG SEAPATH_ANSIBLE_COMMIT=

RUN git clone --branch "${SEAPATH_ANSIBLE_REF}" --depth 1 \
        "${SEAPATH_ANSIBLE_REPOSITORY}" /src && \
    head="$(git -C /src rev-parse HEAD)" && \
    if [ -n "${SEAPATH_ANSIBLE_COMMIT}" ] && \
       [ "${head}" != "${SEAPATH_ANSIBLE_COMMIT}" ]; then \
        echo "${SEAPATH_ANSIBLE_REF} is at ${head}, the build asked for ${SEAPATH_ANSIBLE_COMMIT}" >&2; \
        exit 1; \
    fi

WORKDIR /src
# Three attempts, because prepare.sh spends its time on galaxy.ansible.com and
# on two GitHub releases, and a gateway timeout there fails a release build
# that has nothing wrong with it. The script is safe to re-enter: it installs
# the roles with `--force`, the collections it already has are left alone, and
# the submodule update and the two downloads redo themselves.
RUN set -eu; \
    for attempt in 1 2 3; do \
        if ./prepare.sh; then \
            exit 0; \
        fi; \
        echo "prepare.sh failed on attempt ${attempt}" >&2; \
        sleep 30; \
    done; \
    exit 1

# prepare.sh installs the local collection before it updates the git submodules
# and fetches the Cockpit plugins, so the collection it installed is missing the
# submodule contents. Installing it again, after those steps, brings them in.
RUN ansible-galaxy collection install --collections-path=/src/collections --force . && \
    mkdir -p /opt/ansible && \
    cp -a /src/collections /opt/ansible/collections

# Restore the two Cockpit plugin archives. `build_ignore` in galaxy.yml lists
# "*.tar.gz", and ansible-galaxy matches those patterns against the whole
# relative path, so the pattern strips
# roles/deploy_cockpit_plugins/files/*.tar.gz as well as any archive at the
# root. `deploy_cockpit_plugins` unarchives exactly those two files, and
# `seapath_setup_main.yaml` imports it on every distribution except Yocto, so
# without this the commissioning run fails on a machine that has Cockpit, which
# is every machine installed from the SEAPATH ISO. With any_errors_fatal it
# takes the whole run down with it.
#
# This restores files the packaging step dropped. It changes no role and no
# behaviour, and it goes away when galaxy.yml narrows the pattern.
RUN set -eu; \
    target=/opt/ansible/collections/ansible_collections/seapath/ansible/roles/deploy_cockpit_plugins/files; \
    mkdir -p "${target}"; \
    cp /src/roles/deploy_cockpit_plugins/files/*.tar.gz "${target}/"; \
    ls -l "${target}"

RUN ansible-galaxy collection list --collections-path=/opt/ansible/collections


FROM python:3.11-slim

# Four tools, each earning its place:
#   git                     the inventory repository, which is the audit trail
#   openssh-client          the configuration plane, which reaches every node
#                           over SSH including the local one
#   iproute2                the one hardware reading that is not a file under
#                           /proc or /sys: sysfs carries no IPv4 address
#   rsync                   ansible.posix.synchronize runs it on the controller
#                           as well as on the target, and four roles push files
#                           with it: configure_physical_machine, which
#                           seapath_setup_main imports, snmp, deploy_vm_manager
#                           and deploy_seapath_alloc. Without it the task fails
#                           on every host with "Failed to find required
#                           executable rsync", naming the container's PATH
#
# No `systemd` and no `chrony`. This image held both so it could ask the host
# for unit states, the journal and the clock offset, which is live state that
# prometheus-node-exporter already publishes on every node. Reading it here
# needed a route from the container to the host's systemd, and that route is
# what the quadlet paid for. See docs/deployment.md.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        iproute2 \
        libpam-modules \
        libpam0g \
        openssh-client \
        rsync \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=collection /opt/ansible/collections /opt/ansible/collections
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1


# Reported by GET /api/v1/node, and recorded next to the inventory commit on
# every run. A deployment is reproducible from that pair, so it has to be
# stamped at build time rather than guessed at.
ARG COLLECTION_VERSION=unknown
ENV SEAPATH_WEBUI_COLLECTION_VERSION=${COLLECTION_VERSION} \
    SEAPATH_WEBUI_COLLECTIONS_PATH=/opt/ansible/collections

# M2 adds the runtime plane: libvirt0, the vm_manager package and the Ceph
# client libraries. Absent here because M1 runs no VM operation, and an unused
# dependency tree in an image is only its CVEs.

COPY packaging/pam/seapath-webui /etc/pam.d/seapath-webui

# The host's accounts, reached through the read only /etc the quadlet mounts at
# /run/host/etc. Symlinks rather than a bind mount of each file, because
# `usermod` and `passwd` write a new file and rename it over the old one: a bind
# mount pins the inode the container started with, so an operator added to
# seapath-admin, or a password just changed, would not be seen until this
# service is restarted. A symlink is resolved at every open.
#
# Nothing else in the image reads these, and if the mount is missing they are
# dangling: the service says so at startup, because the only other symptom is
# every password being refused.
RUN ln -sf /run/host/etc/passwd /etc/passwd && \
    ln -sf /run/host/etc/group /etc/group && \
    ln -sf /run/host/etc/shadow /etc/shadow

WORKDIR /app
COPY app ./app

EXPOSE 8006

# Exec form so the service is PID 1 and receives the SIGTERM from `podman
# stop`. `python -m app` rather than a uvicorn command line because the TLS
# material has to exist, and its fingerprint has to reach the console, before
# the listening socket opens.
CMD ["python", "-m", "app"]
