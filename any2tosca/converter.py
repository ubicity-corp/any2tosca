"""
converter.py
------------
Core logic for converting JSON Schema documents to TOSCA data types.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from .yaml_utils import FoldedStr

__all__ = ["Converter", "ConversionResult", "convert_schema", "build_tosca_document"]


class ConversionResult(NamedTuple):
    """Holds the output of a schema conversion."""
    data_types: dict[str, Any]
    node_types: dict[str, Any]
    imports: dict[str, str] = {}  # namespace → tosca_file


class Converter:
    """Converts JSON Schema documents to TOSCA data_types."""

    JSON_SCHEMA_TO_TOSCA_TYPE: dict[str, str] = {
        "string":  "string",
        "integer": "integer",
        "number":  "float",
        "boolean": "boolean",
        "object":  "map",
        "array":   "list",
        "null":    "string",   # TOSCA has no null; fall back to string
    }

    def __init__(self) -> None:
        self._base_path: Path | None = None
        self._imports: dict[str, str] = {}      # namespace → tosca_file
        self._registry: Registry | None = None  # referencing Registry
        self._id_to_path: dict[str, Path] = {}  # $id URI → local Path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def sanitize_name(self, name: str) -> str:
        """Make a string safe for use as a TOSCA type / property name."""
        return name.replace(" ", "_").replace("-", "_").replace(".", "_")

    def _get_registry(self) -> tuple[Registry, dict[str, Path]]:
        """
        Lazily build a ``referencing.Registry`` from all ``.json`` files in
        ``_base_path`` that declare a ``$id``.  Also returns a
        ``{$id URI → local Path}`` mapping used for namespace derivation.
        Reset in ``convert_schema`` when ``_base_path`` changes.
        """
        if self._registry is not None:
            return self._registry, self._id_to_path
        self._id_to_path = {}
        resources: list[tuple[str, Resource]] = []
        if self._base_path is not None:
            for json_file in self._base_path.glob("*.json"):
                try:
                    with open(json_file, encoding="utf-8") as fh:
                        schema = json.load(fh)
                    if isinstance(schema, dict) and "$id" in schema:
                        uri = schema["$id"]
                        self._id_to_path[uri] = json_file
                        resources.append((uri, Resource.from_contents(schema, default_specification=DRAFT7)))
                except (OSError, json.JSONDecodeError):
                    pass
        self._registry = Registry().with_resources(resources)
        return self._registry, self._id_to_path

    def _parse_ref(self, ref: str) -> tuple[str | None, str]:
        """
        Split a ``$ref`` into ``(file_part, type_name)``.

        Returns ``(None, type_name)`` for local refs (``#/...``).
        Returns ``(filename, type_name)`` for relative or URI file refs.
        URI refs are resolved via the ``$id`` registry; unmatched URIs
        return ``(None, "")``.
        """
        if ref.startswith("#"):
            return None, self.sanitize_name(ref.split("/")[-1])

        if "://" in ref:
            uri_no_frag, _, fragment = ref.partition("#")
            _, id_to_path = self._get_registry()
            file_path = id_to_path.get(uri_no_frag)
            if file_path is None:
                return None, ""
            type_name = self.sanitize_name(fragment.split("/")[-1]) if fragment else self.sanitize_name(file_path.stem)
            return file_path.name, type_name

        file_part, _, fragment = ref.partition("#")
        if not file_part:
            return None, ""
        type_name = self.sanitize_name(fragment.split("/")[-1]) if fragment else self.sanitize_name(Path(file_part).stem)
        return file_part, type_name

    def _namespace_for(self, file_part: str) -> str:
        """
        Derive a short TOSCA namespace identifier (≤8 chars, lowercase) from
        a JSON Schema filename.

        Strategy:
        1. Strip version suffix (e.g. ``.v1_0_0``).
        2. Split PascalCase / separator-delimited string into words.
        3. If the joined words fit in 8 chars, return them joined.
        4. Otherwise contract each word by removing non-leading vowels.
        5. If still >8 chars, distribute 8 chars proportionally across words.
        """
        stem = Path(file_part).stem
        base = re.sub(r"[._]v\d+(?:[._]\d+)*$", "", stem) or stem

        # Split on PascalCase boundaries and non-alphanumeric separators
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)
        spaced = re.sub(r"[^a-zA-Z0-9]+", " ", spaced)
        words = [w.lower() for w in spaced.split() if w]
        if not words:
            words = [base.lower()]

        joined = "".join(words)
        if len(joined) <= 8:
            return joined

        # Contract: remove non-leading vowels from each word
        def _contract(w: str) -> str:
            return w[0] + re.sub(r"[aeiou]", "", w[1:]) if len(w) > 1 else w

        contracted = [_contract(w) for w in words]
        result = "".join(contracted)
        if len(result) <= 8:
            return result

        # Still too long: distribute 8 chars proportionally across contracted words
        n = len(contracted)
        base_len = 8 // n
        extra = 8 % n
        return "".join(w[:base_len + (1 if i < extra else 0)] for i, w in enumerate(contracted))

    def _tosca_file_for(self, file_part: str) -> str:
        """Derive the expected TOSCA output filename for a JSON Schema file."""
        return Path(file_part).stem + ".tosca.yaml"

    def _ref_to_tosca_type(self, ref: str) -> str:
        """
        Convert a ``$ref`` to a TOSCA type name.

        External file refs are registered in ``self._imports`` and returned
        as ``namespace:TypeName``; local refs are returned as plain type names.
        """
        file_part, type_name = self._parse_ref(ref)
        if not type_name:
            return "string"  # unresolvable external URI
        if file_part is None:
            return type_name  # local ref
        namespace = self._namespace_for(file_part)
        self._imports[namespace] = self._tosca_file_for(file_part)
        return f"{namespace}:{type_name}"

    def resolve_ref(self, ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve a ``$ref`` to its schema dict.

        Local ``#/...`` refs are resolved directly against ``root_schema``.
        External refs (relative or absolute URI) are resolved via the
        ``referencing`` registry, using the root schema's ``$id`` as the
        base URI so that relative refs resolve correctly.
        """
        if ref.startswith("#/"):
            parts = ref.lstrip("#/").split("/")
            node: Any = root_schema
            for part in parts:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return {}
            return node if isinstance(node, dict) else {}

        if self._base_path is None:
            return {}

        registry, _ = self._get_registry()
        base_uri = root_schema.get("$id", "")
        try:
            resolver = registry.resolver(base_uri=base_uri) if base_uri else registry.resolver()
            contents = resolver.lookup(ref).contents
            return contents if isinstance(contents, dict) else {}
        except Exception:
            return {}

    def get_tosca_type(
        self,
        json_type: str | list[str] | None,
        schema_fragment: dict[str, Any],
        root_schema: dict[str, Any],
    ) -> str:
        """Return the TOSCA primitive type string for a JSON Schema ``type`` value."""
        if isinstance(json_type, list):
            json_type = next((t for t in json_type if t != "null"), json_type[0])
        if json_type == "array":
            return "list"
        return self.JSON_SCHEMA_TO_TOSCA_TYPE.get(json_type or "string", "string")

    def build_validation(self, schema_fragment: dict[str, Any]) -> dict[str, Any] | None:
        """Build a TOSCA 2.0 ``validation`` expression from JSON Schema validation keywords."""
        clauses: list[dict[str, Any]] = []

        if "minimum" in schema_fragment:
            clauses.append({"$greater_or_equal": ["$value", schema_fragment["minimum"]]})
        if "exclusiveMinimum" in schema_fragment:
            clauses.append({"$greater_than": ["$value", schema_fragment["exclusiveMinimum"]]})
        if "maximum" in schema_fragment:
            clauses.append({"$less_or_equal": ["$value", schema_fragment["maximum"]]})
        if "exclusiveMaximum" in schema_fragment:
            clauses.append({"$less_than": ["$value", schema_fragment["exclusiveMaximum"]]})
        if "minLength" in schema_fragment:
            clauses.append({"$greater_or_equal": [{"$length": ["$value"]}, schema_fragment["minLength"]]})
        if "maxLength" in schema_fragment:
            clauses.append({"$less_or_equal": [{"$length": ["$value"]}, schema_fragment["maxLength"]]})
        if "pattern" in schema_fragment:
            clauses.append({"$matches": ["$value", schema_fragment["pattern"]]})
        if "enum" in schema_fragment:
            clauses.append({"$valid_values": ["$value", schema_fragment["enum"]]})

        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def _merge_allof(
        self,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """
        Flatten ``allOf`` into a single merged schema.

        Returns ``(merged_schema, derived_from)`` where ``derived_from`` is the
        TOSCA type name of a pure ``$ref`` sub-schema (inheritance), or ``None``.
        """
        if "allOf" not in schema:
            return schema, None

        merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
        merged_props: dict[str, Any] = dict(merged.pop("properties", {}))
        merged_required: list[str] = list(merged.pop("required", []))
        derived_from: str | None = None

        for sub in schema["allOf"]:
            if "$ref" in sub and len(sub) == 1:
                # Pure $ref → treat as base type (inheritance)
                derived_from = self._ref_to_tosca_type(sub["$ref"])
            else:
                resolved = self.resolve_ref(sub["$ref"], root_schema) if "$ref" in sub else sub
                merged_props.update(resolved.get("properties", {}))
                merged_required.extend(resolved.get("required", []))
                for key in ("description", "title", "type"):
                    if key in resolved and key not in merged:
                        merged[key] = resolved[key]

        if merged_props:
            merged["properties"] = merged_props
        if merged_required:
            # deduplicate while preserving order
            seen: set[str] = set()
            merged["required"] = [r for r in merged_required if not (r in seen or seen.add(r))]  # type: ignore[func-returns-value]

        return merged, derived_from

    def collect_definitions(self, root_schema: dict[str, Any]) -> dict[str, Any]:
        """Collect all named sub-schemas from ``definitions`` and ``$defs``."""
        defs: dict[str, Any] = {}
        defs.update(root_schema.get("definitions", {}))
        defs.update(root_schema.get("$defs", {}))
        return defs

    # ── Core conversion ───────────────────────────────────────────────────────

    def schema_to_property(
        self,
        prop_name: str,
        prop_schema: dict[str, Any],
        required_props: list[str],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a single JSON Schema property definition to a TOSCA property dict."""
        # allOf → merge sub-schemas first
        if "allOf" in prop_schema:
            prop_schema, allof_type = self._merge_allof(prop_schema, root_schema)
            if allof_type and not prop_schema.get("properties"):
                prop_def: dict[str, Any] = {"type": allof_type}
                description = prop_schema.get("description") or prop_schema.get("title")
                if description:
                    prop_def["description"] = FoldedStr(description)
                prop_def["required"] = prop_name in required_props
                if "default" in prop_schema:
                    prop_def["default"] = prop_schema["default"]
                return prop_def
            # otherwise fall through with the merged schema

        # $ref → use the referenced TOSCA data type name directly
        if "$ref" in prop_schema:
            ref_type_name = self._ref_to_tosca_type(prop_schema["$ref"])
            ref_schema = self.resolve_ref(prop_schema["$ref"], root_schema)
            merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
            prop_def: dict[str, Any] = {"type": ref_type_name}
            description = merged.get("description") or merged.get("title")
            if description:
                prop_def["description"] = FoldedStr(description)
            prop_def["required"] = prop_name in required_props
            if "default" in merged:
                prop_def["default"] = merged["default"]
            return prop_def

        json_type = prop_schema.get("type")

        # Inline object with properties → create a named data type and reference it
        if prop_schema.get("properties") and json_type in ("object", None):
            nested_type_name = self.sanitize_name(prop_name)
            data_types[nested_type_name] = self.schema_to_tosca_data_type(
                nested_type_name, prop_schema, root_schema, data_types
            )
            prop_def = {"type": nested_type_name}
            description = prop_schema.get("description") or prop_schema.get("title")
            if description:
                prop_def["description"] = FoldedStr(description)
            prop_def["required"] = prop_name in required_props
            if "default" in prop_schema:
                prop_def["default"] = prop_schema["default"]
            return prop_def

        tosca_type = self.get_tosca_type(json_type, prop_schema, root_schema)
        prop_def = {"type": tosca_type}

        # Description (prefer 'description' over 'title')
        description = prop_schema.get("description") or prop_schema.get("title")
        if description:
            prop_def["description"] = FoldedStr(description)

        prop_def["required"] = prop_name in required_props

        if "default" in prop_schema:
            prop_def["default"] = prop_schema["default"]

        validation = self.build_validation(prop_schema)
        if validation is not None:
            prop_def["validation"] = validation

        # entry_schema for list types
        if tosca_type == "list":
            items = prop_schema.get("items", {})
            if "$ref" in items:
                item_tosca_type = self._ref_to_tosca_type(items["$ref"])
            else:
                item_tosca_type = self.get_tosca_type(items.get("type"), items, root_schema)
            prop_def["entry_schema"] = {"type": item_tosca_type}

        # entry_schema for map types with additionalProperties (no named properties)
        if tosca_type == "map":
            add_props = prop_schema.get("additionalProperties", {})
            if isinstance(add_props, dict):
                if "$ref" in add_props:
                    val_type = self._ref_to_tosca_type(add_props["$ref"])
                else:
                    val_type = self.get_tosca_type(add_props.get("type"), add_props, root_schema)
                prop_def["entry_schema"] = {"type": val_type}

        return prop_def

    def schema_to_tosca_data_type(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a JSON Schema object definition to a TOSCA ``data_types`` entry."""
        schema, derived_from = self._merge_allof(schema, root_schema)

        tosca_type: dict[str, Any] = {}

        description = schema.get("description") or schema.get("title")
        if description:
            tosca_type["description"] = FoldedStr(description)

        if derived_from:
            tosca_type["derived_from"] = derived_from

        properties_schema = schema.get("properties", {})
        required_list: list[str] = schema.get("required", [])

        if properties_schema:
            tosca_type["properties"] = {
                self.sanitize_name(pname): self.schema_to_property(
                    pname, pschema, required_list, root_schema, data_types
                )
                for pname, pschema in properties_schema.items()
            }

        # Non-object named types (e.g. a string with enum constraints)
        json_type = schema.get("type")
        if json_type and json_type != "object" and not properties_schema:
            tosca_type["derived_from"] = self.get_tosca_type(json_type, schema, root_schema)
            validation = self.build_validation(schema)
            if validation is not None:
                tosca_type["validation"] = validation
            tosca_type.pop("properties", None)

        return tosca_type

    def schema_to_tosca_node_type(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a JSON Schema object definition to a TOSCA ``node_types`` entry."""
        schema, derived_from = self._merge_allof(schema, root_schema)

        node_type: dict[str, Any] = {}

        description = schema.get("description") or schema.get("title")
        if description:
            node_type["description"] = FoldedStr(description)

        node_type["derived_from"] = derived_from or "tosca.nodes.Root"

        properties_schema = schema.get("properties", {})
        required_list: list[str] = schema.get("required", [])

        if properties_schema:
            node_type["properties"] = {
                self.sanitize_name(pname): self.schema_to_property(
                    pname, pschema, required_list, root_schema, data_types
                )
                for pname, pschema in properties_schema.items()
            }

        return node_type

    def convert_schema(
        self,
        root_schema: dict[str, Any],
        top_level_name: str,
        node_type_names: frozenset[str] | set[str] = frozenset(),
        base_path: Path | None = None,
    ) -> ConversionResult:
        """
        Convert an entire JSON Schema document to TOSCA types.

        Parameters
        ----------
        root_schema:
            Parsed JSON Schema document (a Python dict).
        top_level_name:
            Name to use for the top-level schema object if it is an ``object`` type.
            Typically derived from the schema's ``title`` field or the file stem.
        base_path:
            Directory containing the schema file.  When provided, external
            ``$ref`` values are resolved relative to this directory and the
            corresponding TOSCA files are recorded as imports.

        Returns
        -------
        ConversionResult
            Named tuple with ``data_types``, ``node_types``, and ``imports`` dicts.
        """
        self._base_path = base_path
        self._registry = None   # rebuild for new base_path
        self._id_to_path = {}
        self._imports = {}
        data_types: dict[str, Any] = {}
        node_types: dict[str, Any] = {}

        def _add(tosca_name: str, schema: dict[str, Any], root: dict[str, Any]) -> None:
            if tosca_name in node_type_names:
                node_types[tosca_name] = self.schema_to_tosca_node_type(
                    tosca_name, schema, root, data_types
                )
            else:
                data_types[tosca_name] = self.schema_to_tosca_data_type(
                    tosca_name, schema, root, data_types
                )

        # Convert named definitions / $defs first
        for def_name, def_schema in self.collect_definitions(root_schema).items():
            if not isinstance(def_schema, dict):
                continue
            _add(self.sanitize_name(def_name), def_schema, root_schema)

        # Convert the top-level schema if it is an object
        if root_schema.get("type") == "object" or root_schema.get("properties"):
            _add(self.sanitize_name(top_level_name), root_schema, root_schema)

        return ConversionResult(data_types=data_types, node_types=node_types, imports=dict(self._imports))

    def build_tosca_document(
        self,
        data_types: dict[str, Any],
        node_types: dict[str, Any] | None = None,
        imports: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Wrap type dicts in a full TOSCA service-template skeleton."""
        doc: dict[str, Any] = {
            "tosca_definitions_version": "tosca_2_0",
            "description": FoldedStr("Types generated from JSON Schema by any2tosca"),
        }
        if imports:
            doc["imports"] = [{"file": file, "namespace": ns} for ns, file in imports.items()]
        if data_types:
            doc["data_types"] = data_types
        if node_types:
            doc["node_types"] = node_types
        return doc


# ── Module-level convenience functions ────────────────────────────────────────

def convert_schema(
    root_schema: dict[str, Any],
    top_level_name: str,
    node_type_names: frozenset[str] | set[str] = frozenset(),
    base_path: Path | None = None,
) -> ConversionResult:
    return Converter().convert_schema(root_schema, top_level_name, node_type_names, base_path)


def build_tosca_document(
    data_types: dict[str, Any],
    node_types: dict[str, Any] | None = None,
    imports: dict[str, str] | None = None,
) -> dict[str, Any]:
    return Converter().build_tosca_document(data_types, node_types, imports)
