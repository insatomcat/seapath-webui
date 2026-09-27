# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A container delivery, as a supplier hands it over.

`roles/deploy_containers_cluster/DELIVERY.md` in seapath-ansible is the
contract: a directory holding the images as archives, the quadlet templates,
an example of each configuration file in `examples/`, `values.yaml` naming the
site values the templates read, `checks.yaml` comparing configuration files
with those values, and `inventory-example.yaml`, the `cluster_containers`
entry with example values.

A delivery made before `examples/` has `files/` instead, the first content of
the RBD image: those are read as the examples of files the RBD image is seeded
with, from the site's copies.

This reads such a delivery and says what is wrong with it before anything is
written: an installer that finds a missing image three minutes into a run has
already committed a workload that cannot start. It writes nothing itself. What
it answers is the entry to commit and the files that go with it, which the
service writes as one commit, and the image archives, which go to the
artefacts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import jinja2
import jinja2.sandbox
import yaml

from app.inventory import quadlets

VALUES_FILE = "values.yaml"
CHECKS_FILE = "checks.yaml"
EXAMPLE_FILE = "inventory-example.yaml"
EXAMPLES_DIR = "examples"
LEGACY_DIR = "files"
SITE_DIR = "site"
MATCHES = ("equal", "in_list")
README_FILE = "README.md"
SUMS_FILE = "SHA256SUMS"

FORMATS = (
    "string",
    "integer",
    "boolean",
    "ipv4",
    "ipv4_network",
    "mac",
    "vlan_list",
    "name",
)

# What a template reads of the workload. `container.images` is the one key the
# delivery does not ask the site for: it is the list of its own images.
_READ = re.compile(r"container\.([A-Za-z_][A-Za-z0-9_]*)")
_UNDER_FILES = re.compile(r"(?:^|/)files/(.+)$")
_MAC = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
# An interface, bridge or port name: what the kernel accepts, IFNAMSIZ less one.
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


class InvalidDelivery(Exception):
    """The delivery cannot be installed; `findings` says every reason."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__(findings[0] if findings else "invalid delivery")
        self.findings = findings


@dataclass
class Value:
    """One site value, as `values.yaml` describes it."""

    key: str
    description: str
    format: str
    example: Any
    default: Any = None
    has_default: bool = False
    minimum: int | None = None
    maximum: int | None = None


@dataclass
class Image:
    name: str
    archive: str
    """The archive's file name under `images/`."""


@dataclass
class Delivery:
    root: Path
    name: str
    """The workload name, the key of `inventory-example.yaml`."""
    example: dict[str, Any]
    values: list[Value]
    images: list[Image]
    quadlets: list[str]
    """The quadlet files, in the order the example lists them."""
    examples: list[str] = field(default_factory=list)
    """The example configuration files, by path under `examples/`, or under
    `files/` for a delivery made before `examples/`."""
    files: dict[str, str] = field(default_factory=dict)
    """Only for a delivery with `files/`: each example to the `dest` it has on
    the RBD image."""
    checks: list[dict[str, Any]] = field(default_factory=list)
    readme: str = ""

    @property
    def legacy(self) -> bool:
        """Made before `examples/`: its quadlets read the RBD image, which the
        role seeds once from `rbd.files`."""
        return bool(self.files)

    @property
    def examples_dir(self) -> str:
        return LEGACY_DIR if self.legacy else EXAMPLES_DIR


# Unpacking


def unpack(archive: Path, into: Path) -> Path:
    """Extract a delivery archive and return the delivery's own directory.

    Members are checked one by one rather than trusted to the `tarfile`
    filter alone: a delivery holds regular files and directories, and an
    absolute path, a `..`, a link or a device in one is refused whole.
    """
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise InvalidDelivery(
                        [f"{member.name} points outside the delivery."]
                    )
                if not (member.isfile() or member.isdir()):
                    raise InvalidDelivery(
                        [f"{member.name} is neither a file nor a directory."]
                    )
            into.mkdir(parents=True, exist_ok=True)
            tar.extractall(into, members=members, filter="data")
    except tarfile.TarError as error:
        raise InvalidDelivery([f"The archive cannot be read: {error}."]) from error
    return root_of(into)


def root_of(into: Path) -> Path:
    """The directory holding `values.yaml`, at the top or one level down."""
    if (into / EXAMPLE_FILE).is_file():
        return into
    candidates = [path for path in into.iterdir() if (path / EXAMPLE_FILE).is_file()]
    if len(candidates) != 1:
        raise InvalidDelivery(
            [
                f"No {EXAMPLE_FILE} at the top of the archive or in its one "
                "directory: this is not a delivery."
            ]
        )
    return candidates[0]


# Reading


def read(root: Path) -> Delivery:
    """Read and check a delivery, raising with every finding at once."""
    findings: list[str] = []
    name, example = _example(root, findings)
    values = _values(root, findings)
    images = _images(root, example, findings)
    names = _quadlets(root, example, findings)
    if (root / EXAMPLES_DIR).is_dir():
        files: dict[str, str] = {}
        examples = _examples(root / EXAMPLES_DIR)
    else:
        files = _files(root, example, findings)
        examples = list(files)
    if values is not None and names:
        templates = [root / "quadlets" / name for name in names] + [
            root / (LEGACY_DIR if files else EXAMPLES_DIR) / example
            for example in examples
        ]
        _keys_match(templates, values, findings)
    checks = _checks(root, values or [], examples, findings)
    if findings:
        raise InvalidDelivery(findings)
    readme = root / README_FILE
    return Delivery(
        root=root,
        name=name,
        example=example,
        values=values or [],
        images=images,
        quadlets=names,
        examples=examples,
        files=files,
        checks=checks,
        readme=readme.read_text(errors="replace") if readme.is_file() else "",
    )


def config_name(path: str) -> str:
    """The name a configuration file is written under on the nodes."""
    name = PurePosixPath(path).name
    return name[: -len(".j2")] if name.endswith(".j2") else name


def _examples(directory: Path) -> list[str]:
    return sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    )


def _checks(
    root: Path, values: list[Value], examples: list[str], findings: list[str]
) -> list[dict[str, Any]]:
    """The checks of `checks.yaml`, which the role runs before each deployment.

    Only their shape is checked here: the XPath is evaluated by lxml on the
    Ansible machine, against the site's files.
    """
    path = root / CHECKS_FILE
    if not path.is_file():
        return []
    loaded = _yaml(path, findings)
    if not isinstance(loaded, list):
        findings.append(f"{CHECKS_FILE} must be a list of checks.")
        return []
    keys = {value.key for value in values}
    names = {config_name(example) for example in examples}
    found: list[dict[str, Any]] = []
    for index, check in enumerate(loaded, start=1):
        where = f"{CHECKS_FILE}, check {index}"
        if not isinstance(check, dict):
            findings.append(f"{where} is not a mapping.")
            continue
        for key in ("file", "xpath", "value"):
            if not isinstance(check.get(key), str) or not check[key].strip():
                findings.append(f"{where} has no {key}.")
        if isinstance(check.get("value"), str) and check["value"] not in keys:
            findings.append(
                f"{where} compares {check['value']}, which {VALUES_FILE} "
                "does not describe."
            )
        if isinstance(check.get("file"), str) and check["file"] not in names:
            findings.append(
                f"{where} reads {check['file']}, which is not one of the examples."
            )
        if check.get("match", "equal") not in MATCHES:
            findings.append(f"{where}: match is one of {', '.join(MATCHES)}.")
        if "base" in check and (
            isinstance(check["base"], bool) or not isinstance(check["base"], int)
        ):
            findings.append(f"{where}: base is an integer, 16 for hexadecimal.")
        if "namespaces" in check and not isinstance(check["namespaces"], dict):
            findings.append(f"{where}: namespaces maps each prefix to its URI.")
        found.append(check)
    return found


def _example(root: Path, findings: list[str]) -> tuple[str, dict[str, Any]]:
    loaded = _yaml(root / EXAMPLE_FILE, findings)
    if isinstance(loaded, dict) and set(loaded) == {"cluster_containers"}:
        loaded = loaded["cluster_containers"]
    if not isinstance(loaded, dict) or len(loaded) != 1:
        findings.append(f"{EXAMPLE_FILE} must hold exactly one workload.")
        return "", {}
    name, spec = next(iter(loaded.items()))
    if not isinstance(name, str) or not quadlets.NAME.match(name):
        findings.append(
            f"{name!r} cannot be a workload name: it becomes the Pacemaker "
            "resource and the RBD image, so letters, digits, dots, dashes and "
            "underscores."
        )
    if not isinstance(spec, dict):
        findings.append(f"{EXAMPLE_FILE}: the entry of {name} is not a mapping.")
        return str(name), {}
    return name, spec


def _values(root: Path, findings: list[str]) -> list[Value] | None:
    loaded = _yaml(root / VALUES_FILE, findings)
    if loaded is None:
        return None
    return parse_values(loaded, findings)


def parse_values(loaded: Any, findings: list[str]) -> list[Value] | None:
    """The site values a `values.yaml` describes, and what is wrong with it.

    Read from the delivery on import, and from the inventory folder afterwards,
    where the import keeps the file so that the values stay editable.
    """
    if not isinstance(loaded, dict):
        findings.append(f"{VALUES_FILE} must map each site value to its description.")
        return None
    found: list[Value] = []
    for key, spec in loaded.items():
        if not isinstance(spec, dict):
            findings.append(f"{VALUES_FILE}: {key} is not described.")
            continue
        value = Value(
            key=str(key),
            description=str(spec.get("description") or ""),
            format=str(spec.get("format") or ""),
            example=spec.get("example"),
            default=spec.get("default"),
            has_default="default" in spec,
            minimum=spec.get("min"),
            maximum=spec.get("max"),
        )
        if not value.description:
            findings.append(f"{VALUES_FILE}: {key} has no description.")
        if value.format not in FORMATS:
            findings.append(
                f"{VALUES_FILE}: {key} has the format {value.format!r}, which is "
                f"not one of {', '.join(FORMATS)}."
            )
            continue
        if "example" not in spec:
            findings.append(f"{VALUES_FILE}: {key} has no example.")
        else:
            error = check(value, value.example)
            if error:
                findings.append(f"{VALUES_FILE}: the example of {key} {error}")
        if value.has_default:
            error = check(value, value.default)
            if error:
                findings.append(f"{VALUES_FILE}: the default of {key} {error}")
        found.append(value)
    return found


def _images(root: Path, example: dict[str, Any], findings: list[str]) -> list[Image]:
    declared = example.get("images") or []
    if not isinstance(declared, list):
        findings.append(f"{EXAMPLE_FILE}: images is not a list.")
        return []
    sums = _sums(root / "images" / SUMS_FILE, findings)
    found: list[Image] = []
    for item in declared:
        name = item.get("name") if isinstance(item, dict) else None
        archive = item.get("archive") if isinstance(item, dict) else None
        if not isinstance(name, str) or not isinstance(archive, str):
            findings.append(
                f"{EXAMPLE_FILE}: every image needs a name and an archive, since "
                "a substation pulls nothing."
            )
            continue
        base = PurePosixPath(archive).name
        path = root / "images" / base
        if not path.is_file():
            findings.append(f"images/{base} is missing.")
            continue
        expected = sums.get(base)
        if expected is None:
            findings.append(f"images/{SUMS_FILE} has no line for {base}.")
        elif _sha256(path) != expected:
            findings.append(f"images/{base} does not match its line in {SUMS_FILE}.")
        names = image_names(path)
        if name not in names:
            findings.append(
                f"images/{base} holds {', '.join(names) or 'no named image'}, "
                f"and the quadlets use {name}."
            )
        found.append(Image(name=name, archive=base))
    return found


def _quadlets(root: Path, example: dict[str, Any], findings: list[str]) -> list[str]:
    names = [PurePosixPath(item).name for item in quadlets.workload_sources(example)]
    if not names:
        findings.append(f"{EXAMPLE_FILE}: the workload lists no quadlet.")
    for name in names:
        path = root / "quadlets" / name
        if not path.is_file():
            findings.append(f"quadlets/{name} is missing.")
        elif quadlets.starts_itself(path.read_text(errors="replace")):
            findings.append(
                f"quadlets/{name} has an [Install] section: Pacemaker starts "
                "the workload on one node, and that section would start it on "
                "every node at boot."
            )
    return names


def _files(root: Path, example: dict[str, Any], findings: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for source, dest in quadlets.workload_files(example):
        # The example names the file where its own installer put it; what
        # locates it in the delivery is the part under `files/`.
        match = _UNDER_FILES.search(source)
        relative = match.group(1) if match else PurePosixPath(source).name
        path = root / "files" / relative
        if not path.is_file():
            findings.append(f"files/{relative} is missing.")
            continue
        found[relative] = dest
    return found


def _keys_match(
    templates: list[Path], values: list[Value], findings: list[str]
) -> None:
    """The keys of `values.yaml` are those the templates, quadlets and example
    configuration files, read."""
    read_keys: set[str] = set()
    for path in templates:
        if path.is_file() and path.name.endswith(".j2"):
            read_keys.update(_READ.findall(path.read_text(errors="replace")))
    read_keys.discard("images")
    described = {value.key for value in values}
    for key in sorted(read_keys - described):
        findings.append(
            f"The templates read container.{key}, which {VALUES_FILE} "
            "does not describe."
        )
    for key in sorted(described - read_keys):
        findings.append(f"{VALUES_FILE} describes {key}, which no template reads.")


# Values


def check(value: Value, given: Any) -> str:
    """Why a value does not fit its format, or an empty string."""
    fmt = value.format
    if fmt == "integer":
        if isinstance(given, bool) or not isinstance(given, int):
            return "is not an integer."
        if value.minimum is not None and given < value.minimum:
            return f"is below {value.minimum}."
        if value.maximum is not None and given > value.maximum:
            return f"is above {value.maximum}."
        return ""
    if fmt == "boolean":
        return "" if isinstance(given, bool) else "is not true or false."
    if not isinstance(given, str) or not given.strip():
        return "is empty." if given in (None, "") else "is not text."
    text = given.strip()
    if fmt == "ipv4":
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            return f"{text} is not an IPv4 address."
    elif fmt == "ipv4_network":
        try:
            ipaddress.IPv4Network(text, strict=True)
        except ValueError:
            return f"{text} is not an IPv4 network such as 192.0.2.0/24."
    elif fmt == "mac":
        if not _MAC.match(text):
            return f"{text} is not a MAC address such as 02:00:00:00:00:01."
    elif fmt == "vlan_list":
        parts = text.split(",")
        if not all(part.strip().isdigit() and 0 <= int(part) <= 4094 for part in parts):
            return f"{text} is not a list of VLANs such as 100,300."
    elif fmt == "name":
        if not _NAME.match(text):
            return (
                f"{text} is not a name of 15 letters, digits, dots, dashes or "
                "underscores at most."
            )
    return ""


def site_values(
    described: list[Value], given: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The values to write, defaults filled in, and why any given one is refused.

    Integers and booleans arrive as text from a form, so they are read into
    their type first.
    """
    found: dict[str, Any] = {}
    refused: dict[str, str] = {}
    for value in described:
        raw = given.get(value.key)
        if raw is None or raw == "":
            if value.has_default:
                found[value.key] = value.default
                continue
            refused[value.key] = "is required."
            continue
        typed = _typed(value, raw)
        error = check(value, typed)
        if error:
            refused[value.key] = error
        else:
            found[value.key] = typed
    return found, refused


def _typed(value: Value, raw: Any) -> Any:
    if (
        value.format == "integer"
        and isinstance(raw, str)
        and raw.strip().lstrip("-").isdigit()
    ):
        return int(raw.strip())
    if value.format == "boolean" and isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    return raw.strip() if isinstance(raw, str) else raw


# The entry


def folder(name: str) -> str:
    """Where the delivery's small files go in the inventory folder."""
    return f"inventories/{name}"


def entry(
    delivery: Delivery, values: dict[str, Any], site: dict[str, str] | None = None
) -> dict[str, Any]:
    """The `cluster_containers` entry, pointing at where the files end up.

    The quadlets, the examples, `values.yaml` and the README go to the
    versioned folder; the image archives to the artefacts, which a run mounts
    under the same root, so `../files/<archive>` finds them. `site` maps each
    example to the site's file in the folder that takes its place.
    """
    home = f"../{folder(delivery.name)}"
    spec: dict[str, Any] = {}
    unit = delivery.example.get("unit")
    if isinstance(unit, str) and unit.strip():
        spec["unit"] = unit.strip()
    spec["images"] = [
        {"name": image.name, "archive": f"../files/{image.archive}"}
        for image in delivery.images
    ]
    spec["quadlets"] = [f"{home}/quadlets/{name}" for name in delivery.quadlets]
    site = site or {}
    sources = [
        f"../{site.get(example) or site_path(delivery, example)}"
        for example in delivery.examples
    ]
    if sources and not delivery.legacy:
        spec["config"] = sources
    if delivery.checks:
        spec["checks"] = delivery.checks
    rbd = delivery.example.get("rbd")
    if isinstance(rbd, dict) and rbd.get("size"):
        spec["rbd"] = {"size": rbd["size"]}
        if delivery.legacy:
            spec["rbd"]["files"] = [
                {"src": source, "dest": delivery.files[example]}
                for example, source in zip(delivery.examples, sources, strict=True)
            ]
    spec.update(values)
    return spec


def site_path(delivery: Delivery, example: str) -> str:
    """Where the site's file for an example goes when the site has none yet."""
    return f"{folder(delivery.name)}/{SITE_DIR}/{example}"


def inventory_files(delivery: Delivery) -> dict[str, bytes]:
    """Every file of the delivery the versioned folder receives, by its path
    there. The site's own files are the service's: an import never writes
    them, beyond a first copy of an example."""
    home = folder(delivery.name)
    found = {
        f"{home}/quadlets/{name}": (delivery.root / "quadlets" / name).read_bytes()
        for name in delivery.quadlets
    }
    for relative in delivery.examples:
        found[f"{home}/{EXAMPLES_DIR}/{relative}"] = example_bytes(delivery, relative)
    found[f"{home}/{VALUES_FILE}"] = (delivery.root / VALUES_FILE).read_bytes()
    checks = delivery.root / CHECKS_FILE
    if checks.is_file():
        found[f"{home}/{CHECKS_FILE}"] = checks.read_bytes()
    readme = delivery.root / README_FILE
    if readme.is_file():
        found[f"{home}/{README_FILE}"] = readme.read_bytes()
    return found


def example_bytes(delivery: Delivery, example: str) -> bytes:
    return (delivery.root / delivery.examples_dir / example).read_bytes()


def render(delivery: Delivery, spec: dict[str, Any]) -> list[str]:
    """Render every template with the entry, as the role will, and say what fails.

    Sandboxed and strict: an undefined value is the finding this exists for.
    A filter only Ansible has cannot be judged here, so a template using one is
    left to the run rather than refused.
    """
    environment = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    findings: list[str] = []
    for name in delivery.quadlets:
        if not name.endswith(".j2"):
            continue
        text = (delivery.root / "quadlets" / name).read_text(errors="replace")
        try:
            rendered = environment.from_string(text).render(container=spec)
        except jinja2.TemplateAssertionError as error:
            if "No filter named" in str(error) or "No test named" in str(error):
                continue
            findings.append(f"quadlets/{name}: {error}.")
            continue
        except jinja2.UndefinedError as error:
            findings.append(f"quadlets/{name} reads a value the entry lacks: {error}.")
            continue
        except jinja2.TemplateError as error:
            findings.append(f"quadlets/{name}: {error}.")
            continue
        if quadlets.starts_itself(rendered):
            findings.append(f"quadlets/{name} renders an [Install] section.")
    return findings


def move_images(delivery: Delivery, target: Path) -> None:
    """Move the image archives out of the staged delivery.

    `target` is in the artefacts, on the same filesystem as the staging area,
    so this is a rename rather than a copy of a few hundred megabytes.
    """
    target.mkdir(parents=True, exist_ok=True)
    for image in delivery.images:
        shutil.move(delivery.root / "images" / image.archive, target / image.archive)


# Helpers


def image_names(archive: Path) -> list[str]:
    """The names an image archive gives what `podman load` loads from it.

    `podman save` writes a docker archive, whose `manifest.json` carries
    `RepoTags`; an OCI archive names its image with the `ref.name` annotation
    of `index.json`.
    """
    names: list[str] = []
    try:
        with tarfile.open(archive) as tar:
            for member_name, key in (
                ("manifest.json", "docker"),
                ("index.json", "oci"),
            ):
                try:
                    member = tar.extractfile(member_name)
                except KeyError:
                    continue
                if member is None:
                    continue
                document = json.loads(member.read())
                if key == "docker":
                    for item in document if isinstance(document, list) else []:
                        names.extend(item.get("RepoTags") or [])
                else:
                    for item in document.get("manifests") or []:
                        ref = (item.get("annotations") or {}).get(
                            "org.opencontainers.image.ref.name"
                        )
                        if ref:
                            names.append(ref)
    except (tarfile.TarError, ValueError, OSError):
        return []
    return names


def _sums(path: Path, findings: list[str]) -> dict[str, str]:
    if not path.is_file():
        findings.append(f"images/{SUMS_FILE} is missing.")
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[1].lstrip("*")] = parts[0].lower()
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _yaml(path: Path, findings: list[str]) -> Any:
    if not path.is_file():
        findings.append(f"{path.name} is missing.")
        return None
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        findings.append(f"{path.name} is not valid YAML: {error}.")
        return None
