<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Security policy

## Reporting a vulnerability

Report privately, through GitHub's private vulnerability reporting on this
repository: the **Security** tab, then **Report a vulnerability**. That opens a
report only the maintainers can read.

If you cannot use it, open a public issue asking for a private contact and
saying nothing about the flaw itself.

Expect an acknowledgement within five working days and an assessment within
fifteen. A fix ships as a new version, listed in
[CHANGELOG.md](CHANGELOG.md), and the report is credited unless you ask
otherwise.

Please do not test against a substation. The test suite runs on a laptop
against fakes, which is where a proof of concept belongs.

## What a flaw here costs

This service runs on every SEAPATH node, and the machines are live electrical
substation hypervisors. It authenticates operators through PAM, it holds the
SSH material that lets one node drive the others, and the account it runs
playbooks under has passwordless sudo. An authenticated session is therefore
root on every machine of the cluster, and an authentication bypass is the worst
thing that can happen to it.

So the reports that matter most are, in order:

1. anything that authenticates a request that should be refused, or that lets
   one session act as another: PAM handling, sessions, CSRF, the console path.
2. anything that reads or writes outside the two places this service owns, the
   inventory repository and its own trust material under
   `/etc/seapath/webui/`. [AGENTS.md](AGENTS.md) states that boundary, and
   crossing it is a bug of the highest severity here, security or not.
3. anything that puts a secret where it can be read: a key, a token or a
   password in a log line, in an API response, in a run artefact or in a git
   commit.
4. command or template injection through an inventory value, a form field or a
   playbook argument.

## What is working as designed

- The console gives a shell on the `ansible` account, which has passwordless
  sudo. That is the point of the button, it asks for an administrator first,
  and [docs/decisions.md](docs/decisions.md) records it.
- The TLS certificate generated at first boot is self signed. A site replaces
  it with its own material.
- `SEAPATH_WEBUI_USE_FAKES=1` serves invented readings. It is a development
  switch, it says so in the log, and a deployed node runs without it.

## Supported versions

The published version, meaning the highest `X.Y.Z` tag on the image, is the
only one that gets a fix. The service updates in place, as an inventory
variable an apply carries, which
[docs/deployment.md](docs/deployment.md) describes.
