#!/bin/bash
# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0
#
# Build the image, check that it answers, then publish it.
#
# The image must be released in step with SEAPATH: it carries the collection
# that decides which playbooks exist and what they do, so an image newer than
# the machines is how a playbook meets a host it was not written for.

set -euo pipefail

REGISTRY_USER="${REGISTRY_USER:-insatomcat}"
IMAGE_NAME="seapath-webui"
# Read from the source rather than repeated here, because the quadlet pins this
# exact tag and a test holds the two together.
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' app/__init__.py)}"
# The branch of seapath/ansible the collection comes from, and the label the
# service reports for it. `galaxy.yml` says 2.0.0 on every branch, so the branch
# is the part of the label that says which code the machines get.
SEAPATH_ANSIBLE_REF="${SEAPATH_ANSIBLE_REF:-seapathalloc}"
COLLECTION_VERSION="${COLLECTION_VERSION:-${SEAPATH_ANSIBLE_REF}}"
IMAGE="${REGISTRY_USER}/${IMAGE_NAME}"

echo "Building ${IMAGE}:${VERSION} from seapath/ansible ${SEAPATH_ANSIBLE_REF}"
podman build \
    --build-arg "SEAPATH_ANSIBLE_REF=${SEAPATH_ANSIBLE_REF}" \
    --build-arg "COLLECTION_VERSION=${COLLECTION_VERSION}" \
    -t "${IMAGE}:${VERSION}" .
podman tag "${IMAGE}:${VERSION}" "${IMAGE}:latest"

echo "Smoke testing the image"
# One host mount and no TLS material: this only proves the process starts and
# answers. Anything about a real machine is validated on one, and written up in
# docs/validation.md.
#
# The mount is the read only /etc the quadlet also gives the container. The
# image symlinks /etc/passwd, /etc/group and /etc/shadow into it, so without it
# the image would be smoke tested in a state no deployment ever has.
container=$(podman run -d --rm -p 18006:8006 \
    -v /etc:/run/host/etc:ro \
    -e SEAPATH_WEBUI_STATE_DIR=/tmp/state \
    -e SEAPATH_WEBUI_PORT=8006 \
    "${IMAGE}:${VERSION}")
trap 'podman stop "${container}" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 30); do
    if curl -ksf https://localhost:18006/healthz >/dev/null; then
        echo "The service answers on /healthz"
        break
    fi
    sleep 1
done
curl -ksf https://localhost:18006/healthz || {
    echo "The image does not answer on /healthz" >&2
    podman logs "${container}" >&2
    exit 1
}

podman stop "${container}" >/dev/null
trap - EXIT

echo "Logging in to docker.io"
podman login docker.io

echo "Pushing"
podman push "${IMAGE}:${VERSION}"
podman push "${IMAGE}:latest"

echo "Published docker.io/${IMAGE}:${VERSION}"
