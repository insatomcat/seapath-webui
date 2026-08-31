# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Changing one variable in an inventory without rewriting the file.

The service used to save by rendering the whole model back to YAML. For a file
it wrote itself that is harmless. For a file an engineer wrote it is a
catastrophe with a clean exit code: the model holds a dozen fields, a real
inventory holds fifty, and the render keeps the dozen.

So a save is an edit, and an edit touches the lines it changes. Everything else
in the file survives byte for byte, comments and layout included, which is what
makes `git log` on this repository worth reading: one form submission is one
line of diff.

The mechanics come from `ruamel.yaml`, used here as a parser that reports
positions rather than as a writer. Every mapping it loads carries the line and
column of each key and each value, so a change becomes a splice into the
original text.

Two rules decide where a change lands:

- A variable already on the host is changed where it sits.
- A variable the host inherits from a group is written **on the host**, as an
  override. The form edits one machine, and rewriting the group would silently
  change the other two.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML

from app.inventory.resolve import ROOT, groups


class UneditableInventory(Exception):
    """The change cannot be expressed as an edit to this file.

    Raised rather than guessed at. A save that mangled a file it did not
    understand would be exactly the failure this module exists to prevent.
    """


def edit(document: str, changes: dict[str, dict[str, Any]]) -> str:
    """Apply per host variable changes, touching only the lines they occupy."""
    if not changes:
        return document

    yaml = _yaml()
    loaded = yaml.load(document)
    if loaded is None:
        raise UneditableInventory("The inventory is empty.")

    lines = document.splitlines(keepends=True)
    inherited = _inherited(loaded)
    splices: list[_Splice] = []

    for host, variables in changes.items():
        mapping = _host_mapping(loaded, host)
        if mapping is None:
            raise UneditableInventory(
                f"{host} has no entry of its own in this inventory, so there is "
                "nowhere to write its variables."
            )
        for variable, value in sorted(variables.items()):
            splices.append(_change(lines, mapping, host, variable, value, inherited))

    # Applied bottom up, so an earlier splice cannot move a later one's lines.
    for splice in sorted(splices, key=lambda s: s.start, reverse=True):
        lines[splice.start : splice.end] = splice.replacement

    return "".join(lines)


class _Splice:
    def __init__(self, start: int, end: int, replacement: list[str]) -> None:
        self.start = start
        self.end = end
        self.replacement = replacement


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _change(
    lines: list[str],
    mapping: Any,
    host: str,
    variable: str,
    value: Any,
    inherited: dict[str, dict[str, str]],
) -> _Splice:
    present = variable in mapping

    if value is None or value == [] or value == "":
        if not present:
            source = inherited.get(host, {}).get(variable)
            if source is not None:
                raise UneditableInventory(
                    f"{variable} comes from the group {source}, and removing it "
                    f"from {host} alone would need an override this service "
                    "does not write yet."
                )
            # Absent already, so the change is a change to nothing.
            return _Splice(0, 0, lines[0:0])
        return _delete(lines, mapping, variable)

    if present:
        return _replace(lines, mapping, variable, value)
    return _insert(lines, mapping, variable, value)


def _replace(lines: list[str], mapping: Any, variable: str, value: Any) -> _Splice:
    line, column = mapping.lc.value(variable)
    key_line, key_column = mapping.lc.key(variable)
    if line != key_line:
        # A value on its own line, meaning a block list or a block scalar. The
        # whole block goes, and the new value is written in its place.
        end = _block_end(lines, key_line + 1, key_column)
        return _Splice(key_line, end, _emit(variable, value, key_column))

    if isinstance(value, list) or _is_block(lines, line + 1, key_column):
        end = _block_end(lines, line + 1, key_column)
        return _Splice(key_line, end, _emit(variable, value, key_column))

    text = lines[line]
    head = text[:column]
    tail = _trailing_comment(text[column:])
    return _Splice(line, line + 1, [f"{head}{_scalar(value)}{tail}"])


def _insert(lines: list[str], mapping: Any, variable: str, value: Any) -> _Splice:
    column = _mapping_column(mapping)
    start = _mapping_end(lines, mapping)
    return _Splice(start, start, _emit(variable, value, column))


def _delete(lines: list[str], mapping: Any, variable: str) -> _Splice:
    key_line, key_column = mapping.lc.key(variable)
    end = _block_end(lines, key_line + 1, key_column)
    return _Splice(key_line, end, [])


def _emit(variable: str, value: Any, column: int) -> list[str]:
    """The variable as YAML, indented to sit where it is going."""
    buffer = io.StringIO()
    _yaml().dump({variable: value}, buffer)
    pad = " " * column
    return [
        f"{pad}{line}\n" if line else "\n"
        for line in buffer.getvalue().rstrip("\n").splitlines()
    ]


def _scalar(value: Any) -> str:
    buffer = io.StringIO()
    _yaml().dump({"v": value}, buffer)
    return buffer.getvalue().rstrip("\n").split(":", 1)[1].strip()


def _trailing_comment(rest: str) -> str:
    """Everything after the value on its line, so a comment survives the edit.

    The `#` has to be found outside quotes: an address is not a comment because
    somebody wrote a hash inside a string next to it.
    """
    newline = "\n" if rest.endswith("\n") else ""
    body = rest.rstrip("\n")
    quote: str | None = None
    for index, character in enumerate(body):
        if quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#":
            return f"  {body[index:]}{newline}"
    return newline


def _is_block(lines: list[str], start: int, column: int) -> bool:
    return _block_end(lines, start, column) > start


def _block_end(lines: list[str], start: int, column: int) -> int:
    """The first line at or before `column`, which is where this block stops."""
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and len(lines[index]) - len(lines[index].lstrip()) <= column:
            break
        index += 1
    return index


def _mapping_column(mapping: Any) -> int:
    keys = list(mapping)
    if keys:
        return mapping.lc.key(keys[0])[1]
    return mapping.lc.col


def _mapping_end(lines: list[str], mapping: Any) -> int:
    """Where to append a new key, which is after everything the mapping holds."""
    keys = list(mapping)
    if not keys:
        return mapping.lc.line + 1
    first_line, column = mapping.lc.key(keys[0])
    end = _block_end(lines, first_line + 1, column - 1)
    # Blank lines at the end of a block belong to whatever follows it.
    while end > first_line + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _host_mapping(loaded: Any, host: str) -> Any:
    """The mapping holding this host's own variables, wherever it is declared."""
    for group in _group_bodies(loaded):
        hosts = group.get("hosts")
        if isinstance(hosts, dict) and host in hosts and isinstance(hosts[host], dict):
            return hosts[host]
    return None


def _group_bodies(loaded: Any) -> list[Any]:
    """Every group body in the file, in both declaration shapes."""
    found: list[Any] = []
    stack = [body for body in loaded.values() if isinstance(body, dict)]
    while stack:
        body = stack.pop()
        found.append(body)
        children = body.get("children")
        if isinstance(children, dict):
            stack.extend(
                child for child in children.values() if isinstance(child, dict)
            )
    return found


def _inherited(loaded: Any) -> dict[str, dict[str, str]]:
    """For each host, which group each inherited variable comes from."""
    table = groups(loaded)
    everywhere = {host for group in table.values() for host in group.hosts}
    sources: dict[str, dict[str, str]] = {host: {} for host in everywhere}
    for group in table.values():
        members = _members(table, group.name) if group.name != ROOT else everywhere
        for host in members:
            for variable in group.variables:
                sources.setdefault(host, {})[variable] = group.name
    return sources


def _members(table: dict, name: str) -> set[str]:
    seen: set[str] = set()
    hosts: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen or current not in table:
            continue
        seen.add(current)
        hosts.update(table[current].hosts)
        stack.extend(table[current].children)
    return hosts
