# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which versions of this service's image a registry holds.

The third half of [D23](../../docs/decisions.md), and the smallest one. The
image a machine runs is `seapath_webui_image` in the inventory, and an operator
who wants a newer one has to know a newer one exists. That question has an
answer nobody should have to type: the registry the reference already names
lists its tags.

What this module does, and the boundary it stays inside: it asks, over HTTPS,
and it returns tags. It pins nothing, applies nothing and reaches no machine.
Pinning is a commit in the inventory and applying is a run, exactly as before,
which is why finding a version and installing it are two separate acts here.

A substation hypervisor may have no route to a registry at all, and that is a
supported configuration rather than a failure of this service. An unreachable
registry produces a sentence an operator can read, and every other page keeps
working.

The registry API is the OCI distribution one, `GET /v2/<name>/tags/list`, with
the anonymous bearer token dance a public registry answers a 401 with. That is
what Docker Hub speaks, and also what ghcr.io and quay.io speak, so a site
hosting its own images is asked the same way.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Protocol

# Short, because this runs inside a request an operator is waiting on, and a
# node with no route to the internet has to fail before they wonder.
DEFAULT_TIMEOUT = 5.0

# How many pages of tags are read before the answer is good enough. A registry
# paginates with a Link header, and a repository of this service has tens of
# tags, not thousands.
_MAX_PAGES = 10
_PAGE_SIZE = 100

# Docker Hub is spelled `docker.io` in a reference and served from another name
# entirely. Every other registry is reached at the name the reference carries.
_HUB = "docker.io"
_HUB_REGISTRY = "registry-1.docker.io"

# The tag grammar of the distribution specification. What is written into the
# inventory has to be a reference the machine can pull.
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

# A tag that carries a version, which is the only kind that can be ordered.
# `latest` names no version, and `0.3.26-rc1` is not a release this service
# would pin a substation hypervisor to on its own.
#
# At least one dot, which is what tells a version from a commit. The image
# workflow pushes the short sha of every build as a tag of its own, and a short
# sha is seven hexadecimal characters that are sometimes all digits: `5582936`
# read as a version outranks every release this service will ever cut, and the
# page offered to pin a substation's management UI to a commit and called it
# newer. Roughly one build in twenty seven lands on such a sha, so this was a
# question of when.
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")

# A repository, without its tag or digest. Deliberately narrower than the
# specification: this string is spliced into a URL, so what it may contain is
# decided here rather than by whatever the inventory happens to hold. The
# lookahead refuses `.` and `..` as a segment, which a registry path has no use
# for and a URL does.
_REPOSITORY = re.compile(
    r"^(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._-]+)*$"
)


class RegistryUnreachable(Exception):
    """The registry could not be asked, with the reason as a sentence."""


class TagSource(Protocol):
    """Everything this service needs from a registry, which is one question."""

    def tags(self, repository: str) -> list[str]:
        """Every tag the repository holds, in no particular order.

        Raises RegistryUnreachable when the question could not be put.
        """


def repository_of(reference: str) -> str | None:
    """The repository part of an image reference, or None when there is none.

    A digest reference names an exact image and no repository this can ask
    about usefully: what the operator pinned is what they get, and there is no
    "newer" of it.
    """
    reference = reference.strip()
    if not reference or "@" in reference:
        return None
    name, separator, tag = reference.rpartition(":")
    # A colon before the last slash is a registry port, so the reference
    # carries no tag and the whole of it is the repository.
    repository = reference if not separator or "/" in tag else name
    return repository if _REPOSITORY.match(repository) else None


def is_version(tag: str) -> bool:
    return bool(_VERSION.match(tag))


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.split("."))


def newest(tags: Iterable[str]) -> str | None:
    """The highest version among these tags, ignoring the ones that are not.

    Numeric comparison, component by component, so 0.3.10 comes after 0.3.9.
    """
    versions = [tag for tag in tags if is_version(tag)]
    if not versions:
        return None
    return max(versions, key=version_key)


class RegistryTagSource:
    """The tags of a repository, read from the registry that serves it."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    def tags(self, repository: str) -> list[str]:
        registry, path = _split(repository)
        url = (
            f"https://{registry}/v2/{urllib.parse.quote(path)}"
            f"/tags/list?n={_PAGE_SIZE}"
        )
        token: str | None = None
        found: list[str] = []
        for _ in range(_MAX_PAGES):
            payload, link, token = self._page(url, repository, token)
            names = payload.get("tags")
            if isinstance(names, list):
                found.extend(str(name) for name in names)
            following = _next_page(link, registry)
            if following is None:
                break
            url = following
        return found

    def _page(
        self, url: str, repository: str, token: str | None
    ) -> tuple[dict, str | None, str | None]:
        """One page, acquiring an anonymous token if the registry asks for one.

        The token is carried from page to page: a registry that challenged once
        challenges every time, and asking for a new one per page would triple
        the requests for nothing.
        """
        try:
            return (*self._get(url, token), token)
        except urllib.error.HTTPError as error:
            if error.code != 401 or token is not None:
                raise RegistryUnreachable(
                    f"The registry answered {error.code} for {repository}."
                ) from error
            token = self._token(error.headers.get("WWW-Authenticate"), repository)
            try:
                return (*self._get(url, token), token)
            except urllib.error.HTTPError as denied:
                raise RegistryUnreachable(
                    f"The registry answered {denied.code} for {repository}, "
                    "with the token it handed out."
                ) from denied

    def _get(self, url: str, token: str | None) -> tuple[dict, str | None]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self._timeout
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                link = response.headers.get("Link")
        except urllib.error.HTTPError:
            raise
        except (OSError, urllib.error.URLError) as error:
            raise RegistryUnreachable(
                f"{urllib.parse.urlsplit(url).netloc} could not be reached: "
                f"{getattr(error, 'reason', error)}."
            ) from error
        except (ValueError, UnicodeDecodeError) as error:
            raise RegistryUnreachable(
                "The registry answered something that is not a tag list."
            ) from error
        if not isinstance(payload, dict):
            raise RegistryUnreachable(
                "The registry answered something that is not a tag list."
            )
        return payload, link

    def _token(self, challenge: str | None, repository: str) -> str:
        """The anonymous pull token a public registry hands to anyone.

        The realm and the scope come from the challenge rather than from a
        table of registries, which is what makes a site's own registry work
        without this module knowing its name.
        """
        parameters = _challenge(challenge)
        realm = parameters.get("realm")
        if not realm or not realm.startswith("https://"):
            raise RegistryUnreachable(
                f"{repository} needs credentials this service does not hold."
            )
        query = {
            name: parameters[name]
            for name in ("service", "scope")
            if parameters.get(name)
        }
        url = f"{realm}?{urllib.parse.urlencode(query)}" if query else realm
        payload, _ = self._get(url, None)
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryUnreachable(
                f"{repository} needs credentials this service does not hold."
            )
        return token


class FakeTagSource:
    """What a registry would answer, on a laptop that has no route to one."""

    def __init__(self, tags: list[str] | None = None) -> None:
        self.tags_returned = tags if tags is not None else []
        self.asked: list[str] = []
        self.failure: str | None = None

    def tags(self, repository: str) -> list[str]:
        self.asked.append(repository)
        if self.failure is not None:
            raise RegistryUnreachable(self.failure)
        return list(self.tags_returned)


def _split(repository: str) -> tuple[str, str]:
    """The registry to ask, and the path to ask it about.

    A reference with no registry at all is Docker Hub's, and a single segment
    there is an official image, which lives under `library`. Neither shape is
    what this service seeds, and both are what someone types.
    """
    if not _REPOSITORY.match(repository):
        raise RegistryUnreachable(f"{repository} is not a repository this can read.")
    head, _, rest = repository.partition("/")
    if not rest or ("." not in head and ":" not in head and head != "localhost"):
        head, rest = _HUB, repository
    if head == _HUB:
        head = _HUB_REGISTRY
        if "/" not in rest:
            rest = f"library/{rest}"
    return head, rest


def _next_page(link: str | None, registry: str) -> str | None:
    """The next page of tags, from the Link header the specification defines.

    Only a link back to the same registry is followed. The header comes from
    the network, and a redirection to somewhere else is not a page of tags.
    """
    if not link:
        return None
    for part in link.split(","):
        target, _, parameters = part.partition(";")
        if 'rel="next"' not in parameters.replace(" ", "").replace("'", '"'):
            continue
        target = target.strip().strip("<>")
        if not target:
            return None
        following = urllib.parse.urljoin(f"https://{registry}/", target)
        if urllib.parse.urlsplit(following).netloc != registry:
            return None
        return following
    return None


def _challenge(header: str | None) -> dict[str, str]:
    """The parameters of a `Bearer` challenge, as a mapping."""
    if not header:
        return {}
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return {}
    parameters: dict[str, str] = {}
    for part in rest.split(","):
        name, separator, value = part.partition("=")
        if separator:
            parameters[name.strip().lower()] = value.strip().strip('"')
    return parameters
