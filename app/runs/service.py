# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Launching runs, and being honest about how they end."""

from __future__ import annotations

import logging
import re
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.errors import ApiError
from app.core.logging import audit_event
from app.hosts.local import parse_cpu_list
from app.inventory.model import Inventory, Mode
from app.inventory.service import InventoryService, InventoryState
from app.runs import actions, catalogue, cyclictest, hwlatdetect, progress, staging
from app.runs import scope as scoping
from app.runs.adapter import RunAdapter, RunRequest, build_command
from app.runs.catalogue import (
    PlaybookEntry,
    Precondition,
    VariableSpec,
    VariableType,
)
from app.runs.models import RunProgress, RunRecord, RunState
from app.runs.scope import RunScope, Scope, ScopeRefused
from app.runs.store import RunLocked, RunStore
from app.trust import known_hosts
from app.trust.authorized_keys import MissingAccount
from app.trust.service import TrustService

logger = logging.getLogger(__name__)

# `ansible-playbook` colours its errors, and a colour code in the middle of a
# message rendered as text is noise an operator has to read around.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class PlaybookAvailability(BaseModel):
    """A catalogue entry, and whether this node can run it right now."""

    entry: PlaybookEntry
    available: bool
    unmet: list[str] = []
    # The codes behind those sentences. A page showing thirteen entries that
    # are all unavailable for the same reason has to be able to say the reason
    # once, and it cannot do that by comparing thirteen sentences that each
    # name a different playbook.
    unmet_codes: list[str] = []
    # What the default scope resolves to against the current inventory: the
    # machines this entry would play, and the guests it leaves out. Computed
    # here rather than in the browser because the answer is Ansible's, read
    # from the file's groups, and the confirmation that names the machines has
    # to name the same ones the run will play.
    machines: list[str] | None = None
    excluded: list[str] = []


class RunPaths(BaseModel):
    # A callable, or the path itself in a test. Resolved at each access rather
    # than held, so a collection installed on the node takes effect on the next
    # run without a restart. Never during a run: installing takes the run lock,
    # and a mirror already staged is symlinks into the tree it would replace.
    collections_root: Callable[[], Path] | Path
    private_key_file: Path
    known_hosts_file: Path
    # Where the keys are declared for the ssh commands a run spawns itself,
    # which `ansible.posix.synchronize` is the reason to care about.
    ssh_config_file: Path
    # Resolved at launch rather than held: an operator can add or remove the
    # site key between two runs, and a run must use what is installed now.
    extra_key_files: Callable[[], tuple[Path, ...]] = tuple

    @property
    def collections_path(self) -> Path:
        root = self.collections_root
        return root() if callable(root) else root


class RunService:
    def __init__(
        self,
        store: RunStore,
        adapter: RunAdapter,
        inventory: InventoryService,
        trust: TrustService,
        paths: RunPaths,
        hostname: str,
        collection_version: str = "unknown",
        node_distribution: Callable[[], str | None] = lambda: None,
        seed_builder: Callable[[], str | None] = lambda: shutil.which(
            catalogue.SEED_TOOL
        ),
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._inventory = inventory
        self._trust = trust
        self._paths = paths
        self._hostname = hostname
        # What the image was built with. The collection actually installed is
        # read from disk at each launch, because a node can be pointed at
        # another one and a branch does not change the version in galaxy.yml.
        self._configured_version = collection_version
        # Which of the five SEAPATH distributions this machine runs, read from
        # /etc/os-release through the host adapter. A callable rather than a
        # value: the reader is the thing with a fake, and a service that cached
        # this at startup would keep answering Debian after a reinstall.
        self._node_distribution = node_distribution
        # Whether this node can build a guest's cloud-init seed, which is a
        # `cloud-localds` on the PATH of this container rather than anything
        # read off a machine. A callable for the same reason as the
        # distribution: the suite runs on a laptop that has no such tool, and
        # a precondition nobody can fake is a precondition nobody tests.
        self._seed_builder = seed_builder
        self._cancelled: set[str] = set()

    # Catalogue

    def entries(self) -> tuple[PlaybookEntry, ...]:
        """The catalogue merged with what the installed collection holds.

        Resolved on every call and cached on the collection's fingerprint, so
        a collection reinstalled under a running service is read again and a
        page reload is not a walk of a few hundred YAML files.
        """
        return catalogue.resolve(
            self._paths.collections_path, self.collection_version()
        )

    def playbooks(self) -> list[PlaybookAvailability]:
        unmet_by_condition = self._unmet_preconditions()
        missing = self._missing_playbooks()
        # Parsed once for the whole catalogue: every entry asks the same file
        # the same question, and the list is a few hundred entries long.
        table = scoping.table(self._document())
        rows = []
        for entry in self.entries():
            blocking = self._blocking(entry, unmet_by_condition, missing)
            plan = scoping.plan(entry.targets, table)
            rows.append(
                PlaybookAvailability(
                    entry=entry,
                    available=not blocking,
                    unmet=[reason for _, reason in blocking],
                    unmet_codes=[code for code, _ in blocking],
                    machines=plan.hosts,
                    excluded=plan.excluded,
                )
            )
        return rows

    def _blocking(
        self,
        entry: PlaybookEntry,
        unmet: dict[Precondition, str],
        missing: set[str],
    ) -> list[tuple[str, str]]:
        """What stops this entry, each reason paired with the code behind it."""
        reasons = [(c.value, unmet[c]) for c in entry.requires if c in unmet]

        wrong = self._wrong_distribution(entry)
        if wrong:
            reasons.append((Precondition.DISTRIBUTION_MATCHES.value, wrong))

        # A variable the reader found in the playbook and this page has no
        # field for. Derived entries only: a reviewed entry types what it
        # accepts.
        unfillable = [
            spec.name
            for spec in entry.variables
            if spec.required and spec.type is VariableType.UNKNOWN
        ]
        if unfillable:
            reasons.append(
                (
                    Precondition.VARIABLES_SUPPORTED.value,
                    (
                        f"{entry.id} refuses to start without "
                        f"{', '.join(unfillable)}, and this page has no field "
                        "for it. Run it from a control machine, or give it a "
                        "reviewed catalogue entry that says what the variable "
                        "may hold."
                    ),
                )
            )

        if entry.id in missing:
            reasons.insert(
                0,
                (
                    Precondition.PLAYBOOK_PRESENT.value,
                    f"{entry.playbook} is not in the SEAPATH collection this "
                    f"image ships (version {self.collection_version()}). The "
                    "catalogue and the collection are released separately.",
                ),
            )
        return reasons

    def _unreachable(self, state) -> list[str]:
        """The machines in the inventory this node has no way of reaching.

        Reachability here is about credentials rather than about the network:
        whether a key would be offered and whether the host key is known. The
        network answer belongs to the run, which says it host by host.
        """
        if state.inventory is None:
            return []
        others = [name for name in state.inventory.hosts if name != state.this_host]
        if not others:
            return []
        if not self._paths.extra_key_files():
            return others

        known = known_hosts.read_peers(self._paths.known_hosts_file)
        return [
            name
            for name in others
            if state.inventory.hosts[name].ansible_host not in known
        ]

    def _addressable_guests(self) -> list[str]:
        """The guests a run could reach, from the inventory as it stands now."""
        state = self._inventory.state()
        return [] if state.inventory is None else _addressable_guests(state.inventory)

    def collection_version(self) -> str:
        """What a run records as the code it ran.

        Read from the installed collection rather than from the build, because
        a site running a branch installs a collection whose `galaxy.yml` says
        the same version as every other branch. The fingerprint is what tells
        two branches apart, and it survives someone reinstalling.
        """
        observed = catalogue.identity(self._paths.collections_path)
        if observed is None:
            return self._configured_version
        build = self._configured_version
        if build and build != "unknown" and build not in observed:
            # A build label the fingerprint cannot carry, such as the branch
            # the image was built from. Dropped when it repeats the version.
            return f"{observed} (build {build})"
        return observed

    def _missing_playbooks(self) -> set[str]:
        return catalogue.missing_from(self._paths.collections_path)

    def _unmet_preconditions(
        self, played: list[str] | None = None
    ) -> dict[Precondition, str]:
        """Why an entry cannot be launched right now.

        `played` narrows the reachability question to the machines a scope
        actually selected. The catalogue listing passes none, because a listing
        is about the ordinary run: every machine the playbook plays.
        """
        unmet: dict[Precondition, str] = {}
        state = self._inventory.state()

        if not state.seeded or state.inventory is None:
            unmet[Precondition.INVENTORY_VALID] = (
                "There is no inventory yet. Fill in the machine's form first."
            )
        elif not state.validation.valid:
            failing = ", ".join(f.rule for f in state.validation.errors())
            unmet[Precondition.INVENTORY_VALID] = (
                f"The inventory does not validate: {failing}."
            )

        try:
            relations = self._trust.relations(self._hostname)
        except MissingAccount as error:
            # A machine with no `ansible` account has nowhere to install the
            # trust, which is one unmet precondition among the others here.
            # Reading the catalogue is still worth answering: every entry is
            # listed, dimmed, saying this. The sentence the exception carries
            # names the account and the directory, which is what fixes it.
            unmet[Precondition.SELF_TRUST] = str(error)
        else:
            if not relations or not relations[0].installed:
                unmet[Precondition.SELF_TRUST] = (
                    "This node has no SSH trust with itself, so it cannot "
                    "converge even its own configuration."
                )

        unreachable = self._unreachable(state)
        if played is not None:
            # A narrowed run is refused by the machines it plays and by no
            # others, which is half of what narrowing is for: a node whose
            # neighbour is down still converges itself.
            unreachable = [name for name in unreachable if name in played]
        if unreachable:
            unmet[Precondition.PEER_REACHABLE] = (
                f"{', '.join(unreachable)} cannot be reached from this node. A "
                "run plays every machine the playbook names, so it would die "
                "on those. Upload the site key, and accept their host keys, in "
                "Reaching the other machines. Narrowing the run to a group or "
                "a machine that answers is the other way out."
            )

        if state.inventory is not None and not _addressable_guests(state.inventory):
            declared = len(state.inventory.guests)
            unmet[Precondition.GUEST_ADDRESSABLE] = (
                (
                    "No guest of this inventory declares an address, so none "
                    f"of the {declared} can be reached over SSH. Measuring "
                    "inside a guest logs into it the way a convergence logs "
                    "into a machine: add `ansible_host` to the guest's entry, "
                    "and install this node's public key in the account Ansible "
                    "connects as."
                )
                if declared
                else (
                    "This inventory declares no guest, so there is nothing to "
                    "measure inside. A guest is declared on the VMs page."
                )
            )

        seeded = _seeded_guests(state.inventory) if state.inventory else []
        if seeded:
            refused = self._seed_refusal(
                seeded, _malformed_seeds(state.inventory) if state.inventory else []
            )
            if refused:
                unmet[Precondition.SEED_BUILDABLE] = refused

        # Which machines the inventory has, rather than which single mode it
        # is in. A file may describe a cluster and a standalone machine at
        # once, and then both kinds of playbook have somewhere to run: asking
        # the mode would refuse one of them on a file that has the machines
        # for it.
        clustered = list(state.inventory.cluster_members) if state.inventory else []
        alone = (
            sorted(set(state.inventory.hosts) - set(clustered))
            if state.inventory
            else []
        )
        if not clustered:
            unmet[Precondition.CLUSTER] = (
                "This inventory declares no cluster machine. Add a node first."
            )
        if not alone:
            unmet[Precondition.STANDALONE] = (
                "This inventory declares no machine outside a cluster."
            )

        return unmet

    def _seed_refusal(self, seeded: list[str], malformed: list[str]) -> str | None:
        """Why a guest asking for a cloud-init seed cannot be deployed yet.

        The mapping is asked about first, then the role, then the tool: a
        mapping the role cannot read fails whatever is installed, and where the
        role is missing nothing would call the tool at all.
        """
        guests = ", ".join(seeded)
        if malformed:
            return (
                f"The `cloud_init` of {', '.join(malformed)} is not a mapping "
                "of cloud-config keys. The role reads `hostname` and the rest "
                "off it, so the deployment would stop on the guest rather than "
                "skip it. The Inventory page says the same thing about the "
                "file."
            )
        if not catalogue.role_present(
            self._paths.collections_path, catalogue.SEED_ROLE
        ):
            return (
                f"{guests} carries a `cloud_init` mapping, and the SEAPATH "
                f"collection this image ships ({self.collection_version()}) "
                f"has no `{catalogue.SEED_ROLE}` role. The deployment would "
                "create the guest with no seed disk and report success, so it "
                "would come up with whatever its image was built with, its "
                "address included."
            )
        if self._seed_builder() is None:
            return (
                f"{guests} carries a `cloud_init` mapping, so the deployment "
                f"builds a NoCloud seed image with `{catalogue.SEED_TOOL}` on "
                "the control machine, which for a run launched here is this "
                "node. It is not installed: the package is "
                f"`{catalogue.SEED_PACKAGE}`, and a control machine running "
                "the same playbook from a checkout needs it just as much."
            )
        return None

    def _wrong_distribution(self, entry: PlaybookEntry) -> str | None:
        """Whether this node rules the playbook out, and why in one sentence.

        Only the five `prerequisites` entries ask. A run plays every machine
        the inventory declares, this node among them, so a playbook that
        configures a distribution this machine does not run is wrong for at
        least this machine, and none of the five checks before it writes.

        Two silences, both deliberate. An unreadable `/etc/os-release` blocks
        nothing: refusing every one of the five because the container was
        mounted wrong is worse than the risk. And an inventory that does not
        declare this node says nothing about what a run will reach, so this
        check has no standing over it.
        """
        if entry.distribution is None:
            return None

        running = self._node_distribution()
        if running is None or running == entry.distribution:
            return None

        state = self._inventory.state()
        if state.inventory is None or state.this_host not in state.inventory.hosts:
            return None

        return (
            f"This machine runs {running}, and this playbook configures "
            f"{entry.distribution}. A run plays every machine the inventory "
            f"declares, {state.this_host} among them, and none of the five "
            "prerequisites playbooks checks the distribution it lands on. Use "
            "Configure every machine, which picks the right one per machine."
        )

    # Runs

    def list(self, limit: int = 50) -> list[RunRecord]:
        return self._store.list(limit)

    def get(self, run_id: str) -> RunRecord | None:
        return self._store.load(run_id)

    def events(self, run_id: str, offset: int = 0):
        return self._store.events(run_id, offset)

    def log(self, run_id: str) -> str:
        return self._store.log(run_id)

    def reconcile(self) -> list[RunRecord]:
        return self._store.reconcile()

    def latency_results(self, run_id: str) -> list[cyclictest.CyclictestResult]:
        """The cyclictest histograms a run fetched, parsed.

        Read from the run directory at each request rather than folded into
        the record: the record is what the run did, and this is what the run
        brought back. A file removed to reclaim space then reads as a run with
        no results, which is exactly what it is.
        """
        return cyclictest.read(self._store.results_dir(run_id))

    def interruption_results(self, run_id: str) -> list[hwlatdetect.HwlatdetectResult]:
        """The hwlatdetect reports a run fetched, parsed."""
        return hwlatdetect.read(self._store.results_dir(run_id))

    def launch(
        self,
        playbook_id: str,
        launched_by: str,
        variables: dict[str, Any] | None = None,
        check: bool = False,
        scope: RunScope | None = None,
    ) -> RunRecord:
        entries = {item.id: item for item in self.entries()}
        entry = entries.get(playbook_id)
        if entry is None:
            raise ApiError(
                "unknown_playbook",
                f"{playbook_id} is not in the catalogue.",
                404,
                {"available": sorted(entries)},
            )
        return self._launch(entry, launched_by, variables, check, scope=scope)

    def launch_action(
        self,
        action: actions.Action,
        guest: str,
        launched_by: str,
        deployment: Mode | None = None,
        host: str = "",
        node: str = "",
    ) -> RunRecord:
        """Start or stop one guest, as a run like any other.

        The play is generated rather than taken from the collection, which is
        the one exception D30 makes and its bounds: one task, one upstream
        module, one command value. Everything around it is the ordinary path,
        the lock included, so a start cannot slip in under a convergence.

        `host` names the machine when the act is one machine's: a quadlet is a
        systemd unit on each machine the inventory sends it to, so a start
        without a machine would be a start of something that has three.

        `node` is the machine an act *names*, which is a different thing: a
        move says where the resource is to go, and the play still runs on
        whichever cluster member answers. The caller has checked it against
        what the cluster reported.
        """
        # The guest's own deployment, so a Pacemaker guest is started through
        # `cluster_vm` and a libvirt one through `community.libvirt.virt`, in a
        # file that holds both.
        mode = deployment or self._mode()
        return self._launch(
            actions.entry(action, guest, mode, host, node),
            launched_by,
            variables=None,
            check=False,
            play=actions.play(action, guest, mode, host, node),
            guest=guest,
        )

    def scopes(self) -> scoping.ScopeChoices:
        """The groups and hosts a run may be narrowed to, from the inventory.

        The machines this node cannot reach travel with them, because that is
        the one precondition a narrowing lifts and the chooser is where an
        operator lifts it.
        """
        return scoping.choices(
            scoping.table(self._document()),
            self._unreachable(self._inventory.state()),
            self._addressable_guests(),
        )

    def _document(self) -> str:
        """The inventory as Ansible will read it, empty where there is none.

        A machine with no inventory yet has every entry blocked by
        INVENTORY_VALID, so an empty document here resolves to a scope with no
        machine in it rather than to an error with no sentence.
        """
        try:
            return self._inventory.raw()
        except OSError as error:
            logger.warning("Could not read the inventory to scope a run: %s", error)
            return ""

    def _scope(self, entry: PlaybookEntry, requested: RunScope | None) -> Scope:
        """What this run plays, checked against the inventory it will use.

        The default subtracts the guests from every playbook that names them,
        which is what keeps a convergence of the machines from dying on a VM
        nobody ever meant to reach with Ansible. See `app.runs.scope`.
        """
        try:
            return scoping.plan(
                entry.targets, scoping.table(self._document()), requested
            )
        except ScopeRefused as refused:
            raise ApiError(
                refused.code, refused.message, 400, refused.detail
            ) from refused

    def _mode(self) -> Mode:
        state = self._inventory.state()
        return state.inventory.mode if state.inventory else Mode.STANDALONE

    def _launch(
        self,
        entry: PlaybookEntry,
        launched_by: str,
        variables: dict[str, Any] | None = None,
        check: bool = False,
        play: str | None = None,
        guest: str | None = None,
        scope: RunScope | None = None,
    ) -> RunRecord:
        # The scope first: it decides which machines the preconditions are
        # about, and a scope naming a group the file does not declare is
        # refused before anything is locked or written.
        plan = self._scope(entry, scope)
        if entry.scope_required and not plan.requested.narrowed:
            # Named rather than widened. The one entry that carries this plays
            # every guest of the inventory, and measuring all of them at once
            # measures the contention between the measurements.
            raise ApiError(
                "scope_required",
                (
                    f"{entry.title} is launched against one guest at a time, "
                    "and this request named none. Choose the guest to measure."
                ),
                400,
                {"guests": plan.hosts or []},
            )
        blocking = self._blocking(
            entry,
            self._unmet_preconditions(plan.hosts),
            self._missing_playbooks(),
        )
        if blocking:
            # Named, never a bare 400: the operator has to know which condition
            # to satisfy.
            raise ApiError(
                "precondition_failed",
                blocking[0][1],
                409,
                {
                    "unmet": [reason for _, reason in blocking],
                    "codes": [code for code, _ in blocking],
                },
            )

        if check and not entry.previewable:
            raise ApiError(
                "not_previewable",
                (
                    f"{entry.title} is driven by commands rather than by file "
                    "templates, so check mode would report nothing meaningful."
                ),
                409,
            )

        state = self._inventory.state()
        chosen = self._accepted_variables(entry, variables or {}, state)

        run_id = _new_run_id()
        extra_vars = dict(chosen)
        if entry.results_variable:
            # Where a measuring playbook fetches what it measured. The service
            # fills it, never the caller: it is a path inside this container,
            # and it names *this* run.
            #
            # It goes to the request and stays out of `record.variables`, which
            # is what a relaunch replays. Recording it made a relaunch send it
            # back as a caller supplied variable, which the API refuses because
            # no catalogue entry declares it, so a measurement could be
            # launched and never relaunched. Had it been accepted the outcome
            # was worse: the second run would have fetched its results into the
            # first run's directory. The exact invocation is still recorded, in
            # `command`, which is built from the request.
            extra_vars[entry.results_variable] = str(self._store.results_dir(run_id))
        record = RunRecord(
            id=run_id,
            playbook=entry.playbook,
            playbook_id=entry.id,
            check=check,
            launched_by=launched_by,
            inventory_commit=state.commit,
            collection_version=self.collection_version(),
            variables=chosen,
            guest=guest,
            scope=plan.requested,
            machines=plan.hosts,
        )

        # The lock before the directory: two operators must not converge the
        # same machines concurrently, and the loser must be told which run is
        # already going.
        try:
            self._store.acquire(run_id)
        except RunLocked as error:
            raise ApiError("run_in_progress", str(error), 409) from error

        try:
            directory = self._store.create(record)
            # The inventory folder, its companion files and the artefacts, laid
            # out where Ansible looks for them. Done before the thread starts,
            # so a failure to stage is reported to the operator who launched
            # the run rather than found in a log afterwards.
            staged = staging.stage(
                directory=directory,
                inventory_dir=self._inventory.folder,
                collections_path=self._paths.collections_path,
                artefacts_dir=self._inventory.artefacts_root,
            )
            record.files = staged.files
            playbook = entry.playbook
            if play is not None:
                # Into the mirror's own `playbooks/`, which is a real
                # directory, so the generated play sits where every other
                # playbook of this run sits and is part of the trace
                # afterwards.
                target = staged.site_root / "playbooks" / f"{entry.id}.yaml"
                target.write_text(play)
                playbook = str(target)
        except Exception:
            self._store.release(run_id)
            raise

        request = self._request(
            record, entry, directory, staged, extra_vars, playbook, plan.limit
        )
        record.state = RunState.RUNNING
        record.started_at = datetime.now(tz=UTC)
        # Recorded before the thread starts, so the run view shows the exact
        # invocation while it is going, and still shows it on a run that ends
        # without ever coming back here.
        record.command = build_command(request)
        self._store.save(record)
        audit_event(
            "run.launched",
            run=run_id,
            playbook=entry.id,
            user=launched_by,
            check=check,
            commit=state.commit,
            # The audit trail says what was played, not only what was asked
            # for: a convergence of one machine and a convergence of five are
            # different acts under the same playbook name.
            limit=plan.limit or "",
            machines=",".join(plan.hosts or []),
        )

        thread = threading.Thread(
            target=self._execute,
            args=(record, request),
            name=f"run-{run_id}",
            daemon=True,
        )
        thread.start()
        return record

    def cancel(self, run_id: str) -> RunRecord:
        record = self._store.load(run_id)
        if record is None:
            raise ApiError("unknown_run", f"There is no run {run_id}.", 404)
        if record.finished:
            raise ApiError(
                "run_finished", f"Run {run_id} is already {record.state.value}.", 409
            )
        self._cancelled.add(run_id)
        audit_event("run.cancel_requested", run=run_id)
        return record

    def _accepted_variables(
        self,
        entry: PlaybookEntry,
        supplied: dict[str, Any],
        state: InventoryState,
    ) -> dict[str, Any]:
        """Only what the catalogue entry declares.

        Anything else is refused, because a free form extra vars field is a tag
        selector wearing a different hat.
        """
        declared = {spec.name: spec for spec in entry.variables}
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ApiError(
                "unknown_variable",
                (
                    f"{entry.title} accepts "
                    + (", ".join(sorted(declared)) or "no variables")
                    + f", not {', '.join(unknown)}."
                ),
                400,
                {"accepted": sorted(declared)},
            )
        missing = [
            name
            for name, spec in declared.items()
            if spec.required and name not in supplied
        ]
        if missing:
            raise ApiError(
                "missing_variable",
                f"{entry.title} requires {', '.join(missing)}.",
                400,
            )
        for name, value in supplied.items():
            spec = declared[name]
            if spec.type is VariableType.MACHINE:
                self._check_machine(entry, name, value, state)
            else:
                supplied[name] = _checked_value(entry, spec, value)
        return dict(supplied)

    def _check_machine(
        self,
        entry: PlaybookEntry,
        name: str,
        value: Any,
        state: InventoryState,
    ) -> None:
        """A machine variable names a machine of this inventory, not this one.

        The playbook behind this is `cluster_remove_machine`, which reads
        `hostvars[machine_to_remove]` and then sends the eviction to another
        member. A name the inventory does not carry fails on an undefined
        host halfway through, and this node's own name asks it to evict itself
        from the cluster it is driving.
        """
        hosts = list(state.inventory.hosts) if state.inventory else []
        if value not in hosts:
            raise ApiError(
                "invalid_variable",
                (
                    f"{value!r} is not a machine of this inventory. "
                    + (f"It declares {', '.join(hosts)}." if hosts else "")
                ).strip(),
                400,
                {"variable": name, "machines": hosts},
            )
        if value == state.this_host:
            raise ApiError(
                "invalid_variable",
                (
                    f"{value} is this machine, and it is the one driving the "
                    f"run. {entry.title} has to be launched from a machine "
                    "that stays in the cluster."
                ),
                400,
                {"variable": name, "machines": hosts},
            )

    def _request(
        self,
        record: RunRecord,
        entry: PlaybookEntry,
        directory: Path,
        staged: staging.Staging,
        extra_vars: dict[str, Any],
        playbook: str | None = None,
        limit: str | None = None,
    ) -> RunRequest:
        return RunRequest(
            run_id=record.id,
            playbook=playbook or entry.playbook,
            inventory_file=staged.inventory_file,
            private_data_dir=directory,
            collections_path=self._paths.collections_path,
            site_collections_path=staged.collections_paths[0],
            private_key_file=self._paths.private_key_file,
            known_hosts_file=self._paths.known_hosts_file,
            ssh_config_file=self._paths.ssh_config_file,
            extra_key_files=self._paths.extra_key_files(),
            extra_vars=extra_vars,
            check=record.check,
            limit=limit,
        )

    def _execute(self, record: RunRecord, request: RunRequest) -> None:
        run_progress = RunProgress()

        def on_event(event: dict) -> None:
            progress.apply_event(run_progress, event)
            summary = progress.summarise(event)
            if summary is not None:
                self._store.append_event(record.id, summary)
            record.progress = run_progress
            self._store.save(record)

        try:
            outcome = self._adapter.execute(
                request,
                on_event=on_event,
                on_output=lambda text: self._store.append_log(record.id, text),
                should_cancel=lambda: record.id in self._cancelled,
            )
            record.return_code = outcome.return_code
            record.state = self._final_state(outcome, run_progress)
            record.message = self._final_message(record, outcome)
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("Run %s raised", record.id)
            record.state = RunState.FAILED
            record.message = str(error)
        finally:
            record.finished_at = datetime.now(tz=UTC)
            record.progress = run_progress
            self._store.save(record)
            self._store.release(record.id)
            self._cancelled.discard(record.id)
            audit_event("run.finished", run=record.id, state=record.state.value)

    @staticmethod
    def _final_state(outcome, run_progress: RunProgress) -> RunState:
        if outcome.cancelled:
            return RunState.CANCELLED
        if outcome.error:
            return RunState.FAILED
        # The rule that matters. A run that ends without Ansible's recap did
        # not finish, it stopped existing, and that is almost always because
        # the playbook rebooted the machine it was running from. Calling it a
        # failure would send an operator looking for a fault that is not there.
        #
        # With one exception, found on a real node: a run that never started a
        # single task never reached a machine at all, so no reboot can explain
        # it. Ansible refused before it began, over a missing collection or a
        # playbook it could not parse, and the reason is in the log. Calling
        # that "interrupted, relaunching is safe" sends an operator to relaunch
        # something that will fail again in half a second.
        if not run_progress.final_status_seen:
            if run_progress.tasks_started == 0:
                return RunState.FAILED
            return RunState.INTERRUPTED
        return RunState.SUCCESS if outcome.return_code == 0 else RunState.FAILED

    def _final_message(self, record: RunRecord, outcome) -> str | None:
        if outcome.error:
            return outcome.error
        if record.state is RunState.FAILED and not record.progress.tasks_started:
            # Ansible said why before it stopped, and that sentence is worth
            # more than anything this service can infer.
            return (
                "Ansible stopped before it reached any machine, so nothing was "
                "changed. " + self._first_error(record.id)
            )
        if record.state is RunState.INTERRUPTED:
            reached = [
                host for host, state in record.progress.hosts.items() if state.reached
            ]
            return (
                "The run ended without a final status, which usually means the "
                "playbook rebooted the machine it was running from. Relaunching "
                "is safe: the playbooks are idempotent. Hosts reached: "
                + (", ".join(sorted(reached)) or "none")
                + "."
            )
        if record.state is RunState.CANCELLED:
            return (
                "Cancelled. A convergence stopped part way leaves the machine "
                "between two states, so relaunch or check it before relying "
                "on it."
            )
        if record.state is RunState.FAILED:
            return self._why_it_stopped(record)
        return None

    def _why_it_stopped(self, record: RunRecord) -> str:
        """What ended the run, from the per host counters Ansible recapped.

        A host that could not be reached and a task that failed are two
        different events with two different answers, and saying "a host failed
        and any_errors_fatal stopped everything" over an unreachable host sends
        an operator reading a role for a fault that is in the SSH path. The
        recap already separates them, so this reads it rather than assuming.
        """
        hosts = record.progress.hosts
        unreachable = sorted(name for name, state in hosts.items() if state.unreachable)
        failed = sorted(name for name, state in hosts.items() if state.failed)
        if not unreachable:
            return (
                "A host failed and any_errors_fatal stopped everything. The "
                "per host results below name which ones were reached."
            )
        names = ", ".join(unreachable)
        opening = (
            f"{names} could not be reached, so nothing ran there and nothing "
            "was changed on it. "
        )
        if failed:
            opening = (
                f"{', '.join(failed)} failed a task and {names} could not be "
                "reached at all. "
            )
        # What SSH answered, which is the sentence that names the cause. It is
        # in the event stream and was only ever in the log, so an operator read
        # this service's advice and downloaded a file to find out which half of
        # it applied.
        said = [
            f"{name}: {hosts[name].unreachable_message}"
            for name in unreachable
            if hosts[name].unreachable_message
        ]
        if said:
            opening += " ".join(said) + " "
        return opening + self._connection_advice(unreachable)

    def _connection_advice(self, unreachable: list[str]) -> str:
        """Where to look for a connection that never opened.

        A guest is worth its own sentence. The machines carry the trust this
        service provisions and their reachability is a precondition it checks
        before the run; a guest is reached over SSH like any other host and
        this service installs nothing in it, so an operator who has never had
        to think about that is exactly the one measuring the latency inside one
        for the first time.
        """
        state = self._inventory.state()
        declared = state.inventory.guests if state.inventory else {}
        guests = [name for name in unreachable if name in declared]
        if guests and len(guests) == len(unreachable):
            return (
                "A guest is reached over SSH like any other host, and this "
                "service installs nothing inside one: its inventory entry "
                "needs an ansible_host this node can route to, and an account "
                "this node holds a key for, with sudo. The log below carries "
                "what SSH answered."
            )
        return (
            "Unreachable is the connection failing rather than a task: the "
            "address in the inventory, the host key, the key of the ansible "
            "account, or a machine that is down. The log below carries what "
            "SSH answered."
        )

    def _first_error(self, run_id: str) -> str:
        """The line in the log that names the cause, colours stripped."""
        try:
            log = self._store.log(run_id)
        except OSError:
            return "The log below has the reason."
        plain = _ANSI.sub("", log)
        for line in plain.splitlines():
            stripped = line.strip()
            if stripped.startswith("ERROR!") or stripped.startswith("fatal:"):
                return stripped
        for line in reversed(plain.splitlines()):
            if line.strip():
                return line.strip()
        return "The log below has the reason."


def _addressable_guests(inventory: Inventory) -> list[str]:
    """The guests whose entry carries an address a run could connect to.

    Most guests carry none: the VM roles read the group to create domains, and
    creating a domain needs no route to the guest. An address is there because
    somebody put it there so that a play could reach inside, which is either an
    operator writing it or the network section of the VMs page writing it
    beside the address it gives the guest.
    """
    return [
        name
        for name, guest in inventory.guests.items()
        if (guest.ansible_host or "").strip()
    ]


def _seeded_guests(inventory: Inventory) -> list[str]:
    """The guests whose entry asks the deployment to build a cloud-init seed.

    Presence rather than shape, because presence is exactly what the roles
    test: `hostvars[item].cloud_init is defined`. A mapping the parser could
    not read stays in `extra` and is still a guest the deployment will try to
    seed, so it counts here too, and `validation` is what says the mapping is
    wrong.
    """
    return [
        name
        for name, guest in inventory.guests.items()
        if guest.cloud_init is not None or "cloud_init" in guest.extra
    ]


def _malformed_seeds(inventory: Inventory) -> list[str]:
    """The seeded guests whose mapping is not one the role could read.

    `extra` is where the parser keeps a modelled name whose value has the wrong
    shape, so a `cloud_init` found there is one that reached the file as
    something other than a mapping.
    """
    return [
        name for name, guest in inventory.guests.items() if "cloud_init" in guest.extra
    ]


def _new_run_id() -> str:
    # Sortable, readable, and unique on a node: the store lists runs by
    # sorting on it, and an operator reads it in a directory listing.
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S.%f")


def _checked_value(entry: PlaybookEntry, spec: VariableSpec, value: Any) -> Any:
    """One measurement parameter, checked before it reaches a command line.

    These are the first variables here that carry a number an operator picks,
    and they end up inside a shell command on every machine of the inventory.
    Each one is checked against the bound that makes it safe.
    """
    if spec.type is VariableType.BOOLEAN:
        return bool(value)

    if spec.type is VariableType.SECONDS:
        return _bounded(entry, spec, value, 1, 24 * 3600, "seconds")

    if spec.type is VariableType.MICROSECONDS:
        # A second is the ceiling: hwlatdetect's width is an interval during
        # which the machine's interrupts are held off, and a window is the
        # period one sits in. Beyond a second either is a machine taken away
        # from its guests rather than measured.
        return _bounded(entry, spec, value, 1, 1_000_000, "microseconds")

    if spec.type is VariableType.PRIORITY:
        # SCHED_FIFO's own range, minus its two ends. 0 leaves the measurement
        # outside real time scheduling altogether, and 99 puts it above the
        # kernel's own threads on a PREEMPT_RT machine, which is how a
        # measurement wedges the host it was measuring.
        return _bounded(entry, spec, value, 1, 98, "a SCHED_FIFO priority")

    if spec.type is VariableType.CPU_LIST:
        text = str(value).strip()
        if text == "smp":
            return text
        if not parse_cpu_list(text):
            raise ApiError(
                "invalid_variable",
                (
                    f"{spec.name} takes `smp` or a CPU list such as 4-7. "
                    f"{value!r} is neither."
                ),
                400,
                {"variable": spec.name},
            )
        return text

    return value


def _bounded(
    entry: PlaybookEntry,
    spec: VariableSpec,
    value: Any,
    low: int,
    high: int,
    unit: str,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(
            "invalid_variable",
            f"{spec.name} takes a whole number of {unit}. {value!r} is not one.",
            400,
            {"variable": spec.name},
        ) from None
    if not low <= number <= high:
        raise ApiError(
            "invalid_variable",
            (
                f"{spec.name} takes {unit} between {low} and {high}. "
                f"{entry.title} was asked for {number}."
            ),
            400,
            {"variable": spec.name},
        )
    return number
