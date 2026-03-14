"""
redfish.py
----------
Pre-processor that normalizes Redfish JSON Schema for use with the Converter.
"""

from __future__ import annotations

import copy
import re
from typing import Any

__all__ = ["RedfishNormalizer"]


class RedfishNormalizer:
    """
    Normalizes a Redfish JSON Schema document into plain JSON Schema.

    Redfish quirks handled:

    - The top-level ``$ref`` identifies the resource type → becomes a node type.
    - ``anyOf: [{$ref: ...}, {type: null}]`` → flattened to the non-null schema.
    - ``@odata.*`` and ``@Redfish.*`` properties are dropped.
    - ``x-longDescription`` is promoted to ``description`` when no description
      is present.
    - All other ``x-*`` extension keywords are dropped.
    - External ``$ref`` URIs are left as-is; the converter treats them as
      unresolvable and skips them gracefully.

    Usage::

        normalizer = RedfishNormalizer()
        root_schema, node_type_names = normalizer.normalize(redfish_schema)
        result = Converter().convert_schema(root_schema, "api", node_type_names)
    """

    #: Property name prefixes that are Redfish/OData annotations, not data.
    EXCLUDED_PREFIXES: tuple[str, ...] = ("@odata.", "@Redfish.")

    def normalize(self, redfish_schema: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        """
        Normalize a Redfish schema into a plain JSON Schema dict and
        a set of definition names that should become TOSCA node types.

        Two schema layouts are handled:

        - **Definitions-based** (typical): a top-level ``$ref`` names the
          resource; all types live under ``definitions``.
        - **Flat** (e.g. ``Resource.json``): properties are defined directly
          at the top level with no ``definitions`` section. The resource name
          is derived from the ``title`` field.

        Returns
        -------
        tuple[dict, set[str]]
            ``(root_schema, node_type_names)`` ready to pass to
            ``Converter.convert_schema()``.
        """
        resource_name = self._resource_name(redfish_schema) or self._title_name(redfish_schema)

        definitions: dict[str, Any] = {}
        for name, schema in redfish_schema.get("definitions", {}).items():
            if isinstance(schema, dict):
                definitions[name] = self._normalize_schema(schema)

        if definitions:
            root_schema: dict[str, Any] = {"definitions": definitions}
        else:
            # Flat schema: properties live at the top level — normalize directly.
            _skip = {"$schema", "$id", "$ref", "title", "definitions", "$defs"}
            root_schema = self._normalize_schema(
                {k: v for k, v in redfish_schema.items() if k not in _skip}
            )

        node_type_names: set[str] = {resource_name} if resource_name else set()
        return root_schema, node_type_names

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resource_name(self, redfish_schema: dict[str, Any]) -> str | None:
        """Return the definition name pointed to by the top-level ``$ref``."""
        ref = redfish_schema.get("$ref", "")
        if ref.startswith("#/definitions/"):
            return ref.split("/")[-1]
        return None

    #: Matches Redfish version suffix segments like ``v1_7_0`` or ``v1.7.0``.
    _VER_SEGMENT = re.compile(r"^v\d+[._]\d+[._]\d+")

    def _title_name(self, redfish_schema: dict[str, Any]) -> str | None:
        """
        Derive a type name from the ``title`` field.

        Redfish titles look like ``"#Resource.Resource"``, ``"#ProcessorCollection.ProcessorCollection"``,
        or ``"#Redundancy.v1_7_0"`` (versioned).  We strip the leading ``#``,
        split on ``.``, skip any trailing version segment, then return the last segment.
        """
        title = redfish_schema.get("title", "").lstrip("#")
        if not title:
            return None
        parts = title.split(".")
        # Drop a trailing version segment (e.g. "v1_7_0") to get the base type name.
        if len(parts) >= 2 and self._VER_SEGMENT.match(parts[-1]):
            parts = parts[:-1]
        return parts[-1] or None

    def _normalize_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Normalize a definition schema."""
        schema = copy.deepcopy(schema)

        # Promote x-longDescription to description
        if "description" not in schema and "x-longDescription" in schema:
            schema["description"] = schema["x-longDescription"]

        # Drop all x-* extension keywords
        schema = {k: v for k, v in schema.items() if not k.startswith("x-")}

        if "properties" in schema:
            schema["properties"] = self._normalize_properties(schema["properties"])
            if not schema["properties"]:
                del schema["properties"]

        if "items" in schema:
            schema["items"] = self._normalize_property_schema(schema["items"])

        if isinstance(schema.get("additionalProperties"), dict):
            schema["additionalProperties"] = self._normalize_property_schema(
                schema["additionalProperties"]
            )

        return schema

    def _normalize_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        """Filter and normalize a properties dict."""
        result: dict[str, Any] = {}
        for name, prop_schema in properties.items():
            if any(name.startswith(prefix) for prefix in self.EXCLUDED_PREFIXES):
                continue
            result[name] = self._normalize_property_schema(prop_schema)
        return result

    def _normalize_property_schema(self, schema: Any) -> Any:
        """Normalize a property schema, flattening anyOf/null patterns."""
        if not isinstance(schema, dict):
            return schema

        # Flatten anyOf: [{...}, {type: null}] → the non-null alternative
        if "anyOf" in schema:
            non_null = [
                s for s in schema["anyOf"]
                if not (isinstance(s, dict) and s.get("type") == "null")
            ]
            if len(non_null) == 1:
                merged = {**non_null[0], **{k: v for k, v in schema.items() if k != "anyOf"}}
                return self._normalize_property_schema(merged)

        # Promote x-longDescription
        if "description" not in schema and "x-longDescription" in schema:
            schema = dict(schema)
            schema["description"] = schema["x-longDescription"]

        # Drop x-* extensions
        schema = {k: v for k, v in schema.items() if not k.startswith("x-")}

        # Recurse into nested properties and array items
        if "properties" in schema:
            schema["properties"] = self._normalize_properties(schema["properties"])

        if "items" in schema:
            schema["items"] = self._normalize_property_schema(schema["items"])

        return schema
