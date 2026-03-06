"""
yang.py
-------
Pre-processor that converts YANG modules to a JSON Schema-like dict
for use with the Converter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pyang.context import Context
from pyang.repository import FileRepository

__all__ = ["YANGNormalizer"]


def _to_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


class YANGNormalizer:
    """
    Converts a YANG module file to a plain JSON Schema-like dict.

    Top-level ``container`` and ``list`` statements become TOSCA node types.
    ``typedef`` statements become TOSCA data types.
    ``grouping`` statements expanded via ``uses`` are inlined automatically
    by pyang during validation.

    Usage::

        normalizer = YANGNormalizer()
        root_schema, node_type_names = normalizer.normalize(Path("server.yang"))
        result = Converter().convert_schema(root_schema, normalizer.module_name,
                                            node_type_names, base_path=path.parent)
    """

    # YANG built-in type names → JSON Schema type strings
    YANG_TO_JSON: dict[str, str] = {
        "string":               "string",
        "boolean":              "boolean",
        "int8":                 "integer",
        "int16":                "integer",
        "int32":                "integer",
        "int64":                "integer",
        "uint8":                "integer",
        "uint16":               "integer",
        "uint32":               "integer",
        "uint64":               "integer",
        "decimal64":            "number",
        "binary":               "string",
        "empty":                "null",
        "anyxml":               "string",
        "anydata":              "string",
        "leafref":              "string",
        "identityref":          "string",
        "instance-identifier":  "string",
        "bits":                 "string",
        "counter32":            "integer",
        "counter64":            "integer",
        "gauge32":              "integer",
        "gauge64":              "integer",
        "timeticks":            "integer",
    }

    def __init__(self) -> None:
        self.module_name: str = ""

    def normalize(self, yang_path: Path) -> tuple[dict[str, Any], set[str]]:
        """
        Parse *yang_path* and return a JSON Schema-like dict plus the set of
        names that should become TOSCA node types.

        All YANG files in the same directory are added to the pyang search
        path so that ``import`` and ``include`` statements resolve correctly.

        Returns
        -------
        tuple[dict, set[str]]
            ``(root_schema, node_type_names)`` ready for
            ``Converter.convert_schema()``.
        """
        ctx = Context(FileRepository(str(yang_path.parent)))
        with open(yang_path, encoding="utf-8") as fh:
            text = fh.read()
        mod_stmt = ctx.add_module(str(yang_path), text)
        ctx.validate()

        mod = ctx.get_module(mod_stmt.arg, None)
        self.module_name = mod.arg

        definitions: dict[str, Any] = {}
        node_type_names: set[str] = set()

        # typedefs → definitions (data types)
        for stmt in mod.substmts:
            if stmt.keyword == "typedef":
                definitions[stmt.arg] = self._typedef_to_schema(stmt)

        # top-level containers and lists → definitions + node types
        for stmt in getattr(mod, "i_children", []) or []:
            if stmt.keyword in ("container", "list"):
                definitions[stmt.arg] = self._container_to_schema(stmt)
                node_type_names.add(stmt.arg)

        return {"definitions": definitions}, node_type_names

    # ── Conversion helpers ────────────────────────────────────────────────────

    def _typedef_to_schema(self, typedef: Any) -> dict[str, Any]:
        type_stmt = typedef.search_one("type")
        schema = self._type_to_schema(type_stmt) if type_stmt else {"type": "string"}
        desc = typedef.search_one("description")
        if desc:
            schema["description"] = desc.arg
        return schema

    def _container_to_schema(self, stmt: Any) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object"}
        desc = stmt.search_one("description")
        if desc:
            schema["description"] = desc.arg

        properties: dict[str, Any] = {}
        required: list[str] = []

        for child in getattr(stmt, "i_children", []) or []:
            prop = self._child_to_schema(child)
            if prop is None:
                continue
            properties[child.arg] = prop
            mandatory = child.search_one("mandatory")
            if mandatory and mandatory.arg == "true":
                required.append(child.arg)

        if properties:
            schema["properties"] = properties
        if required:
            schema["required"] = required

        return schema

    def _child_to_schema(self, stmt: Any) -> dict[str, Any] | None:
        kw = stmt.keyword
        desc = stmt.search_one("description")

        if kw == "leaf":
            type_stmt = stmt.search_one("type")
            prop = self._type_to_schema(type_stmt) if type_stmt else {"type": "string"}
            if desc:
                prop["description"] = desc.arg
            return prop

        if kw == "leaf-list":
            type_stmt = stmt.search_one("type")
            items = self._type_to_schema(type_stmt) if type_stmt else {"type": "string"}
            prop: dict[str, Any] = {"type": "array", "items": items}
            if desc:
                prop["description"] = desc.arg
            return prop

        if kw == "container":
            return self._container_to_schema(stmt)

        if kw == "list":
            # A YANG list is an array of objects; expose the item schema
            item_schema = self._container_to_schema(stmt)
            prop = {"type": "array", "items": item_schema}
            if desc:
                prop["description"] = desc.arg
            return prop

        return None  # rpc, notification, action, etc. — skip

    def _type_to_schema(self, type_stmt: Any) -> dict[str, Any]:
        type_name = type_stmt.arg

        if type_name == "enumeration":
            enums = [e.arg for e in type_stmt.search("enum")]
            return {"type": "string", "enum": enums}

        if type_name == "union":
            for member in type_stmt.search("type"):
                candidate = self._type_to_schema(member)
                if candidate.get("type") not in (None, "null"):
                    return candidate
            return {"type": "string"}

        json_type = self.YANG_TO_JSON.get(type_name)
        if json_type is None:
            # Named typedef reference
            return {"$ref": f"#/definitions/{type_name}"}

        schema: dict[str, Any] = {"type": json_type}

        # Range → numeric min/max constraints
        range_stmt = type_stmt.search_one("range")
        if range_stmt and json_type in ("integer", "number"):
            lo, hi = self._parse_range(range_stmt.arg)
            if lo is not None:
                schema["minimum"] = lo
            if hi is not None:
                schema["maximum"] = hi

        # Length → string minLength/maxLength
        length_stmt = type_stmt.search_one("length")
        if length_stmt and json_type == "string":
            lo, hi = self._parse_range(length_stmt.arg)
            if lo is not None:
                schema["minLength"] = lo
            if hi is not None:
                schema["maxLength"] = hi

        # Pattern constraint
        pattern_stmt = type_stmt.search_one("pattern")
        if pattern_stmt:
            schema["pattern"] = pattern_stmt.arg

        return schema

    def _parse_range(self, range_str: str) -> tuple[int | None, int | None]:
        """Parse a YANG range/length string like ``'1..100'`` into ``(min, max)``."""
        part = range_str.split("|")[0].strip()
        if ".." in part:
            lo_str, hi_str = part.split("..", 1)
            lo = None if lo_str.strip() in ("min", "") else _to_int(lo_str.strip())
            hi = None if hi_str.strip() in ("max", "") else _to_int(hi_str.strip())
            return lo, hi
        v = _to_int(part)
        return v, v
