# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading a tag list from a registry, without a registry.

The Deployment page asks one question off the machine, and this is the code
that asks it. What is worth testing is the shape of the conversation: which
host a reference resolves to, the anonymous token a public registry hands out
before it answers, the pages the answer may arrive in, and the fact that every
way of failing produces a sentence rather than an exception reaching a page.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest

from app.services import registry
from app.services.registry import (
    RegistryTagSource,
    RegistryUnreachable,
    is_version,
    newest,
    repository_of,
)


class _Response(io.BytesIO):
    """What `urlopen` returns, with the two things this code reads."""

    def __init__(self, payload: dict, link: str | None = None) -> None:
        super().__init__(json.dumps(payload).encode())
        self.headers = email.message.Message()
        if link:
            self.headers["Link"] = link

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _challenge_error(url: str, challenge: str) -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["WWW-Authenticate"] = challenge
    return urllib.error.HTTPError(url, 401, "Unauthorized", headers, None)


class _Registry:
    """A registry that records what it was asked, and answers a script."""

    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.asked: list[tuple[str, str | None]] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.asked.append((request.full_url, request.get_header("Authorization")))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (
            "docker.io/insatomcat/seapath-webui:0.3.26",
            "docker.io/insatomcat/seapath-webui",
        ),
        # A registry port is a colon too, and what follows it is not a tag.
        (
            "registry.substation.local:5000/seapath-webui",
            "registry.substation.local:5000/seapath-webui",
        ),
        # A digest names an exact image, and there is no newer one of it.
        ("docker.io/insatomcat/seapath-webui@sha256:" + "a" * 64, None),
        ("", None),
    ],
)
def test_the_repository_is_read_out_of_the_reference(
    reference: str, expected: str | None
) -> None:
    assert repository_of(reference) == expected


def test_versions_are_ordered_by_number_and_the_rest_is_ignored() -> None:
    tags = ["latest", "main", "0.3.9", "0.3.10", "0.4.0-rc1"]

    assert newest(tags) == "0.3.10"


def test_a_repository_holding_no_version_has_no_newest() -> None:
    assert newest(["latest", "stable"]) is None


def test_a_commit_sha_that_happens_to_be_all_digits_is_not_a_version() -> None:
    """The workflow tags every build with its short sha, and some are numeric.

    `5582936` was one, and read as a version it outranks every release this
    service will ever cut: the page offered to pin a substation's management UI
    to a commit and called it newer than the release the inventory named. A
    version carries at least one dot, which a seven character sha never does.
    """
    assert not is_version("5582936")
    assert not is_version("1234567")
    assert newest(["0.3.58", "0.3.60", "5582936", "latest", "e514f5b"]) == "0.3.60"


def test_a_version_is_still_a_version() -> None:
    for tag in ("0.3.60", "1.0", "2.0.0", "0.3.10"):
        assert is_version(tag), tag


def test_docker_hub_is_asked_at_the_host_that_serves_it(monkeypatch) -> None:
    # A reference spells it `docker.io` and the registry answers at another
    # name entirely.
    hub = _Registry([_Response({"tags": ["0.3.26"]})])
    monkeypatch.setattr(registry.urllib.request, "urlopen", hub)

    assert RegistryTagSource().tags("docker.io/insatomcat/seapath-webui") == ["0.3.26"]
    assert hub.asked[0][0].startswith(
        "https://registry-1.docker.io/v2/insatomcat/seapath-webui/tags/list"
    )


def test_the_anonymous_token_a_public_registry_asks_for_is_acquired(
    monkeypatch,
) -> None:
    # The realm and the scope come from the challenge rather than from a table
    # of registries, which is what makes a site's own registry work.
    url = "https://registry-1.docker.io/v2/insatomcat/seapath-webui/tags/list"
    hub = _Registry(
        [
            _challenge_error(
                url,
                'Bearer realm="https://auth.docker.io/token",'
                'service="registry.docker.io",'
                'scope="repository:insatomcat/seapath-webui:pull"',
            ),
            _Response({"token": "handed-out"}),
            _Response({"tags": ["0.3.26", "0.3.27"]}),
        ]
    )
    monkeypatch.setattr(registry.urllib.request, "urlopen", hub)

    tags = RegistryTagSource().tags("docker.io/insatomcat/seapath-webui")

    assert tags == ["0.3.26", "0.3.27"]
    token_url = hub.asked[1][0]
    assert token_url.startswith("https://auth.docker.io/token?")
    assert "scope=repository%3Ainsatomcat%2Fseapath-webui%3Apull" in token_url
    assert hub.asked[2][1] == "Bearer handed-out"


def test_a_registry_needing_real_credentials_says_so(monkeypatch) -> None:
    # A private repository is a supported situation and an unsupported answer:
    # this service holds no registry credentials.
    url = "https://registry.substation.local/v2/seapath-webui/tags/list"
    private = _Registry([_challenge_error(url, 'Basic realm="registry"')])
    monkeypatch.setattr(registry.urllib.request, "urlopen", private)

    with pytest.raises(RegistryUnreachable, match="credentials"):
        RegistryTagSource().tags("registry.substation.local/seapath-webui")


def test_the_pages_of_a_long_tag_list_are_followed(monkeypatch) -> None:
    paged = _Registry(
        [
            _Response(
                {"tags": ["0.3.0"]},
                '</v2/insatomcat/seapath-webui/tags/list?n=100&last=0.3.0>; rel="next"',
            ),
            _Response({"tags": ["0.3.1"]}),
        ]
    )
    monkeypatch.setattr(registry.urllib.request, "urlopen", paged)

    assert RegistryTagSource().tags("docker.io/insatomcat/seapath-webui") == [
        "0.3.0",
        "0.3.1",
    ]
    assert paged.asked[1][0].endswith("last=0.3.0")


def test_a_next_page_pointing_somewhere_else_is_not_followed(monkeypatch) -> None:
    # The header comes from the network. A redirection to another host is not
    # a page of tags.
    elsewhere = _Registry(
        [_Response({"tags": ["0.3.0"]}, '<https://elsewhere.invalid/v2/x>; rel="next"')]
    )
    monkeypatch.setattr(registry.urllib.request, "urlopen", elsewhere)

    assert RegistryTagSource().tags("docker.io/insatomcat/seapath-webui") == ["0.3.0"]
    assert len(elsewhere.asked) == 1


def test_a_node_with_no_route_gets_a_sentence(monkeypatch) -> None:
    # The ordinary case in a substation, and the page has to keep working.
    unreachable = _Registry([urllib.error.URLError("timed out")])
    monkeypatch.setattr(registry.urllib.request, "urlopen", unreachable)

    with pytest.raises(RegistryUnreachable, match="registry-1.docker.io"):
        RegistryTagSource().tags("docker.io/insatomcat/seapath-webui")


def test_an_answer_that_is_not_a_tag_list_is_refused(monkeypatch) -> None:
    nonsense = _Registry([_Response(["not", "a", "mapping"])])
    monkeypatch.setattr(registry.urllib.request, "urlopen", nonsense)

    with pytest.raises(RegistryUnreachable, match="not a tag list"):
        RegistryTagSource().tags("docker.io/insatomcat/seapath-webui")


def test_a_repository_this_cannot_read_never_reaches_the_network(monkeypatch) -> None:
    # What goes into the URL comes out of the inventory, so what it may hold is
    # decided here.
    forbidden = _Registry([])
    monkeypatch.setattr(registry.urllib.request, "urlopen", forbidden)

    with pytest.raises(RegistryUnreachable):
        RegistryTagSource().tags("docker.io/../../etc/passwd")
    assert forbidden.asked == []
