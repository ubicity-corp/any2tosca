"""
converter.py
------------
Core logic for converting JSON Schema documents to TOSCA data types.
"""

from __future__ import annotations

import json
import re
import sys
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
        self._search_paths: list[Path] = []     # additional schema search dirs
        self._imports: dict[str, str] = {}      # namespace → tosca_file
        self._no_import_files: set[str] = set() # filenames whose types are treated as local
        self._registry: Registry | None = None  # referencing Registry
        self._id_to_path: dict[str, Path] = {}  # $id URI → local Path
        self._current_filename: str = ""        # filename of the schema being converted

    def _all_dirs(self) -> list[Path]:
        """All directories to search for schema files, base path first."""
        dirs: list[Path] = []
        if self._base_path is not None:
            dirs.append(self._base_path)
        dirs.extend(self._search_paths)
        return dirs

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
        for search_dir in self._all_dirs():
            for json_file in search_dir.glob("*.json"):
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
                # Fallback: match by filename when $id doesn't match the URI
                fname = Path(uri_no_frag).name
                for search_dir in self._all_dirs():
                    candidate = search_dir / fname
                    if candidate.exists():
                        file_path = candidate
                        break
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
        return Path(file_part).stem + ".yaml"

    def _collect_no_import_files(self, root_schema: dict[str, Any]) -> None:
        """
        Pre-scan all ``$ref`` values in *root_schema* and add any external
        files that would create a circular import to ``_no_import_files``.

        Two cases are handled:

        - **Versioned files** (``BaseName.vX_Y_Z``): checked directly — if
          the file's content mentions the current filename, it references back.
        - **Unversioned files** (e.g. ``LogEntry.json``): checked transitively
          — if any versioned ref *inside* the unversioned file points to a file
          that mentions the current filename, the whole unversioned file would
          create a circular import chain and is suppressed.
        """
        if not self._current_filename:
            return
        _VER = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
        seen: set[str] = set()

        def _collect(obj: Any) -> None:
            if isinstance(obj, dict):
                if "$ref" in obj and isinstance(obj["$ref"], str):
                    seen.add(obj["$ref"])
                for v in obj.values():
                    _collect(v)
            elif isinstance(obj, list):
                for item in obj:
                    _collect(item)

        _collect(root_schema)

        for ref in seen:
            if ref.startswith("#"):
                continue
            uri = ref.partition("#")[0]
            if not uri:
                continue
            fname = Path(uri).name
            if not fname or fname in self._no_import_files:
                continue
            stem = Path(uri.split("/")[-1]).stem
            if _VER.match(stem):
                # Versioned file: direct check
                if self._refs_back_to_current(ref):
                    self._no_import_files.add(fname)
            else:
                # Unversioned file: transitive check through its versioned refs
                if self._refs_back_to_current_transitive(ref):
                    self._no_import_files.add(fname)

    def _refs_back_to_current(self, ref: str) -> bool:
        """
        Return ``True`` if the external file identified by *ref* contains any
        ``$ref`` pointing back to the file currently being converted.

        This detects potential circular imports (e.g. ``Resource.json`` has
        ``anyOf → Resource.v1_23_0.json``, but ``Resource.v1_23_0.json``
        also references ``Resource.json`` for stable shared types).  When a
        circular import would result from using the versioned ref as
        ``derived_from``, the caller should merge properties inline instead.
        """
        if not self._current_filename:
            return False
        file_no_frag = ref.partition("#")[0]
        if not file_no_frag:
            return False
        fname = Path(file_no_frag).name
        candidate = next(
            (d / fname for d in self._all_dirs() if (d / fname).exists()), None
        )
        if candidate is None:
            return False
        try:
            content = candidate.read_text(encoding="utf-8")
            return self._current_filename in content
        except OSError:
            return False

    def _refs_back_to_current_transitive(self, ref: str) -> bool:
        """
        Transitive version of :meth:`_refs_back_to_current`.

        Reads the file identified by *ref*, extracts all versioned external
        ``$ref`` URIs from it, and returns ``True`` if any of those versioned
        files directly reference the current file.

        This handles chains like::

            CurrentFile.json → OtherFile.json → OtherFile.vX_Y_Z.json → CurrentFile.json
        """
        if not self._current_filename:
            return False
        file_no_frag = ref.partition("#")[0]
        if not file_no_frag:
            return False
        fname = Path(file_no_frag).name
        candidate = next(
            (d / fname for d in self._all_dirs() if (d / fname).exists()), None
        )
        if candidate is None:
            return False
        try:
            doc = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        _VER = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
        nested_refs: set[str] = set()

        def _collect(obj: Any) -> None:
            if isinstance(obj, dict):
                if "$ref" in obj and isinstance(obj["$ref"], str):
                    nested_refs.add(obj["$ref"])
                for v in obj.values():
                    _collect(v)
            elif isinstance(obj, list):
                for item in obj:
                    _collect(item)

        _collect(doc)
        for nested in nested_refs:
            if nested.startswith("#"):
                continue
            nested_uri = nested.partition("#")[0]
            nested_stem = Path(nested_uri.split("/")[-1]).stem
            if not _VER.match(nested_stem):
                continue
            if self._refs_back_to_current(nested):
                return True
        return False

    def _rewrite_local_refs(self, obj: Any, file_part: str) -> Any:
        """
        Recursively rewrite ``$ref`` values that start with ``#/`` to be
        prefixed with *file_part*, turning local refs into external file refs.

        This is applied to properties inlined from external schemas so that
        references to types defined in that external schema are expressed as
        ``ExternalFile.json#/definitions/TypeName`` rather than the ambiguous
        ``#/definitions/TypeName``.
        """
        if isinstance(obj, dict):
            return {
                k: (f"{file_part}{v}"
                    if k == "$ref" and isinstance(v, str) and v.startswith("#/")
                    else self._rewrite_local_refs(v, file_part))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._rewrite_local_refs(item, file_part) for item in obj]
        return obj

    def _resolve_for_merge(self, ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve *ref* for the purpose of inlining its properties.

        For external refs, local ``#/…`` sub-refs inside the resolved schema
        are rewritten to ``filename#/…`` so that the converter later generates
        correctly namespaced TOSCA type references (and records the import).
        """
        resolved = self.resolve_ref(ref, root_schema)
        if ref.startswith("#"):
            return resolved
        file_part, _ = self._parse_ref(ref)
        if file_part:
            resolved = self._rewrite_local_refs(resolved, file_part)
        return resolved

    def _ref_to_tosca_type(self, ref: str) -> str:
        """
        Convert a ``$ref`` to a TOSCA type name.

        External file refs are registered in ``self._imports`` and returned
        as ``namespace:TypeName``; local refs are returned as plain type names.
        """
        file_part, type_name = self._parse_ref(ref)
        if not type_name:
            print(f"Warning: cannot resolve $ref '{ref}' — using 'string'", file=sys.stderr)
            return "string"
        if file_part is None:
            return type_name  # local ref
        # Ref points back to the file being converted — the type is defined in the
        # same TOSCA output file, so no import is needed.
        if self._current_filename and Path(file_part).name == self._current_filename:
            return type_name
        # File was merged inline to avoid a circular import — treat all its types
        # as locally defined (they appear in the inline-merged properties).
        if Path(file_part).name in self._no_import_files:
            return type_name
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
            pass

        # Registry lookup failed — fall back to loading the file directly by name
        file_no_frag, _, fragment = ref.partition("#")
        if file_no_frag:
            fname = Path(file_no_frag).name
            candidate = next(
                (d / fname for d in self._all_dirs() if (d / fname).exists()), None
            )
            if candidate is not None:
                try:
                    with open(candidate, encoding="utf-8") as fh:
                        doc = json.load(fh)
                    if fragment:
                        node: Any = doc
                        for part in fragment.lstrip("/").split("/"):
                            if isinstance(node, dict) and part in node:
                                node = node[part]
                            else:
                                node = {}
                                break
                        return node if isinstance(node, dict) else {}
                    return doc if isinstance(doc, dict) else {}
                except (OSError, json.JSONDecodeError):
                    pass

        print(f"Warning: failed to resolve $ref '{ref}' — schema details will be missing", file=sys.stderr)
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

    def _pick_latest_versioned_ref(self, refs: list[str]) -> str | None:
        """
        If every ref in *refs* points to a versioned variant of the same base
        resource (``BaseName.vX_Y_Z`` filename pattern), return the ref with
        the highest version tuple.  Returns ``None`` if the pattern doesn't
        match or the refs have different base names.
        """
        _VER = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
        entries: list[tuple[str, tuple[int, int, int], str]] = []
        for ref in refs:
            uri = ref.partition("#")[0]
            stem = Path(uri.split("/")[-1]).stem   # last path component, no ext
            m = _VER.match(stem)
            if not m:
                return None
            base = m.group(1)
            version = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
            entries.append((base, version, ref))
        bases = {e[0] for e in entries}
        if len(bases) != 1:
            return None
        return max(entries, key=lambda e: e[1])[2]

    def _merge_allof(
        self,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """
        Flatten ``allOf``, ``anyOf``, or ``oneOf`` into a single merged schema.

        Returns ``(merged_schema, derived_from)`` where ``derived_from`` is the
        TOSCA type name of the first pure ``$ref`` sub-schema (inheritance), or
        ``None``.

        ``anyOf`` / ``oneOf`` handling:

        - Null-only members are stripped first.
        - If a single non-null member remains: versioned external refs and local
          refs become ``derived_from``; unversioned external refs (e.g. ``idRef``)
          represent link patterns, not inheritance — their properties are merged
          inline and ``derived_from`` is left unset.
        - Versioned ``$refs`` (``BaseName.vX_Y_Z``) identify the actual resource
          type; the latest version becomes ``derived_from`` and properties from
          any remaining (unversioned) ``$refs`` are merged in.
        - Multiple pure ``$ref`` members with no versioned group: first becomes
          ``derived_from``; the rest have their properties merged in.
        - Mixed members (refs + inline schemas): same merge as ``allOf``.
        """
        keyword = next((k for k in ("allOf", "anyOf", "oneOf") if k in schema), None)
        if keyword is None:
            return schema, None

        members: list[dict[str, Any]] = list(schema[keyword])

        # anyOf / oneOf: strip null-only members; flatten if one remains
        if keyword in ("anyOf", "oneOf"):
            non_null = [m for m in members
                        if not (isinstance(m, dict) and m.get("type") == "null")]
            if non_null:
                members = non_null
            if len(members) == 1:
                outer = {k: v for k, v in schema.items() if k != keyword}
                single = members[0]
                if "$ref" in single and len(single) == 1:
                    ref = single["$ref"]
                    if not ref.startswith("#"):
                        # External ref: only use as derived_from if it is a
                        # versioned resource ref (BaseName.vX_Y_Z).  Unversioned
                        # external refs (e.g. idRef) represent a link/reference
                        # pattern, not true type inheritance — merging their
                        # properties inline avoids mixing data-type and node-type
                        # hierarchies in TOSCA.
                        _VER = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
                        uri = ref.partition("#")[0]
                        stem = Path(uri.split("/")[-1]).stem
                        if not _VER.match(stem):
                            # Unversioned external ref → merge inline, no derived_from
                            resolved = self._resolve_for_merge(ref, root_schema)
                            if resolved.get("properties"):
                                outer["properties"] = {
                                    **resolved.get("properties", {}),
                                    **outer.get("properties", {}),
                                }
                            if resolved.get("required"):
                                outer["required"] = list(dict.fromkeys(
                                    resolved["required"] + outer.get("required", [])
                                ))
                            return outer, None
                    # Versioned external ref that references the current file
                    # back would create a circular import — merge inline instead.
                    if self._refs_back_to_current(ref):
                        fname = Path(ref.partition("#")[0]).name
                        if fname:
                            self._no_import_files.add(fname)
                        resolved = self._resolve_for_merge(ref, root_schema)
                        if resolved.get("properties"):
                            outer["properties"] = {
                                **resolved.get("properties", {}),
                                **outer.get("properties", {}),
                            }
                        if resolved.get("required"):
                            outer["required"] = list(dict.fromkeys(
                                resolved["required"] + outer.get("required", [])
                            ))
                        return outer, None
                    # Local ref or versioned external ref → derived_from
                    return outer, self._ref_to_tosca_type(ref)
                return self._merge_allof({**single, **outer}, root_schema)

            # Multiple pure $refs: separate versioned (BaseName.vX_Y_Z) from others.
            # If exactly one versioned-base group exists, pick its latest version as
            # derived_from and merge properties from unversioned alternatives.
            pure_refs = [m["$ref"] for m in members if "$ref" in m and len(m) == 1]
            if len(pure_refs) == len(members):
                _VER = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")
                versioned: dict[str, list[str]] = {}
                unversioned: list[str] = []
                for ref in pure_refs:
                    uri = ref.partition("#")[0]
                    stem = Path(uri.split("/")[-1]).stem
                    m = _VER.match(stem)
                    if m:
                        versioned.setdefault(m.group(1), []).append(ref)
                    else:
                        unversioned.append(ref)

                if len(versioned) == 1:
                    best = self._pick_latest_versioned_ref(
                        next(iter(versioned.values()))
                    )
                    if best is not None:
                        outer = {k: v for k, v in schema.items() if k != keyword}
                        # If the versioned file references the current file back,
                        # using it as derived_from would create a circular import.
                        # Merge all refs inline instead.
                        if self._refs_back_to_current(best):
                            for ref in pure_refs:
                                fname = Path(ref.partition("#")[0]).name
                                if fname:
                                    self._no_import_files.add(fname)
                            merged_props_ci: dict[str, Any] = {}
                            merged_required_ci: list[str] = []
                            for ref in pure_refs:
                                resolved = self._resolve_for_merge(ref, root_schema)
                                merged_props_ci.update(resolved.get("properties", {}))
                                merged_required_ci.extend(resolved.get("required", []))
                            if merged_props_ci:
                                outer["properties"] = merged_props_ci
                            if merged_required_ci:
                                outer["required"] = list(dict.fromkeys(merged_required_ci))
                            return outer, None
                        derived_from = self._ref_to_tosca_type(best)
                        if not unversioned:
                            return outer, derived_from
                        # Merge properties from unversioned alternatives
                        merged_props: dict[str, Any] = {}
                        merged_required: list[str] = []
                        for ref in unversioned:
                            resolved = self._resolve_for_merge(ref, root_schema)
                            merged_props.update(resolved.get("properties", {}))
                            merged_required.extend(resolved.get("required", []))
                        if merged_props:
                            outer["properties"] = merged_props
                        if merged_required:
                            outer["required"] = list(dict.fromkeys(merged_required))
                        return outer, derived_from

        merged: dict[str, Any] = {k: v for k, v in schema.items() if k != keyword}
        merged_props: dict[str, Any] = dict(merged.pop("properties", {}))
        merged_required: list[str] = list(merged.pop("required", []))
        derived_from: str | None = None

        for sub in members:
            if "$ref" in sub and len(sub) == 1:
                if derived_from is None:
                    # First pure $ref → base type
                    derived_from = self._ref_to_tosca_type(sub["$ref"])
                else:
                    # Additional pure $refs (anyOf/oneOf union) — merge properties
                    if keyword in ("anyOf", "oneOf"):
                        print(
                            f"Warning: {keyword} has multiple $ref alternatives — "
                            f"merging '{sub['$ref']}' properties; semantic union is lost",
                            file=sys.stderr,
                        )
                    resolved = self._resolve_for_merge(sub["$ref"], root_schema)
                    merged_props.update(resolved.get("properties", {}))
                    merged_required.extend(resolved.get("required", []))
            else:
                resolved = self._resolve_for_merge(sub["$ref"], root_schema) if "$ref" in sub else sub
                merged_props.update(resolved.get("properties", {}))
                merged_required.extend(resolved.get("required", []))
                for key in ("description", "title", "type"):
                    if key in resolved and key not in merged:
                        merged[key] = resolved[key]

        if merged_props:
            merged["properties"] = merged_props
        if merged_required:
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

    def _inline_type_or_primitive(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> str:
        """
        Given a schema resolved from a circular-import file, return an
        appropriate TOSCA type name without creating an external import.

        - If the schema has ``properties``, create a local data type named
          *type_name* in *data_types* and return that name.
        - Otherwise return the TOSCA primitive that best matches the schema
          (e.g. ``"string"`` for enum types).
        """
        if any(k in schema for k in ("allOf", "anyOf", "oneOf")):
            schema, _ = self._merge_allof(schema, root_schema)

        json_type = schema.get("type")
        if schema.get("properties") or json_type in ("object", None) and schema.get("properties"):
            safe_name = self.sanitize_name(type_name)
            if safe_name not in data_types:
                data_types[safe_name] = self.schema_to_tosca_data_type(
                    safe_name, schema, root_schema, data_types
                )
            return safe_name
        return self.get_tosca_type(json_type, schema, root_schema)

    def schema_to_property(
        self,
        prop_name: str,
        prop_schema: dict[str, Any],
        required_props: list[str],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a single JSON Schema property definition to a TOSCA property dict."""
        # allOf / anyOf / oneOf → merge sub-schemas first
        if any(k in prop_schema for k in ("allOf", "anyOf", "oneOf")):
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
            ref = prop_schema["$ref"]
            file_part, _ = self._parse_ref(ref)
            if file_part is not None and Path(file_part).name in self._no_import_files:
                # Referenced file would create a circular import — resolve the
                # schema inline and fall through to normal type detection.
                # Use _resolve_for_merge (not resolve_ref) so that any local
                # #/... refs inside the resolved schema are rewritten to
                # filename#/... — otherwise they resolve against root_schema
                # where the types don't exist.
                ref_schema = self._resolve_for_merge(ref, root_schema)
                prop_schema = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                # fall through to inline processing below
            else:
                ref_type_name = self._ref_to_tosca_type(ref)
                ref_schema = self.resolve_ref(ref, root_schema)
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
                item_ref = items["$ref"]
                fp, item_type_name = self._parse_ref(item_ref)
                if fp is not None and Path(fp).name in self._no_import_files:
                    # Circular file — resolve inline
                    item_schema = self._resolve_for_merge(item_ref, root_schema)
                    item_schema = {**item_schema, **{k: v for k, v in items.items() if k != "$ref"}}
                    item_tosca_type = self._inline_type_or_primitive(
                        item_type_name, item_schema, root_schema, data_types
                    )
                else:
                    item_tosca_type = self._ref_to_tosca_type(item_ref)
            else:
                item_tosca_type = self.get_tosca_type(items.get("type"), items, root_schema)
            prop_def["entry_schema"] = {"type": item_tosca_type}

        # entry_schema for map types with additionalProperties (no named properties)
        if tosca_type == "map":
            add_props = prop_schema.get("additionalProperties", {})
            if isinstance(add_props, dict):
                if "$ref" in add_props:
                    ap_ref = add_props["$ref"]
                    fp, ap_type_name = self._parse_ref(ap_ref)
                    if fp is not None and Path(fp).name in self._no_import_files:
                        ap_schema = self._resolve_for_merge(ap_ref, root_schema)
                        ap_schema = {**ap_schema, **{k: v for k, v in add_props.items() if k != "$ref"}}
                        val_type = self._inline_type_or_primitive(
                            ap_type_name, ap_schema, root_schema, data_types
                        )
                    else:
                        val_type = self._ref_to_tosca_type(ap_ref)
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

        if derived_from:
            node_type["derived_from"] = derived_from

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
        search_paths: list[Path] | None = None,
        source_path: Path | None = None,
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
        search_paths:
            Additional directories to search for referenced schema files,
            consulted after ``base_path``.
        source_path:
            Path to the source schema file being converted.  Used to detect
            potential circular imports when the source file's filename cannot
            be inferred from the schema's ``$id`` (e.g. after normalization).

        Returns
        -------
        ConversionResult
            Named tuple with ``data_types``, ``node_types``, and ``imports`` dicts.
        """
        self._base_path = base_path
        self._search_paths = list(search_paths) if search_paths else []
        self._registry = None   # rebuild for new base_path / search_paths
        self._id_to_path = {}
        self._imports = {}
        self._no_import_files = set()
        self._current_filename = (
            source_path.name if source_path
            else Path(root_schema.get("$id", "")).name
        )
        self._collect_no_import_files(root_schema)
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
            doc["imports"] = [{"url": file, "namespace": ns} for ns, file in imports.items()]
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
    search_paths: list[Path] | None = None,
    source_path: Path | None = None,
) -> ConversionResult:
    return Converter().convert_schema(root_schema, top_level_name, node_type_names,
                                      base_path, search_paths, source_path)


def build_tosca_document(
    data_types: dict[str, Any],
    node_types: dict[str, Any] | None = None,
    imports: dict[str, str] | None = None,
) -> dict[str, Any]:
    return Converter().build_tosca_document(data_types, node_types, imports)
