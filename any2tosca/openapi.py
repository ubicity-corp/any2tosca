"""
openapi.py
----------
Pre-processor that extracts JSON Schema from OpenAPI 2.0 and 3.x documents
and identifies which schemas should become TOSCA node types.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import copy
from typing import Any

__all__ = ["OpenAPIExtractor"]


class OpenAPIExtractor:
    """
    Extracts and normalizes JSON Schema from an OpenAPI document.

    Usage::

        extractor = OpenAPIExtractor()
        root_schema, node_type_names = extractor.extract(openapi_doc)
        result = Converter().convert_schema(root_schema, "api", node_type_names)
    """

    def extract(self, openapi_doc: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        """
        Convert an OpenAPI document into a normalized JSON Schema dict and
        a set of schema names that should become TOSCA node types.

        Schemas referenced in POST request bodies are classified as node types.

        Returns
        -------
        tuple[dict, set[str]]
            ``(root_schema, node_type_names)`` ready to pass to
            ``Converter.convert_schema()``.
        """
        version = str(openapi_doc.get("openapi", openapi_doc.get("swagger", "")))
        if version.startswith("3."):
            return self._extract_3x(openapi_doc)
        return self._extract_2x(openapi_doc)

    # ── OpenAPI 3.x ───────────────────────────────────────────────────────────

    def _extract_3x(self, doc: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        schemas = copy.deepcopy(doc.get("components", {}).get("schemas", {}))
        for name in list(schemas):
            schemas[name] = self._normalize_schema(schemas[name])

        # Rewrite all $refs before handing off to the converter
        root_schema: dict[str, Any] = self._rewrite_refs_3x({"definitions": schemas})

        node_type_names = self._post_schema_names_3x(doc)
        return root_schema, node_type_names

    def _rewrite_refs_3x(self, obj: Any) -> Any:
        """Rewrite ``#/components/schemas/X`` refs to ``#/definitions/X``."""
        if isinstance(obj, dict):
            return {
                k: (
                    "#/definitions/" + v.split("/")[-1]
                    if k == "$ref" and isinstance(v, str)
                    and v.startswith("#/components/schemas/")
                    else self._rewrite_refs_3x(v)
                )
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._rewrite_refs_3x(item) for item in obj]
        return obj

    def _post_schema_names_3x(self, doc: dict[str, Any]) -> set[str]:
        """Return schema names used as POST requestBody schemas (OpenAPI 3.x)."""
        names: set[str] = set()
        for path_item in doc.get("paths", {}).values():
            post = path_item.get("post", {})
            for media in post.get("requestBody", {}).get("content", {}).values():
                ref = media.get("schema", {}).get("$ref", "")
                if ref.startswith("#/components/schemas/"):
                    names.add(ref.split("/")[-1])
        return names

    # ── OpenAPI 2.0 (Swagger) ─────────────────────────────────────────────────

    def _extract_2x(self, doc: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        schemas = copy.deepcopy(doc.get("definitions", {}))
        for name in list(schemas):
            schemas[name] = self._normalize_schema(schemas[name])

        root_schema: dict[str, Any] = {"definitions": schemas}
        node_type_names = self._post_schema_names_2x(doc)
        return root_schema, node_type_names

    def _post_schema_names_2x(self, doc: dict[str, Any]) -> set[str]:
        """Return schema names used as POST body parameters (OpenAPI 2.0)."""
        names: set[str] = set()
        for path_item in doc.get("paths", {}).values():
            for param in path_item.get("post", {}).get("parameters", []):
                if param.get("in") == "body":
                    ref = param.get("schema", {}).get("$ref", "")
                    if ref.startswith("#/definitions/"):
                        names.add(ref.split("/")[-1])
        return names

    # ── Schema normalization ───────────────────────────────────────────────────

    def _normalize_schema(self, schema: Any) -> Any:
        """Recursively normalize a schema fragment to plain JSON Schema."""
        if not isinstance(schema, dict):
            return schema

        # Flatten anyOf/oneOf where the only variation is a null alternative
        for kw in ("anyOf", "oneOf"):
            if kw in schema:
                non_null = [
                    s for s in schema[kw]
                    if not (isinstance(s, dict) and s.get("type") == "null")
                ]
                if len(non_null) == 1 and len(schema[kw]) > len(non_null):
                    merged = {**non_null[0], **{k: v for k, v in schema.items() if k != kw}}
                    return self._normalize_schema(merged)

        # Drop OpenAPI 3.0 nullable extension (TOSCA has no null type)
        schema = {k: v for k, v in schema.items() if k != "nullable"}

        # Recurse into sub-schemas
        if "properties" in schema:
            schema["properties"] = {
                k: self._normalize_schema(v) for k, v in schema["properties"].items()
            }
        if "items" in schema:
            schema["items"] = self._normalize_schema(schema["items"])
        if isinstance(schema.get("additionalProperties"), dict):
            schema["additionalProperties"] = self._normalize_schema(schema["additionalProperties"])

        return schema
