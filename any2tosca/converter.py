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

    #: Matches versioned JSON Schema filenames: ``BaseName.vX_Y_Z`` or ``BaseName.vX.Y.Z``.
    _VER_RE = re.compile(r"^(.+)\.v(\d+)[._](\d+)[._](\d+)")

    JSON_SCHEMA_TO_TOSCA_TYPE: dict[str, str] = {
        "string":  "string",
        "integer": "integer",
        "number":  "float",
        "boolean": "boolean",
        "object":  "map",
        "array":   "list",
        "null":    "string",   # TOSCA has no null; fall back to string
    }

    #: TOSCA 2.0 built-in primitive and collection types — never node types.
    _TOSCA_PRIMITIVES: frozenset[str] = frozenset({
        "string", "integer", "float", "boolean", "bytes", "timestamp",
        "list", "map",
        "scalar-unit.size", "scalar-unit.time",
        "scalar-unit.frequency", "scalar-unit.bitrate",
    })

    def __init__(self) -> None:
        self._base_path: Path | None = None
        self._search_paths: list[Path] = []     # additional schema search dirs
        self._imports: dict[str, str] = {}      # namespace → tosca_file
        self._no_import_files: set[str] = set() # filenames whose types are treated as local
        self._registry: Registry | None = None  # referencing Registry
        self._id_to_path: dict[str, Path] = {}  # $id URI → local Path
        self._current_filename: str = ""        # filename of the schema being converted
        # Persistent caches (survive across convert_schema calls for the same search dirs)
        self._file_cache: dict[str, str] = {}          # abs path → raw text
        self._doc_cache: dict[str, dict[str, Any]] = {}# abs path → parsed JSON
        self._fname_to_path: dict[str, Path | None] = {}  # filename → abs Path (or None)
        # Per-conversion cache (reset each convert_schema call)
        self._transitive_back_cache: dict[str, bool] = {}  # start_fname → bool
        self._node_type_names: set[str] = set() # names that are node types in this file

    def _all_dirs(self) -> list[Path]:
        """All directories to search for schema files, base path first."""
        dirs: list[Path] = []
        if self._base_path is not None:
            dirs.append(self._base_path)
        dirs.extend(self._search_paths)
        return dirs

    def _resolve_fname(self, fname: str) -> "Path | None":
        """Resolve *fname* to an absolute Path, caching the result."""
        if fname in self._fname_to_path:
            return self._fname_to_path[fname]
        candidate = next(
            (d / fname for d in self._all_dirs() if (d / fname).exists()), None
        )
        self._fname_to_path[fname] = candidate
        return candidate

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
            # Fast path: match by filename extracted from the URI (works for most schemas).
            fname = Path(uri_no_frag).name
            file_path: Path | None = next(
                (d / fname for d in self._all_dirs() if (d / fname).exists()), None
            )
            # Slow path: consult the $id registry for schemas whose URI doesn't
            # end with the same filename as the local file.
            if file_path is None:
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
        return Path(file_part).stem + ".yaml"

    def _load_cached_doc(self, fname: str) -> dict[str, Any] | None:
        """Find *fname* in the schema search dirs, read it (using ``_file_cache``), and parse JSON.

        Returns the parsed document dict, or ``None`` if the file cannot be
        found, read, or parsed.  The raw text is always stored in
        ``_file_cache`` so that text-search callers (e.g.
        :meth:`_refs_back_to_current`) can reuse the cached content.
        """
        candidate = self._resolve_fname(fname)
        if candidate is None:
            return None
        key = str(candidate)
        if key not in self._file_cache:
            try:
                self._file_cache[key] = candidate.read_text(encoding="utf-8")
            except OSError:
                self._file_cache[key] = ""
        if not self._file_cache[key]:
            return None
        if key in self._doc_cache:
            return self._doc_cache[key]
        try:
            doc = json.loads(self._file_cache[key])
        except json.JSONDecodeError:
            return None
        self._doc_cache[key] = doc
        return doc

    def _mark_no_import(self, fname: str) -> None:
        """Add *fname* to ``_no_import_files`` and register its root type in ``_node_type_names``.

        When a file is inlined to avoid a circular TOSCA import, any type that
        the file designates as its root resource (via a top-level
        ``$ref: "#/definitions/TypeName"``) is also added to
        ``_node_type_names`` so that references to that type are recognised as
        node-type references and converted to TOSCA requirements.
        """
        self._no_import_files.add(fname)
        doc = self._load_cached_doc(fname)
        if doc is None:
            return
        top_ref = doc.get("$ref", "")
        if top_ref.startswith("#/definitions/"):
            self._node_type_names.add(self.sanitize_name(top_ref.split("/")[-1]))

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
        _VER = self._VER_RE
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

        # Deduplicate to one representative ref per filename so each external
        # file is read at most once regardless of how many fragment-refs point
        # to it (e.g. Resource.json#/Description, Resource.json#/Name, …).
        seen_files: dict[str, str] = {}  # fname → first ref seen for that file
        for ref in seen:
            if ref.startswith("#"):
                continue
            uri = ref.partition("#")[0]
            if not uri:
                continue
            fname = Path(uri).name
            if fname and fname not in seen_files:
                seen_files[fname] = ref

        for fname, ref in seen_files.items():
            if fname in self._no_import_files:
                continue
            stem = Path(fname).stem
            if _VER.match(stem):
                # Versioned file: direct check
                if self._refs_back_to_current(ref):
                    self._mark_no_import(fname)
            else:
                # Unversioned file: transitive check through its versioned refs
                if self._refs_back_to_current_transitive(ref):
                    self._mark_no_import(fname)

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
        candidate = self._resolve_fname(fname)
        if candidate is None:
            return False
        key = str(candidate)
        if key not in self._file_cache:
            try:
                self._file_cache[key] = candidate.read_text(encoding="utf-8")
            except OSError:
                self._file_cache[key] = ""
        return bool(self._file_cache[key]) and self._current_filename in self._file_cache[key]

    def _collect_ref_fnames(self, obj: Any) -> set[str]:
        """Collect all external JSON Schema filenames referenced in *obj*."""
        fnames: set[str] = set()

        def _walk(o: Any) -> None:
            if isinstance(o, dict):
                ref = o.get("$ref")
                if isinstance(ref, str) and not ref.startswith("#"):
                    file_part = ref.partition("#")[0]
                    fname = Path(file_part).name if file_part else ""
                    if fname:
                        fnames.add(fname)
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for item in o:
                    _walk(item)

        _walk(obj)
        return fnames

    def _refs_back_to_current_transitive(self, ref: str) -> bool:
        """
        Return ``True`` if the current file is transitively reachable from
        *ref* via any chain of external ``$ref`` pointers.

        Uses breadth-first search through all reachable files, stopping as
        soon as any file is found whose raw text mentions the current filename.
        This handles arbitrarily deep chains such as::

            CurrentFile.json → A.json → A.vX_Y_Z.json → B.json
                → B.vX_Y_Z.json → CurrentFile.json
        """
        if not self._current_filename:
            return False
        file_no_frag = ref.partition("#")[0]
        if not file_no_frag:
            return False
        start_fname = Path(file_no_frag).name
        if not start_fname:
            return False

        if start_fname in self._transitive_back_cache:
            return self._transitive_back_cache[start_fname]

        visited: set[str] = set()
        queue: list[str] = [start_fname]
        result = False
        while queue:
            fname = queue.pop(0)
            if fname in visited:
                continue
            visited.add(fname)
            # Ensure raw text is cached for the fast text-search below.
            candidate = self._resolve_fname(fname)
            if candidate is None:
                continue
            key = str(candidate)
            if key not in self._file_cache:
                try:
                    self._file_cache[key] = candidate.read_text(encoding="utf-8")
                except OSError:
                    self._file_cache[key] = ""
            text = self._file_cache[key]
            if not text:
                continue
            if self._current_filename in text:
                result = True
                break
            doc = self._load_cached_doc(fname)
            if doc is None:
                continue
            for nested_fname in self._collect_ref_fnames(doc):
                if nested_fname not in visited:
                    queue.append(nested_fname)
        self._transitive_back_cache[start_fname] = result
        return result

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
        fname = Path(file_part).name
        if fname in self._no_import_files:
            return type_name
        # On-demand circular-import check: catches refs inside files that were
        # already inlined (and therefore not present in the root schema's refs,
        # so missed by the initial _collect_no_import_files pre-scan).
        stem = Path(fname).stem
        if self._VER_RE.match(stem):
            if self._refs_back_to_current(ref):
                self._mark_no_import(fname)
                return type_name
        elif self._refs_back_to_current_transitive(ref):
            self._mark_no_import(fname)
            return type_name
        namespace = self._namespace_for(file_part)
        self._imports[namespace] = self._tosca_file_for(file_part)
        return f"{namespace}:{type_name}"

    def resolve_ref(self, ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve a ``$ref`` to its schema dict.

        Local ``#/...`` refs are resolved directly against ``root_schema``.
        External refs are resolved by filename first (fast path); the
        ``referencing`` registry is only consulted as a fallback for schemas
        whose URI does not end with the local filename.
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

        # Fast path: load file directly by filename extracted from the ref.
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

        # Slow path: consult the referencing.Registry for complex URI resolution.
        registry, _ = self._get_registry()
        base_uri = root_schema.get("$id", "")
        try:
            resolver = registry.resolver(base_uri=base_uri) if base_uri else registry.resolver()
            contents = resolver.lookup(ref).contents
            return contents if isinstance(contents, dict) else {}
        except Exception:
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

    def _classify_member(self, member: dict[str, Any]) -> str:
        """
        Classify an ``anyOf``/``oneOf``/``allOf`` member.

        Returns one of:

        - ``"local"``       — pure ``$ref`` to a local ``#/...`` target.
        - ``"versioned"``   — pure ``$ref`` to a versioned external file
          (``BaseName.vX_Y_Z``), not circular.
        - ``"circular"``    — pure ``$ref`` to an external file that is in
          ``_no_import_files`` or references the current file back.
        - ``"unversioned"`` — pure ``$ref`` to any other external file.
        - ``"inline"``      — member has no ``$ref``, or has ``$ref`` plus
          additional keys (mixed schema).
        """
        if "$ref" not in member or len(member) != 1:
            return "inline"
        ref = member["$ref"]
        if ref.startswith("#"):
            return "local"
        uri = ref.partition("#")[0]
        fname = Path(uri).name
        if fname in self._no_import_files:
            return "circular"
        stem = Path(uri.split("/")[-1]).stem
        if self._VER_RE.match(stem):
            if self._refs_back_to_current(ref):
                if fname:
                    self._mark_no_import(fname)
                return "circular"
            return "versioned"
        else:
            if self._refs_back_to_current_transitive(ref):
                if fname:
                    self._mark_no_import(fname)
                return "circular"
            return "unversioned"

    def _merge_props_from(
        self,
        refs: list[str],
        schemas: list[dict[str, Any]],
        root_schema: dict[str, Any],
        outer: dict[str, Any],
        outer_wins: bool = False,
    ) -> dict[str, Any]:
        """
        Resolve *refs* and merge *schemas* inline into *outer*.

        Properties and ``required`` lists are accumulated; scalar metadata
        (``description``, ``title``, ``type``) is lifted into *outer* only
        when not already present.

        When *outer_wins* is ``True`` the outer schema's existing properties
        take precedence over those resolved from refs/schemas (used when
        merging a single unversioned or circular ref whose properties are
        treated as defaults).  When ``False`` (the default), later-resolved
        properties overwrite earlier ones.
        """
        result = dict(outer)
        merged_props: dict[str, Any] = dict(result.pop("properties", {}))
        merged_required: list[str] = list(result.pop("required", []))

        for ref in refs:
            resolved = self._resolve_for_merge(ref, root_schema)
            if outer_wins:
                merged_props = {**resolved.get("properties", {}), **merged_props}
            else:
                merged_props.update(resolved.get("properties", {}))
            merged_required.extend(resolved.get("required", []))

        for sub in schemas:
            resolved = self._resolve_for_merge(sub["$ref"], root_schema) if "$ref" in sub else sub
            merged_props.update(resolved.get("properties", {}))
            merged_required.extend(resolved.get("required", []))
            # Lift scalar metadata
            for sub_src in (sub, resolved):
                for key in ("description", "title", "type"):
                    if key in sub_src and key not in result:
                        result[key] = sub_src[key]

        if merged_props:
            result["properties"] = merged_props
        if merged_required:
            result["required"] = list(dict.fromkeys(merged_required))
        return result

    def _pick_latest_versioned_ref(self, refs: list[str]) -> str | None:
        """
        If every ref in *refs* points to a versioned variant of the same base
        resource (``BaseName.vX_Y_Z`` filename pattern), return the ref with
        the highest version tuple.  Returns ``None`` if the pattern doesn't
        match or the refs have different base names.
        """
        entries: list[tuple[str, tuple[int, int, int], str]] = []
        for ref in refs:
            uri = ref.partition("#")[0]
            stem = Path(uri.split("/")[-1]).stem   # last path component, no ext
            m = self._VER_RE.match(stem)
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
        TOSCA type name chosen for inheritance, or ``None``.

        Member classification (via :meth:`_classify_member`):

        - ``"local"`` / ``"versioned"`` refs → candidate for ``derived_from``.
        - ``"unversioned"`` / ``"circular"`` refs → properties merged inline.
        - ``"inline"`` (no ``$ref`` or mixed) → properties + metadata merged inline.

        ``anyOf`` / ``oneOf`` extra rules:

        - Null-only members are stripped first.
        - Among versioned refs, the latest version of the sole versioned-base
          group wins as ``derived_from``; all unversioned / circular refs are
          merged inline instead.
        - If no versioned group exists, the first local ref becomes
          ``derived_from``; remaining refs and inline schemas are merged.
        """
        keyword = next((k for k in ("allOf", "anyOf", "oneOf") if k in schema), None)
        if keyword is None:
            return schema, None

        members: list[dict[str, Any]] = list(schema[keyword])
        outer = {k: v for k, v in schema.items() if k != keyword}

        # Strip null-only members for anyOf/oneOf
        if keyword in ("anyOf", "oneOf"):
            non_null = [m for m in members
                        if not (isinstance(m, dict) and m.get("type") == "null")]
            if non_null:
                members = non_null

        # ── Classify all members ──────────────────────────────────────────────

        versioned_groups: dict[str, list[str]] = {}  # base-name → list of refs
        to_merge_refs: list[str] = []                # refs to inline-merge
        inline_schemas: list[dict[str, Any]] = []    # non-pure-ref members
        first_local_ref: str | None = None

        for member in members:
            kind = self._classify_member(member)
            ref = member.get("$ref", "")

            if kind == "local":
                if first_local_ref is None:
                    first_local_ref = ref
                else:
                    to_merge_refs.append(ref)

            elif kind in ("unversioned", "circular"):
                to_merge_refs.append(ref)

            elif kind == "versioned":
                uri = ref.partition("#")[0]
                m = self._VER_RE.match(Path(uri.split("/")[-1]).stem)
                versioned_groups.setdefault(m.group(1), []).append(ref)  # type: ignore[union-attr]

            else:  # "inline"
                inline_schemas.append(member)

        # ── Determine derived_from ────────────────────────────────────────────

        derived_from: str | None = None

        if len(versioned_groups) == 1:
            best = self._pick_latest_versioned_ref(next(iter(versioned_groups.values())))
            if best is not None:
                derived_from = self._ref_to_tosca_type(best)
            # Any local ref defers to the versioned winner — merge it inline
            if first_local_ref:
                to_merge_refs.append(first_local_ref)

        elif versioned_groups:
            # Multiple versioned-base groups: can't pick one parent, merge all inline
            for refs in versioned_groups.values():
                to_merge_refs.extend(refs)
            if first_local_ref:
                derived_from = self._ref_to_tosca_type(first_local_ref)

        else:
            # No versioned refs at all: first local ref (if any) is the parent
            if first_local_ref:
                derived_from = self._ref_to_tosca_type(first_local_ref)
            # Otherwise: only unversioned/circular/inline members → no parent

        # Warn when anyOf/oneOf has multiple refs whose union semantics are lost
        if keyword in ("anyOf", "oneOf") and derived_from and to_merge_refs:
            for ref in to_merge_refs:
                if ref:
                    print(
                        f"Warning: {keyword} has multiple $ref alternatives — "
                        f"merging '{ref}' properties; semantic union is lost",
                        file=sys.stderr,
                    )
                    break  # one warning per schema is enough

        # ── Merge inline ──────────────────────────────────────────────────────

        # For a single unversioned/circular ref the outer schema's properties
        # take priority (outer_wins=True); for multi-ref merges last-ref wins.
        outer_wins = (
            not inline_schemas
            and len(to_merge_refs) == 1
            and not derived_from
        )
        return self._merge_props_from(
            to_merge_refs, inline_schemas, root_schema, outer, outer_wins=outer_wins
        ), derived_from

    def collect_definitions(self, root_schema: dict[str, Any]) -> dict[str, Any]:
        """Collect all named sub-schemas from ``definitions`` and ``$defs``."""
        defs: dict[str, Any] = {}
        defs.update(root_schema.get("definitions", {}))
        defs.update(root_schema.get("$defs", {}))
        return defs

    # ── Node-type detection ───────────────────────────────────────────────────

    def _is_file_root_type(self, fname: str, type_name: str) -> bool:
        """Return True if *type_name* is the root definition of external file *fname*.

        Two checks are applied (either is sufficient):

        1. The file's top-level ``$ref`` is ``"#/definitions/TypeName"`` — the
           explicit Redfish convention for designating the file's resource type.
        2. The type name matches the file's base stem (version suffix stripped)
           **and** the file is a Redfish schema (``$id`` contains
           ``redfish.dmtf.org``).  This covers Redfish utility schemas like
           ``Resource.json`` that omit the top-level ``$ref``.
        """
        doc = self._load_cached_doc(fname)
        if doc is None:
            return False

        # Definitive: explicit top-level $ref points to this definition.
        if doc.get("$ref") == f"#/definitions/{type_name}":
            return True

        # Heuristic for Redfish schemas only: type name equals the base file stem.
        if "redfish.dmtf.org" in doc.get("$id", ""):
            stem = Path(fname).stem
            base_stem = re.sub(r"[._]v\d+(?:[._]\d+)*$", "", stem) or stem
            if self.sanitize_name(type_name) == self.sanitize_name(base_stem):
                return True

        return False

    def _is_node_type_tosca(self, tosca_type: str) -> bool:
        """Return True if *tosca_type* refers to a TOSCA node type rather than a data type.

        - TOSCA primitives are never node types.
        - Local (unqualified) types are checked against ``_node_type_names``.
        - Namespaced types (``namespace:TypeName``) are checked against the
          root definition of the corresponding external JSON Schema file.
        """
        if tosca_type in self._TOSCA_PRIMITIVES:
            return False
        if ":" in tosca_type:
            namespace, type_name = tosca_type.split(":", 1)
            tosca_file = self._imports.get(namespace)
            if tosca_file is None:
                return False
            fname = Path(tosca_file).stem + ".json"
            return self._is_file_root_type(fname, type_name)
        return tosca_type in self._node_type_names

    def _prop_to_requirement(
        self, prop_def: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return a TOSCA requirement dict if *prop_def* references a node type, else None.

        Count range is derived from the property's ``required`` flag and whether
        it represents a collection (``type: list``) or a single reference:

        - list + required=False → ``[0, UNBOUNDED]``
        - list + required=True  → ``[1, UNBOUNDED]``
        - single + required=False → ``[0, 1]``
        - single + required=True  → ``[1, 1]``
        """
        prop_type = prop_def.get("type", "")
        is_required = prop_def.get("required", False)

        if prop_type == "list":
            entry_type = (prop_def.get("entry_schema") or {}).get("type", "")
            if not entry_type or not self._is_node_type_tosca(entry_type):
                return None
            node_type = entry_type
            count_range: list[Any] = [1, "UNBOUNDED"] if is_required else [0, "UNBOUNDED"]
        elif self._is_node_type_tosca(prop_type):
            node_type = prop_type
            count_range = [1, 1] if is_required else [0, 1]
        else:
            return None

        self._imports["core"] = "Core.yaml"
        req: dict[str, Any] = {
            "node": node_type,
            "capability": "core:Containable",
            "relationship": "core:Contains",
            "count_range": count_range,
        }
        description = prop_def.get("description")
        if description:
            req["description"] = description
        return req

    def _split_props_and_requirements(
        self, properties: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Split *properties* into plain TOSCA properties and node-type requirements.

        Returns ``(plain_props, requirements)`` where ``requirements`` is a list
        of single-key dicts ``{name: req_def}`` suitable for TOSCA
        ``requirements`` sections.
        """
        plain_props: dict[str, Any] = {}
        requirements: list[dict[str, Any]] = []
        for pname, pdef in properties.items():
            req = self._prop_to_requirement(pdef)
            if req is not None:
                requirements.append({pname: req})
            else:
                plain_props[pname] = pdef
        return plain_props, requirements

    # ── Core conversion ───────────────────────────────────────────────────────

    def _build_simple_prop_def(
        self,
        tosca_type: str,
        schema: dict[str, Any],
        prop_name: str,
        required_props: list[str],
    ) -> dict[str, Any]:
        """Build a basic TOSCA property dict with type, description, required, and default."""
        prop_def: dict[str, Any] = {"type": tosca_type}
        description = schema.get("description") or schema.get("title")
        if description:
            prop_def["description"] = FoldedStr(description)
        prop_def["required"] = prop_name in required_props
        if "default" in schema:
            prop_def["default"] = schema["default"]
        return prop_def

    def _resolve_entry_type(
        self,
        item_schema: dict[str, Any],
        prop_name: str,
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> str:
        """
        Resolve the TOSCA type for a list ``items`` or map ``additionalProperties`` schema.

        Handles circular-import files by inlining the referenced schema as a
        local data type rather than generating an external import.
        """
        if "$ref" in item_schema:
            ref = item_schema["$ref"]
            fp, type_name = self._parse_ref(ref)
            if fp is not None and Path(fp).name in self._no_import_files:
                resolved = self._resolve_for_merge(ref, root_schema)
                resolved = {**resolved, **{k: v for k, v in item_schema.items() if k != "$ref"}}
                return self._inline_type_or_primitive(type_name, resolved, root_schema, data_types)
            return self._ref_to_tosca_type(ref)
        return self.get_tosca_type(item_schema.get("type"), item_schema, root_schema)

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
        if schema.get("properties"):
            safe_name = self.sanitize_name(type_name)
            if safe_name not in data_types:
                data_types[safe_name] = {}  # sentinel: breaks recursive cycles
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
                return self._build_simple_prop_def(allof_type, prop_schema, prop_name, required_props)
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
                return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)

        json_type = prop_schema.get("type")

        # Inline object with properties → create a named data type and reference it
        if prop_schema.get("properties") and json_type in ("object", None):
            nested_type_name = self.sanitize_name(prop_name)
            if nested_type_name not in data_types:
                data_types[nested_type_name] = {}  # sentinel: breaks recursive cycles
                data_types[nested_type_name] = self.schema_to_tosca_data_type(
                    nested_type_name, prop_schema, root_schema, data_types
                )
            return self._build_simple_prop_def(nested_type_name, prop_schema, prop_name, required_props)

        # Primitive type — also carries validation and entry_schema
        tosca_type = self.get_tosca_type(json_type, prop_schema, root_schema)
        prop_def = self._build_simple_prop_def(tosca_type, prop_schema, prop_name, required_props)

        validation = self.build_validation(prop_schema)
        if validation is not None:
            prop_def["validation"] = validation

        if tosca_type == "list":
            prop_def["entry_schema"] = {"type": self._resolve_entry_type(
                prop_schema.get("items", {}), prop_name, root_schema, data_types,
            )}

        if tosca_type == "map":
            add_props = prop_schema.get("additionalProperties", {})
            if isinstance(add_props, dict):
                prop_def["entry_schema"] = {"type": self._resolve_entry_type(
                    add_props, prop_name, root_schema, data_types,
                )}

        return prop_def

    def _schema_to_tosca_type(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
        is_node_type: bool = False,
    ) -> dict[str, Any]:
        """Shared implementation for data-type and node-type conversion."""
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
            all_props = {
                self.sanitize_name(pname): self.schema_to_property(
                    pname, pschema, required_list, root_schema, data_types
                )
                for pname, pschema in properties_schema.items()
            }
            if is_node_type:
                # Properties referencing node types become TOSCA requirements.
                plain_props, requirements = self._split_props_and_requirements(all_props)
                if plain_props:
                    tosca_type["properties"] = plain_props
                if requirements:
                    tosca_type["requirements"] = requirements
            else:
                tosca_type["properties"] = all_props

        # Non-object named types (e.g. a string with enum constraints) — data types only
        if not is_node_type:
            json_type = schema.get("type")
            if json_type and json_type != "object" and not properties_schema:
                tosca_type["derived_from"] = self.get_tosca_type(json_type, schema, root_schema)
                validation = self.build_validation(schema)
                if validation is not None:
                    tosca_type["validation"] = validation
                tosca_type.pop("properties", None)

        return tosca_type

    def schema_to_tosca_data_type(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a JSON Schema object definition to a TOSCA ``data_types`` entry."""
        return self._schema_to_tosca_type(type_name, schema, root_schema, data_types, is_node_type=False)

    def schema_to_tosca_node_type(
        self,
        type_name: str,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a JSON Schema object definition to a TOSCA ``node_types`` entry."""
        return self._schema_to_tosca_type(type_name, schema, root_schema, data_types, is_node_type=True)

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
        new_search_paths = list(search_paths) if search_paths else []
        if base_path != self._base_path or new_search_paths != self._search_paths:
            # Search dirs changed: invalidate all path-dependent caches.
            self._registry = None
            self._id_to_path = {}
            self._file_cache = {}
            self._doc_cache = {}
            self._fname_to_path = {}
        self._base_path = base_path
        self._search_paths = new_search_paths
        self._imports = {}
        self._no_import_files = set()
        self._transitive_back_cache = {}
        self._current_filename = (
            source_path.name if source_path
            else Path(root_schema.get("$id", "")).name
        )
        self._node_type_names = set(node_type_names)
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

        # Post-processing: data types that have properties referencing node types
        # cannot be valid TOSCA data types.  Promote them to node types so the
        # node-type property references can be expressed as TOSCA requirements.
        for name in list(data_types):
            props = data_types[name].get("properties") or {}
            plain_props, requirements = self._split_props_and_requirements(props)
            if not requirements:
                continue
            dtype = data_types.pop(name)
            node_type: dict[str, Any] = {k: v for k, v in dtype.items() if k != "properties"}
            if plain_props:
                node_type["properties"] = plain_props
            node_type["requirements"] = requirements
            node_types[name] = node_type

        # Post-processing: remove property redefinitions in derived types that
        # would relax a required:true constraint set by an ancestor.  TOSCA
        # forbids relaxing constraints, so the inherited definition stands.
        all_types: dict[str, Any] = {**data_types, **node_types}
        for type_def in list(data_types.values()) + list(node_types.values()):
            derived_from = type_def.get("derived_from")
            if not derived_from:
                continue
            # Collect property names that are required:true in any ancestor.
            ancestor_required: set[str] = set()
            visited: set[str] = set()
            stack = [derived_from]
            while stack:
                ancestor_name = stack.pop()
                if ancestor_name in visited or ":" in ancestor_name:
                    continue
                visited.add(ancestor_name)
                ancestor = all_types.get(ancestor_name)
                if not ancestor:
                    continue
                for pname, pdef in ancestor.get("properties", {}).items():
                    if pdef.get("required", False):
                        ancestor_required.add(pname)
                if ancestor.get("derived_from"):
                    stack.append(ancestor["derived_from"])
            # Drop any child property that relaxes a required:true ancestor constraint.
            child_props = type_def.get("properties", {})
            for pname in [p for p in child_props if p in ancestor_required
                          and not child_props[p].get("required", False)]:
                del child_props[pname]
            if not child_props:
                type_def.pop("properties", None)

        # Every node type can be the target of a core:Contains requirement,
        # so add the core:Containable capability to all of them.
        if node_types:
            self._imports["core"] = "Core.yaml"
            for nt in node_types.values():
                nt.setdefault("capabilities", {})["containable"] = {"type": "core:Containable"}

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
