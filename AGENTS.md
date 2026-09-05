<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Working agreement for seapath-webui

Read [SPEC.md](SPEC.md) first, then [docs/inventory.md](docs/inventory.md). This
file is only about how to work here.

## The rule that overrides everything

**This service never configures a machine directly.** It writes two things: the
inventory repository, and the trust material, meaning its own keys and
certificates under `/etc/seapath/webui/` plus the `authorized_keys` of the
`ansible` account. Everything else on a host is changed by an Ansible run,
through the upstream roles, unchanged.

There is exactly one exception, and it is bounded: at first boot the service
appends its own key to the `ansible` account's `authorized_keys`, provisioning
the trust it needs to reach even its own machine. Without it nothing can
converge at all, not even a standalone node. Append, never rewrite: that file
arrives from the ISO carrying the site key, and clobbering it locks out any
conventional Ansible control machine. Do not grow this exception. Anything else
that tempts you to write to a host is a variable in the inventory and a
playbook.

If you find yourself writing `/etc/corosync/corosync.conf`, calling
`corosync-keygen`, running `cephadm`, or restarting a host service from Python,
stop. The answer is a variable in the inventory and a playbook. That is not a
stylistic preference: it is the property that makes SEAPATH infrastructure as
code, and it is why the roles you would be duplicating are the ones the CI
tests.

## Consequences to keep in mind

- The target machines are live electrical substation hypervisors. An apply can
  restart services under running VMs. Confirmations name the impacted machines.
- Latency is the product. `isolcpus` and the tuning variables are edited in the
  inventory like any other, and the ceremony sits where a machine actually
  changes: the apply confirmation names the disruption and the machines. The
  service itself stays on housekeeping CPUs.
- The inventory is the audit trail. Every commit carries the authenticated user
  and a generated message. Never write to the repository outside the commit
  path.
- Never log a secret. The corosync authkey is handled entirely by the role over
  the SSH mesh and must never pass through this service.

## Stack and layout

Python 3.11, FastAPI, uvicorn, Jinja2 templates, vanilla JS, `ansible-runner`.
No Node build step in production, same as `rtperfui` and `claudewebui`.

```
app/
  __main__.py        entry point: TLS material, console banner, then uvicorn
  main.py            app factory, router mounting, lifespan
  core/              settings, logging, errors, auth, sessions, security, tls
  hosts/             the host adapters and their fakes
  cluster/           what the other machines publish: the exporter fan-out and
                     its parsers, for the CPU pool, the tuning, Pacemaker, Ceph
  console/           the shell this node serves over its own SSH path
  inventory/         git repository, schema, validation, discovery, forms
  trust/             invitations, CSR signing, SSH key provisioning, revocation
  runs/              ansible-runner driver, event stream, artefacts
  services/          node.py, realtime.py, cluster.py, storage.py, vms.py
  api/v1/            routers, one module per resource
  ui/                Jinja templates and static assets
packaging/           the PAM service file the image ships
tests/               pytest, fakes, fixtures
Dockerfile
seapath-webui.container
requirements.txt
```

Host access is confined to two adapters, both under `app/hosts/`: an SSH and
`ansible-runner` adapter for everything that changes a machine, and a read only
adapter describing what the machine **is**, which is what the seed inventory is
written from and what the node view reports. What a machine is *doing* is not
read here at all: every node runs `prometheus-node-exporter`, and D13 in
[decisions.md](docs/decisions.md) records why that boundary is worth
defending. Both adapters have a fake
implementation, and the whole test suite runs against the fakes, on a laptop,
with no cluster and no libvirt.

Two rules keep that promise true rather than aspirational. The read only
adapter takes its filesystem root as a parameter, so its parsers are exercised
against a recorded `/proc` and `/sys` tree rather than the developer's machine.
And it never shells out directly: commands go through an injected runner, which
keeps the list of commands the service may run short and reviewable in one
place, and lets a test replay recorded output.

## Style

- English for code, comments, docs, commit messages and UI strings.
- **Never use the em-dash character.** Use a hyphen, a colon, parentheses, or
  two sentences.
- SPDX headers everywhere: `Apache-2.0` for code, `CC-BY-4.0` for docs.
- Comments explain why, not what. No TODO owners, no reference to uncommitted
  work, no hint that an AI wrote the code.
- `black` and `ruff` defaults, type hints on public functions.

## Tests

- Inventory: golden file tests on the generated YAML for the reference
  topologies, and a test asserting `ansible-inventory --list` parses it. The
  generated inventory must be equivalent to the hand written example, because
  that equivalence is the product claim.
- Validation: one test per rule, both the accepting and the refusing case.
- Trust: the generated `authorized_keys` line byte for byte, fingerprint
  mismatch, expired and replayed tokens, and revocation removing exactly the
  intended keys. One test must start from an `authorized_keys` containing the
  ISO's site key and assert it is still there, untouched, after provisioning and
  after revocation.
- Runs: the exact `ansible-runner` invocation for each exposed playbook, and the
  mapping from Ansible events to the progress model, including a run that is
  interrupted without a final status.

## Definition of done for a milestone

1. Endpoints implemented and documented in `docs/api.md`, visible in OpenAPI.
2. UI screens usable without reading the API docs, failure paths included.
3. `pytest` green against the fakes.
4. Manual validation on a real machine written up in `docs/validation.md`.
5. **The acceptance criterion that matters**: export the inventory the UI
   produced, run the same playbooks from a conventional Ansible control machine,
   and observe no change. If that passes, infrastructure as code survived. If it
   does not, something in the service configured a machine behind Ansible's
   back, and that is a bug of the highest severity here.

## Do not

- Do not reimplement a role, a template, or `vm_manager` logic.
- Do not add a database. The inventory is git, the cluster state is corosync,
  Pacemaker and Ceph, and the run history is files.
- Do not request `--privileged`, the podman socket, or `--pid=host` in the
  quadlet. Needing one of those means the design was violated somewhere above.
