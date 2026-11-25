"""
split.py
--------
Splits one normalized root schema into several units, each converted to its
own TOSCA file.  See ``docs/multi-output.md``.

Split rules assign every definition of the root schema to a unit (or skip
it) and give it its TOSCA type name.  The splitter builds one in-memory JSON
Schema document per unit, rewrites references between definitions into
references within or across units, and checks that the units import each
other without cycles.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import copy
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "SplitError", "SplitRule", "SplitRules", "Unit", "SplitResult",
    "load_rules", "skipped_definitions", "split_schema",
]

_DEF_PREFIX = "#/definitions/"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSIONED_RE = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
_RULE_KEYS = {"match", "unit", "namespace", "skip"}


class SplitError(Exception):
    """Raised when split rules are invalid or cannot be applied to a schema."""


def _sanitize(name: str) -> str:
    """Make a string safe for use as a TOSCA name (same as ``Converter.sanitize_name``)."""
    return name.replace(" ", "_").replace("-", "_").replace(".", "_")


# ── Rules ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SplitRule:
    """One split rule: a pattern and what to do with the definitions it matches."""
    pattern: re.Pattern[str]
    unit: str | None = None
    namespace: str | None = None
    skip: bool = False


@dataclass(frozen=True)
class SplitRules:
    """An ordered list of split rules and the unit for unmatched definitions."""
    rules: tuple[SplitRule, ...]
    default_unit: str | None = None


def load_rules(source: Path | dict[str, Any]) -> SplitRules:
    """Load split rules from a YAML file or an already-parsed mapping."""
    if isinstance(source, Path):
        try:
            data = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SplitError(f"cannot read split rules '{source}': {exc}") from exc
    else:
        data = source
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise SplitError("split rules must be a mapping with a 'rules' list")
    unknown = set(data) - {"rules", "default_unit"}
    if unknown:
        raise SplitError(f"unknown split-rules keys: {', '.join(sorted(unknown))}")

    rules: list[SplitRule] = []
    for i, entry in enumerate(data["rules"], start=1):
        if not isinstance(entry, dict) or "match" not in entry:
            raise SplitError(f"rule {i}: a rule is a mapping with a 'match' pattern")
        unknown = set(entry) - _RULE_KEYS
        if unknown:
            raise SplitError(f"rule {i}: unknown keys: {', '.join(sorted(unknown))}")
        try:
            pattern = re.compile(entry["match"])
        except re.error as exc:
            raise SplitError(f"rule {i}: invalid pattern {entry['match']!r}: {exc}") from exc
        skip = bool(entry.get("skip", False))
        unit = entry.get("unit")
        if not skip and unit is None and "unit" not in pattern.groupindex:
            raise SplitError(f"rule {i}: give a 'unit' or a 'unit' named group in the pattern")
        rules.append(SplitRule(pattern, unit, entry.get("namespace"), skip))
    return SplitRules(tuple(rules), data.get("default_unit"))


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class Unit:
    """One output unit: an in-memory schema document converted to one TOSCA file."""
    name: str
    namespace: str
    document: dict[str, Any] = field(default_factory=lambda: {"definitions": {}})
    node_type_names: set[str] = field(default_factory=set)

    @property
    def filename(self) -> str:
        """The in-memory schema filename other units reference."""
        return f"{self.name}.json"

    @property
    def tosca_file(self) -> str:
        """The TOSCA file the unit is written to."""
        return f"{self.name}.yaml"


@dataclass
class SplitResult:
    """The units of a split, in import order (every unit after the units it imports)."""
    units: list[Unit]
    warnings: list[str] = field(default_factory=list)

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return {u.filename: u.document for u in self.units}

    @property
    def namespaces(self) -> dict[str, str]:
        return {u.filename: u.namespace for u in self.units}

    @property
    def node_types_by_file(self) -> dict[str, set[str]]:
        return {u.filename: u.node_type_names for u in self.units}


# ── Splitting ─────────────────────────────────────────────────────────────────

@dataclass
class _Assignment:
    unit: str
    type_name: str
    namespace: str | None
    rank: tuple[int, int]   # (rule index, first-seen index): orders units for merges


def _assign(definitions: dict[str, Any], rules: SplitRules) -> tuple[dict[str, _Assignment], set[str]]:
    """Apply the rules to every definition name; return assignments and skipped names."""
    assigned: dict[str, _Assignment] = {}
    skipped: set[str] = set()
    unmatched: list[str] = []
    for seen, def_name in enumerate(definitions):
        for index, rule in enumerate(rules.rules):
            m = rule.pattern.fullmatch(def_name)
            if not m:
                continue
            if rule.skip:
                skipped.add(def_name)
                break
            groups = m.groupdict()
            unit = rule.unit if rule.unit is not None else groups.get("unit")
            if not unit:
                raise SplitError(f"definition '{def_name}': rule {index + 1} gives no unit")
            type_name = groups.get("name") or def_name
            assigned[def_name] = _Assignment(unit, _sanitize(type_name), rule.namespace, (index, seen))
            break
        else:
            if rules.default_unit is None:
                unmatched.append(def_name)
            else:
                assigned[def_name] = _Assignment(
                    rules.default_unit, _sanitize(def_name), None, (len(rules.rules), seen)
                )
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        raise SplitError(
            f"{len(unmatched)} definition(s) match no rule and there is no default_unit: {shown}"
        )
    return assigned, skipped


def skipped_definitions(root_schema: dict[str, Any], rules: SplitRules) -> set[str]:
    """The definition names of *root_schema* that *rules* skip."""
    return _assign(root_schema.get("definitions") or {}, rules)[1]


def _walk_refs(obj: Any) -> Iterable[str]:
    """Yield every ``$ref`` string in *obj*."""
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in obj.values():
            yield from _walk_refs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_refs(item)


def _target(ref: str) -> tuple[str, str] | None:
    """Split a local definition ref into (definition name, remaining fragment)."""
    if not ref.startswith(_DEF_PREFIX):
        return None
    name, _, rest = ref[len(_DEF_PREFIX):].partition("/")
    return name, (f"/{rest}" if rest else "")


def _edges(
    definitions: dict[str, Any], assigned: dict[str, _Assignment]
) -> dict[tuple[str, str], tuple[str, str]]:
    """Unit graph edges, each with one (source definition, target definition) creating it."""
    edges: dict[tuple[str, str], tuple[str, str]] = {}
    for def_name, a in assigned.items():
        for ref in _walk_refs(definitions[def_name]):
            t = _target(ref)
            if t is None or t[0] not in assigned:
                continue
            target_unit = assigned[t[0]].unit
            if target_unit != a.unit:
                edges.setdefault((a.unit, target_unit), (def_name, t[0]))
    return edges


def _strongly_connected(nodes: list[str], edges: Collection[tuple[str, str]]) -> list[list[str]]:
    """Tarjan's algorithm: strongly connected components, each listed in *nodes* order."""
    succ: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        succ[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = 0

    def visit(v: str) -> None:
        nonlocal counter
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in succ[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            order = {n: i for i, n in enumerate(nodes)}
            components.append(sorted(component, key=order.__getitem__))

    for n in nodes:
        if n not in index:
            visit(n)
    return components


def _unit_order(assigned: dict[str, _Assignment]) -> list[str]:
    """Units in rule order: the rule that first produced them, then first appearance."""
    rank: dict[str, tuple[int, int]] = {}
    for a in assigned.values():
        if a.unit not in rank or a.rank < rank[a.unit]:
            rank[a.unit] = a.rank
    return sorted(rank, key=rank.__getitem__)


def _resolve_cycles(
    definitions: dict[str, Any], assigned: dict[str, _Assignment], merge_cycles: bool
) -> None:
    """Fail on import cycles between units, or merge each cycle into one unit."""
    while True:
        units = _unit_order(assigned)
        edges = _edges(definitions, assigned)
        cycles = [c for c in _strongly_connected(units, edges) if len(c) > 1]
        if not cycles:
            return
        if not merge_cycles:
            lines = []
            for cycle in cycles:
                members = set(cycle)
                lines.append(f"  units {', '.join(cycle)}:")
                for (a, b), (src, dst) in sorted(edges.items()):
                    if a in members and b in members:
                        lines.append(f"    {a} -> {b}: {src} references {dst}")
            raise SplitError(
                "the units import each other in a cycle (use --merge-cycles to merge them):\n"
                + "\n".join(lines)
            )
        for cycle in cycles:
            keep, merged = cycle[0], set(cycle[1:])
            for a in assigned.values():
                if a.unit in merged:
                    a.unit = keep


def _check(assigned: dict[str, _Assignment], reserved_namespaces: Collection[str]) -> dict[str, str]:
    """Check names and collisions; return the namespace of every unit."""
    namespaces: dict[str, str] = {}
    sources: dict[tuple[str, str], list[str]] = {}
    for def_name, a in assigned.items():
        sources.setdefault((a.unit, a.type_name), []).append(def_name)
        if a.namespace is not None:
            if namespaces.get(a.unit, a.namespace) != a.namespace:
                raise SplitError(
                    f"unit '{a.unit}' is given two namespaces: "
                    f"'{namespaces[a.unit]}' and '{a.namespace}'"
                )
            namespaces[a.unit] = a.namespace

    clashes = [
        f"  {unit}:{type_name} from {', '.join(defs)}"
        for (unit, type_name), defs in sources.items() if len(defs) > 1
    ]
    if clashes:
        raise SplitError("two or more definitions get the same type name in a unit:\n" + "\n".join(clashes))

    units = {a.unit for a in assigned.values()}
    for unit in units:
        namespace = namespaces.setdefault(unit, unit)
        if not _NAME_RE.match(unit):
            raise SplitError(f"unit name '{unit}' is not a valid TOSCA name")
        if _VERSIONED_RE.match(unit):
            raise SplitError(f"unit name '{unit}' looks like a versioned schema file name")
        if not _NAME_RE.match(namespace):
            raise SplitError(f"namespace '{namespace}' of unit '{unit}' is not a valid TOSCA name")
        if namespace in reserved_namespaces:
            raise SplitError(f"namespace '{namespace}' of unit '{unit}' is reserved")
    return namespaces


def _rewrite(
    obj: Any,
    unit: str,
    assigned: dict[str, _Assignment],
    skipped: set[str],
    definitions: dict[str, Any],
) -> Any:
    """Rewrite definition refs to unit refs; replace refs to skipped definitions."""
    if isinstance(obj, list):
        return [_rewrite(item, unit, assigned, skipped, definitions) for item in obj]
    if not isinstance(obj, dict):
        return obj
    ref = obj.get("$ref")
    t = _target(ref) if isinstance(ref, str) else None
    if t is not None and t[0] in skipped:
        # Fall back to the referenced schema's JSON type, as for an unresolvable $ref.
        fallback = {k: v for k, v in obj.items() if k != "$ref"}
        target_type = definitions.get(t[0], {}).get("type") if isinstance(definitions.get(t[0]), dict) else None
        fallback.setdefault("type", target_type or "object")
        return fallback
    out = {k: _rewrite(v, unit, assigned, skipped, definitions) for k, v in obj.items() if k != "$ref"}
    if isinstance(ref, str):
        if t is not None and t[0] in assigned:
            a = assigned[t[0]]
            local = f"{_DEF_PREFIX}{a.type_name}{t[1]}"
            out["$ref"] = local if a.unit == unit else f"{a.unit}.json{local}"
        else:
            out["$ref"] = ref
        # Keep $ref first, as written.
        out = {"$ref": out.pop("$ref"), **out}
    return out


def _skipped_refs(
    definitions: dict[str, Any], assigned: dict[str, _Assignment], skipped: set[str]
) -> list[str]:
    """Describe every reference from a kept definition to a skipped one."""
    found = []
    for def_name in assigned:
        targets = sorted({t[0] for ref in _walk_refs(definitions[def_name])
                          if (t := _target(ref)) is not None and t[0] in skipped})
        found.extend(f"  {def_name} references {target}" for target in targets)
    return found


def _topological(units: list[str], edges: Collection[tuple[str, str]]) -> list[str]:
    """Order units so that every unit comes after the units it imports (graph is acyclic)."""
    deps: dict[str, set[str]] = {u: set() for u in units}
    for a, b in edges:
        deps[a].add(b)
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(units):
        ready = [u for u in units if u not in done and deps[u] <= done]
        ordered.extend(ready)
        done.update(ready)
    return ordered


def split_schema(
    root_schema: dict[str, Any],
    rules: SplitRules,
    node_type_names: Collection[str] = (),
    *,
    merge_cycles: bool = False,
    allow_skipped_refs: bool = False,
    reserved_namespaces: Collection[str] = (),
) -> SplitResult:
    """
    Split the definitions of *root_schema* into units.

    Parameters
    ----------
    root_schema:
        A normalized root schema whose definitions are under ``definitions``.
    rules:
        The split rules.
    node_type_names:
        Definition names (as in *root_schema*) that are node types.
    merge_cycles:
        Merge units that import each other in a cycle instead of failing.
    allow_skipped_refs:
        Warn instead of failing when a kept definition references a skipped one.
    reserved_namespaces:
        Namespaces no unit may take.

    Raises
    ------
    SplitError
        When the rules cannot be applied (see ``docs/multi-output.md``, *Checks*).
    """
    definitions: dict[str, Any] = root_schema.get("definitions") or {}
    assigned, skipped = _assign(definitions, rules)

    warnings: list[str] = []
    skipped_refs = _skipped_refs(definitions, assigned, skipped)
    if skipped_refs:
        message = "kept definitions reference skipped ones:\n" + "\n".join(skipped_refs)
        if not allow_skipped_refs:
            raise SplitError(message + "\n(use --allow-skipped-refs to convert them anyway)")
        warnings.append(message)

    _resolve_cycles(definitions, assigned, merge_cycles)
    namespaces = _check(assigned, reserved_namespaces)

    units = {name: Unit(name, namespaces[name]) for name in _unit_order(assigned)}
    node_types = set(node_type_names)
    for def_name, a in assigned.items():
        unit = units[a.unit]
        unit.document["definitions"][a.type_name] = _rewrite(
            copy.deepcopy(definitions[def_name]), a.unit, assigned, skipped, definitions
        )
        if def_name in node_types:
            unit.node_type_names.add(a.type_name)

    order = _topological(list(units), _edges(definitions, assigned).keys())
    return SplitResult([units[name] for name in order], warnings)
