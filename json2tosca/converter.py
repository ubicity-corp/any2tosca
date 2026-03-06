"""
converter.py
------------
Core logic for converting JSON Schema documents to TOSCA data types.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Converter", "convert_schema", "build_tosca_document"]


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

    # ── Internal helpers ──────────────────────────────────────────────────────

    def sanitize_name(self, name: str) -> str:
        """Make a string safe for use as a TOSCA type / property name."""
        return name.replace(" ", "_").replace("-", "_").replace(".", "_")

    def resolve_ref(self, ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
        """
        Minimal local ``$ref`` resolver.
        Only handles ``#/definitions/<name>`` and ``#/$defs/<name>`` patterns.
        """
        if not ref.startswith("#/"):
            return {}
        parts = ref.lstrip("#/").split("/")
        node: Any = root_schema
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node if isinstance(node, dict) else {}

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

    def build_constraints(self, schema_fragment: dict[str, Any]) -> list[dict[str, Any]]:
        """Build a TOSCA ``constraints`` list from JSON Schema validation keywords."""
        constraints: list[dict[str, Any]] = []
        mapping = [
            ("minimum",          "greater_or_equal"),
            ("exclusiveMinimum", "greater_than"),
            ("maximum",          "less_or_equal"),
            ("exclusiveMaximum", "less_than"),
            ("minLength",        "min_length"),
            ("maxLength",        "max_length"),
            ("pattern",          "pattern"),
        ]
        for js_kw, tosca_kw in mapping:
            if js_kw in schema_fragment:
                constraints.append({tosca_kw: schema_fragment[js_kw]})
        if "enum" in schema_fragment:
            constraints.append({"valid_values": schema_fragment["enum"]})
        return constraints

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
        # $ref → use the referenced TOSCA data type name directly
        if "$ref" in prop_schema:
            ref_type_name = self.sanitize_name(prop_schema["$ref"].split("/")[-1])
            ref_schema = self.resolve_ref(prop_schema["$ref"], root_schema)
            merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
            prop_def: dict[str, Any] = {"type": ref_type_name}
            description = merged.get("description") or merged.get("title")
            if description:
                prop_def["description"] = description
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
                prop_def["description"] = description
            prop_def["required"] = prop_name in required_props
            if "default" in prop_schema:
                prop_def["default"] = prop_schema["default"]
            return prop_def

        tosca_type = self.get_tosca_type(json_type, prop_schema, root_schema)
        prop_def = {"type": tosca_type}

        # Description (prefer 'description' over 'title')
        description = prop_schema.get("description") or prop_schema.get("title")
        if description:
            prop_def["description"] = description

        prop_def["required"] = prop_name in required_props

        if "default" in prop_schema:
            prop_def["default"] = prop_schema["default"]

        constraints = self.build_constraints(prop_schema)
        if constraints:
            prop_def["constraints"] = constraints

        # entry_schema for list types
        if tosca_type == "list":
            items = prop_schema.get("items", {})
            if "$ref" in items:
                item_tosca_type = self.sanitize_name(items["$ref"].split("/")[-1])
            else:
                item_tosca_type = self.get_tosca_type(items.get("type"), items, root_schema)
            prop_def["entry_schema"] = {"type": item_tosca_type}

        # entry_schema for map types with additionalProperties (no named properties)
        if tosca_type == "map":
            add_props = prop_schema.get("additionalProperties", {})
            if isinstance(add_props, dict):
                if "$ref" in add_props:
                    val_type = self.sanitize_name(add_props["$ref"].split("/")[-1])
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
        tosca_type: dict[str, Any] = {}

        description = schema.get("description") or schema.get("title")
        if description:
            tosca_type["description"] = description

        tosca_type["derived_from"] = "tosca.datatypes.Root"

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
            constraints = self.build_constraints(schema)
            if constraints:
                tosca_type["constraints"] = constraints
            tosca_type.pop("properties", None)

        return tosca_type

    def convert_schema(self, root_schema: dict[str, Any], top_level_name: str) -> dict[str, Any]:
        """
        Convert an entire JSON Schema document to a dict of TOSCA ``data_types``.

        Parameters
        ----------
        root_schema:
            Parsed JSON Schema document (a Python dict).
        top_level_name:
            Name to use for the top-level schema object if it is an ``object`` type.
            Typically derived from the schema's ``title`` field or the file stem.

        Returns
        -------
        dict
            Mapping of TOSCA type name → TOSCA data type definition dict.
        """
        data_types: dict[str, Any] = {}

        # Convert named definitions / $defs first
        for def_name, def_schema in self.collect_definitions(root_schema).items():
            if not isinstance(def_schema, dict):
                continue
            tosca_name = self.sanitize_name(def_name)
            data_types[tosca_name] = self.schema_to_tosca_data_type(
                tosca_name, def_schema, root_schema, data_types
            )

        # Convert the top-level schema if it is an object
        if root_schema.get("type") == "object" or root_schema.get("properties"):
            safe_top = self.sanitize_name(top_level_name)
            data_types[safe_top] = self.schema_to_tosca_data_type(
                safe_top, root_schema, root_schema, data_types
            )

        return data_types

    def build_tosca_document(self, all_data_types: dict[str, Any]) -> dict[str, Any]:
        """Wrap a ``data_types`` dict in a full TOSCA service-template skeleton."""
        return {
            "tosca_definitions_version": "tosca_simple_yaml_1_3",
            "description": "Data types generated from JSON Schema by json2tosca",
            "data_types": all_data_types,
        }


# ── Module-level convenience functions ────────────────────────────────────────

def convert_schema(root_schema: dict[str, Any], top_level_name: str) -> dict[str, Any]:
    return Converter().convert_schema(root_schema, top_level_name)


def build_tosca_document(all_data_types: dict[str, Any]) -> dict[str, Any]:
    return Converter().build_tosca_document(all_data_types)
