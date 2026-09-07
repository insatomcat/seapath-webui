<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Contributing to seapath-webui

Read [SPEC.md](SPEC.md) for what this service is, and
[AGENTS.md](AGENTS.md) for how work is done here. This file is the short
version of what a change has to satisfy to be merged.

## Before you write code

**This service never configures a machine directly.** It writes the inventory
repository and its own trust material, and everything else on a host is changed
by an Ansible run through the upstream roles, unchanged. A patch that renders
`corosync.conf`, calls `cephadm` or restarts a host service from Python will be
refused, however well it works. The answer to that need is a variable in the
inventory and a playbook. [AGENTS.md](AGENTS.md) states the one bounded
exception and why it exists.

The target machines are live electrical substation hypervisors, and an apply
can restart services under running VMs. That is the reason the boundary above
is enforced rather than encouraged.

Open an issue before a large change, so the design discussion happens before
the work. Small fixes can go straight to a pull request.

## Setting up

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The suite runs on a laptop with no cluster, no libvirt and no container. Keep
it that way: everything that touches a host goes through an adapter that has a
fake. To browse the UI without a SEAPATH machine, set
`SEAPATH_WEBUI_USE_FAKES=1`.

## What a change has to carry

1. **Tests.** New behaviour comes with tests against the fakes. The specific
   obligations for the inventory, the validation rules, the trust material and
   the runs are listed under "Tests" in [AGENTS.md](AGENTS.md), and they are
   requirements rather than examples.
2. **Green suite.** `pytest`, `ruff check app tests` and `black --check app
   tests` all pass. CI runs the three on every pull request.
3. **Coverage.** The suite holds above 90% of statements and above 80% of
   branches, and CI fails below either floor. A change that lowers coverage
   needs the tests that keep it up.
4. **Documentation.** An endpoint is documented in [docs/api.md](docs/api.md)
   and visible in OpenAPI. A decision with alternatives goes in
   [docs/decisions.md](docs/decisions.md) as a numbered entry. Anything that
   can only be checked on real hardware goes in
   [docs/validation.md](docs/validation.md) as a numbered check.
5. **SPDX header.** `Apache-2.0` for code, `CC-BY-4.0` for documents, on every
   new file.

The full definition of done for a milestone, including the acceptance criterion
that matters, is at the end of [AGENTS.md](AGENTS.md).

## Style

- English for code, comments, documents, commit messages and UI strings.
- `black` and `ruff` defaults, type hints on public functions.
- Comments explain why. A comment restating the line above it is noise.
- **Never use the em-dash character.** Use a hyphen, a colon, parentheses, or
  two sentences.

## Commits and pull requests

Every commit carries a `Signed-off-by` line, which certifies the
[Developer Certificate of Origin](https://developercertificate.org/). Use
`git commit -s`.

The subject line is under 72 characters and says what the change does in the
present tense, in the voice of the thing that changed:
`fix: a placement says which node, and never the one it is already on`. The
prefixes in use are `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:` and
`release:`. The body is wrapped at 72 columns and says why.

Do not add an attribution trailer beyond `Signed-off-by`.

One pull request does one thing. A branch that fixes a bug and reformats a
module is two pull requests.

## Releases

Releases are cut by the maintainers. Each shipped change gets its own version,
bumped in `app/__init__.py`, `seapath-webui.container` and the tag quoted in
[docs/deployment.md](docs/deployment.md), in a commit named `release: X.Y.Z`,
and recorded in [CHANGELOG.md](CHANGELOG.md). The version is the identity of a
build, which is how a machine says which code is answering on it, so a number
is used once.

## Reporting a vulnerability

Privately, and [SECURITY.md](SECURITY.md) says how.
