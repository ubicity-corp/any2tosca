"""
companions.py
-------------
By-value pre-pass for the ``value`` relationship model (``docs/kubernetes.md``,
*References are by value*).

In OpenAPI a ``$ref`` embeds the referenced schema's value.  A property whose
schema references a node-type schema therefore holds a value of that shape,
not a relationship to a node.  The pre-pass gives every such node-type schema
a **companion**: a copy named ``<definition name>Value``, which converts to a
data type, and points the by-value references at it.  After the pre-pass, no
schema references a node-type schema except as the base (``allOf``) of another
node-type schema.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import copy
from collections.abc import Collection
from typing import Any

__all__ = ["COMPANION_SUFFIX", "CompanionError", "add_value_companions"]

#: Appended to a node-type definition name to name its companion data type.
COMPANION_SUFFIX = "Value"

_CONTAINERS = ("definitions", "$defs")


class CompanionError(Exception):
    """Raised when a companion name is already taken by another definition."""


def _target(ref: str) -> tuple[str, str, str] | None:
    """Split a local definition ref into (container, definition name, remaining fragment)."""
    for container in _CONTAINERS:
        prefix = f"#/{container}/"
        if ref.startswith(prefix):
            name, _, rest = ref[len(prefix):].partition("/")
            return container, name, (f"/{rest}" if rest else "")
    return None


def add_value_companions(
    root_schema: dict[str, Any],
    node_type_names: Collection[str],
    ignore: Collection[str] = (),
) -> dict[str, Any]:
    """
    Return a copy of *root_schema* in which no schema references a node-type
    schema by value.

    Parameters
    ----------
    root_schema:
        A normalized root schema with its definitions under ``definitions``
        (or ``$defs``).
    node_type_names:
        Definition names (as in *root_schema*) that are node types.
    ignore:
        Definition names whose references do not count, because they are left
        out of the output (skipped by split rules).  They are not rewritten.

    Every ``$ref`` to a node-type definition is pointed at the definition's
    companion, ``<name>Value``, a copy of the node-type schema added under the
    same container.  A ``$ref`` that is a direct ``allOf`` entry of a node-type
    schema is inheritance and is left alone.  References inside a companion are
    processed the same way, so a companion's base is the base's companion.

    Raises
    ------
    CompanionError
        When ``<name>Value`` already names a definition.
    """
    node_types = set(node_type_names)
    result = copy.deepcopy(root_schema)
    created: set[tuple[str, str]] = set()
    pending: list[tuple[str, str]] = []   # (container, name) of companions to scan

    def companion_ref(container: str, name: str, rest: str) -> str:
        definitions = result[container]
        value_name = f"{name}{COMPANION_SUFFIX}"
        if (container, value_name) not in created:
            if value_name in definitions:
                raise CompanionError(
                    f"companion '{value_name}' for node type '{name}' is already a definition"
                )
            definitions[value_name] = copy.deepcopy(definitions[name])
            created.add((container, value_name))
            pending.append((container, value_name))
        return f"#/{container}/{value_name}{rest}"

    def rewrite(obj: Any, inheritance_ok: bool) -> None:
        """Retarget by-value refs in *obj*; ``inheritance_ok`` for a node type's allOf."""
        if isinstance(obj, list):
            for item in obj:
                rewrite(item, False)
            return
        if not isinstance(obj, dict):
            return
        ref = obj.get("$ref")
        t = _target(ref) if isinstance(ref, str) else None
        if is_node_ref(t) and not inheritance_ok:
            obj["$ref"] = companion_ref(*t)
        for key, value in obj.items():
            if key == "$ref":
                continue
            rewrite(value, False)

    def is_node_ref(t: tuple[str, str, str] | None) -> bool:
        return (t is not None and t[1] in node_types
                and t[1] in (result.get(t[0]) or {}))

    def scan(schema: Any, is_node_type: bool) -> None:
        """Retarget by-value refs in a definition; a node type's own base refs stay."""
        if not isinstance(schema, dict):
            return
        for key, value in schema.items():
            if key == "$ref" or key in _CONTAINERS:
                continue
            if key == "allOf" and is_node_type and isinstance(value, list):
                for entry in value:
                    rewrite(entry, True)
            else:
                rewrite(value, False)
        ref = schema.get("$ref")
        t = _target(ref) if isinstance(ref, str) else None
        if is_node_ref(t) and not is_node_type:
            schema["$ref"] = companion_ref(*t)

    scan(result, False)   # the root schema itself, outside its definitions
    skip = set(ignore)
    for container in _CONTAINERS:
        for name in list(result.get(container) or {}):
            if name not in skip:
                scan(result[container][name], name in node_types)
    while pending:
        container, name = pending.pop()
        scan(result[container][name], False)
    return result
