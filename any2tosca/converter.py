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

from .yaml_utils import CommentedOut, FoldedStr

__all__ = ["Converter", "ConversionResult", "convert_schema", "build_tosca_document"]


class ConversionResult(NamedTuple):
    """Holds the output of a schema conversion."""
    data_types: dict[str, Any]
    node_types: dict[str, Any]
    imports: dict[str, str] = {}           # namespace → tosca_file
    relationship_types: dict[str, Any] = {}  # name → TOSCA relationship type dict


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
        self._ref_fnames_cache: dict[str, frozenset[str]] = {}  # abs path → ext fnames
        self._reachable_cache: dict[str, frozenset[str]] = {}   # start_fname → all reachable fnames
        # Per-conversion cache (reset each convert_schema call)
        self._transitive_back_cache: dict[str, bool] = {}  # start_fname → bool (for current _current_filename)
        self._back_import_cache: dict[tuple[str, str], bool] = {}  # (fname, checking_for) → bool
        # Per-conversion state (reset each convert_schema call)
        self._current_file_refs: frozenset[str] = frozenset()  # direct refs of current file (from root_schema)
        self._expanded_link_types: set[str] = set()          # types expanded from Links props
        self._relationship_types: dict[str, Any] = {}        # name → relationship type dict
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
            file_path = self._resolve_fname(fname)
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
        return self._current_filename in self._ref_fnames_for(fname)

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

    def _ref_fnames_for(self, fname: str) -> frozenset[str]:
        """Return the cached set of external filenames directly referenced by *fname*."""
        candidate = self._resolve_fname(fname)
        if candidate is None:
            return frozenset()
        key = str(candidate)
        if key not in self._ref_fnames_cache:
            doc = self._load_cached_doc(fname)
            refs = (
                frozenset(self._collect_ref_fnames(doc)) if doc is not None
                else frozenset()
            )
            self._ref_fnames_cache[key] = refs
        return self._ref_fnames_cache[key]

    def _get_reachable_fnames(self, start_fname: str) -> frozenset[str]:
        """Return all filenames transitively reachable from *start_fname* via ``$ref``.

        The result is cached permanently per starting filename so the BFS is
        only ever run once per unique starting file across all conversions.
        """
        if start_fname in self._reachable_cache:
            return self._reachable_cache[start_fname]
        visited: set[str] = set()
        queue: list[str] = [start_fname]
        while queue:
            fname = queue.pop(0)
            if fname in visited:
                continue
            visited.add(fname)
            for nested in self._ref_fnames_for(fname):
                if nested not in visited:
                    queue.append(nested)
        result = frozenset(visited)
        self._reachable_cache[start_fname] = result
        return result

    def _refs_back_to_current_transitive(self, ref: str) -> bool:
        """
        Return ``True`` if the current file is transitively reachable from
        *ref* via any chain of external ``$ref`` pointers.

        Uses a pre-computed persistent reachable-set so the BFS is performed
        at most once per starting file across all conversions.  The check
        itself is an O(1) frozenset membership test: is ``_current_filename``
        in the reachable set of ``start_fname``?

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
        # O(1) frozenset lookup: is _current_filename reachable from start_fname?
        result = self._current_filename in self._get_reachable_fnames(start_fname)
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
        fname = Path(file_part).name
        if fname in self._no_import_files:
            return type_name
        # On-demand circular-import check: catches refs inside files that were
        # already inlined (and therefore not present in the root schema's refs,
        # so missed by the initial _collect_no_import_files pre-scan).
        stem = Path(fname).stem
        if self._VER_RE.match(stem):
            # If the unversioned base of this versioned schema is already
            # no-import, this versioned schema is part of the same family
            # and should also be treated as no-import.  This prevents
            # circular imports when an inlined circular schema's anyOf
            # references versioned variants (e.g. inlined Control.json
            # has anyOf → Control.v1_7_0.json, but Control.json is circular
            # so all Control versioned schemas should be suppressed too).
            base_fname = re.sub(r"\.v\d.*$", "", stem) + Path(fname).suffix
            if base_fname in self._no_import_files:
                self._mark_no_import(fname)
                return type_name
            if self._refs_back_to_current(ref):
                self._mark_no_import(fname)
                return type_name
        elif self._would_create_back_import(fname, self._current_filename):
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

        # Fast path: load file via cache, then navigate to the fragment.
        file_no_frag, _, fragment = ref.partition("#")
        if file_no_frag:
            fname = Path(file_no_frag).name
            doc = self._load_cached_doc(fname)
            if doc is not None:
                if fragment:
                    node: Any = doc
                    for part in fragment.lstrip("/").split("/"):
                        if isinstance(node, dict) and part in node:
                            node = node[part]
                        else:
                            node = {}
                            break
                    return node if isinstance(node, dict) else {}
                return doc

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
          (``BaseName.vX_Y_Z``), including circular ones (they can still be
          picked as ``derived_from`` via the file-root bypass).
        - ``"circular"``    — pure ``$ref`` to a non-versioned external file
          that is in ``_no_import_files`` or references the current file back.
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
        stem = Path(uri.split("/")[-1]).stem
        if self._VER_RE.match(stem):
            # Versioned refs are always classified as "versioned" regardless of
            # circularity — _merge_allof uses the file-root bypass to import the
            # winning version even when it back-refs the current file.
            if self._refs_back_to_current(ref):
                if fname:
                    self._mark_no_import(fname)
            return "versioned"
        else:
            if fname in self._no_import_files:
                return "circular"
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
                # If the best versioned ref was circular, _ref_to_tosca_type
                # returned a bare type name (no import recorded).  Try the
                # file-root bypass: versioned files are always file-root types
                # and the current file directly refs them (they're in its anyOf),
                # so as long as the current file has an explicit root ($ref) or
                # the versioned file doesn't ref the current file back in a way
                # that would create a TOSCA cycle, we can force-import it.
                if best and ":" not in derived_from:
                    file_part, type_name_from_ref = self._parse_ref(best)
                    bypassed = False
                    if file_part is not None:
                        fname_best = Path(file_part).name
                        if fname_best in self._no_import_files and self._is_file_root_type(fname_best, type_name_from_ref or ""):
                            _current_refs_fname = fname_best in self._current_file_refs
                            _current_is_explicit_root = self._is_explicit_file_root(self._current_filename)
                            _unsafe_bypass = _current_refs_fname and not _current_is_explicit_root
                            # Also block if the versioned file refs our own root type back —
                            # that would cause the versioned YAML to import ours when
                            # independently converted, creating a TOSCA cycle.
                            _back_imports = self._versioned_refs_our_root_type(fname_best)
                            # Block when fname_best's unversioned base is circular AND
                            # fname_best was not directly referenced by the current file.
                            # This catches inlined circular schema anyOf members:
                            # e.g. when Sensor.json (circular) is inlined and its anyOf
                            # has Sensor.v1_12_0.json — we block the bypass since the
                            # ref was discovered through inlining, not direct reference.
                            _base_circular = False
                            _base_fname = re.sub(r"\.v\d.*$", "", Path(fname_best).stem) + Path(fname_best).suffix
                            if _base_fname != fname_best and _base_fname in self._no_import_files and not _current_refs_fname:
                                _base_circular = True
                            if not _unsafe_bypass and not _back_imports and not _base_circular:
                                namespace = self._namespace_for(file_part)
                                self._imports[namespace] = self._tosca_file_for(file_part)
                                derived_from = f"{namespace}:{type_name_from_ref}"
                                bypassed = True
                    if not bypassed:
                        # Circular and not a file-root type (e.g. a sub-definition
                        # like AdditionalVersions inside a versioned file) — cannot
                        # import, so the bare derived_from would be a self-reference.
                        # Fall back to merging all versioned refs inline instead.
                        derived_from = None
                        to_merge_refs.extend(next(iter(versioned_groups.values())))
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

    def _versioned_refs_our_root_type(self, versioned_fname: str) -> bool:
        """Return True if *versioned_fname* contains a ``$ref`` pointing to the
        current file's root definition (e.g.
        ``EthernetInterface.json#/definitions/EthernetInterface``).

        When this is true, the versioned file would import the current file's
        YAML when independently converted (the file-root bypass would fire),
        so using it as ``derived_from`` from the current file would create a
        circular TOSCA import.
        """
        if not self._current_filename:
            return False
        cur_doc = self._load_cached_doc(self._current_filename)
        if cur_doc is None:
            return False
        cur_root_ref = cur_doc.get("$ref", "")
        if not cur_root_ref.startswith("#/definitions/"):
            return False
        cur_root_type = cur_root_ref[len("#/definitions/"):]
        versioned_doc = self._load_cached_doc(versioned_fname)
        if versioned_doc is None:
            return False

        def _has_root_ref(obj: Any) -> bool:
            if isinstance(obj, dict):
                ref = obj.get("$ref", "")
                if ref:
                    fp, tn = self._parse_ref(ref)
                    if fp is not None and Path(fp).name == self._current_filename and tn == cur_root_type:
                        return True
                return any(_has_root_ref(v) for v in obj.values())
            if isinstance(obj, list):
                return any(_has_root_ref(v) for v in obj)
            return False

        return _has_root_ref(versioned_doc)

    def _would_create_back_import(
        self,
        fname: str,
        checking_for: str,
        _stack: frozenset[tuple[str, str]] | None = None,
    ) -> bool:
        """Return True if independently converting *fname* would eventually produce
        a TOSCA import of *checking_for*, which would create a circular import.

        This detects the pattern where:
        1. *fname* has a versioned schema V that would be selected as ``derived_from``
           when *fname* is independently converted (i.e. V does NOT ref *fname*'s
           root definition type, so ``_versioned_refs_our_root_type`` would not block it).
        2. That versioned schema V directly references *checking_for* or its
           unversioned base — meaning when V.yaml is generated, it would import
           checking_for.yaml (or its base's yaml, which in turn imports the versioned
           checking_for.yaml via derived_from).

        Example: Sensor.json has Sensor.v1_12_0.json, which does NOT ref
        Sensor.json#/definitions/Sensor (so it would be selected as derived_from),
        and Sensor.v1_12_0.json directly refs Control.json (the base of
        Control.v1_7_0.json).  When Sensor.v1_12_0.yaml is generated it imports
        Control.yaml, which imports Control.v1_7_0.yaml — creating the cycle
        Control.v1_7_0.yaml → Sensor.yaml → … → Control.v1_7_0.yaml.

        Symmetric cycles are broken lexicographically: when mutual recursion is
        detected (the same ``(fname, checking_for)`` key appears in ``_stack``),
        the call returns ``fname > checking_base``.  This means the
        lexicographically smaller family (e.g. Control) is the *winner* (returns
        False → allowed to import the other family), and the larger one (e.g.
        Sensor) is the *loser* (returns True → the node reference is commented out).
        """
        if _stack is None:
            _stack = frozenset()
        key = (fname, checking_for)
        if key in self._back_import_cache:
            return self._back_import_cache[key]
        # Mutual-recursion sentinel: same call already in the stack → cycle detected.
        # Break symmetry lexicographically: the larger fname loses.
        if key in _stack:
            checking_stem_r = Path(checking_for).stem
            if self._VER_RE.match(checking_stem_r):
                checking_base_r = re.sub(r"\.v\d.*$", "", checking_stem_r) + Path(checking_for).suffix
            else:
                checking_base_r = checking_for
            result = fname > checking_base_r
            self._back_import_cache[key] = result
            return result
        _stack = _stack | {key}

        fname_doc = self._load_cached_doc(fname)
        if fname_doc is None:
            return False
        # Get fname's root definition type name
        fname_root_ref = fname_doc.get("$ref", "")
        fname_root_type = (
            fname_root_ref[len("#/definitions/"):]
            if fname_root_ref.startswith("#/definitions/")
            else ""
        )
        # Determine the unversioned "base" of checking_for
        checking_stem = Path(checking_for).stem
        if self._VER_RE.match(checking_stem):
            base_name = re.sub(r"\.v\d.*$", "", checking_stem)
            checking_base = base_name + Path(checking_for).suffix
        else:
            checking_base = checking_for
        # Iterate over versioned schemas directly referenced by fname
        for versioned_fname in self._ref_fnames_for(fname):
            if not self._VER_RE.match(Path(versioned_fname).stem):
                continue
            # Would this versioned schema be blocked as derived_from?
            # Blocked when it refs fname's root type (same logic as _versioned_refs_our_root_type)
            if fname_root_type:
                v_doc = self._load_cached_doc(versioned_fname)
                if v_doc is not None:
                    def _refs_fname_root(obj: Any) -> bool:
                        if isinstance(obj, dict):
                            ref = obj.get("$ref", "")
                            if ref:
                                fp, tn = self._parse_ref(ref)
                                if fp is not None and Path(fp).name == fname and tn == fname_root_type:
                                    return True
                            return any(_refs_fname_root(v) for v in obj.values())
                        if isinstance(obj, list):
                            return any(_refs_fname_root(v) for v in obj)
                        return False
                    if _refs_fname_root(v_doc):
                        continue  # would be blocked as derived_from → skip
            # This versioned schema would be imported as derived_from.
            # Check if it directly refs checking_for or its unversioned base.
            v_direct_refs = self._ref_fnames_for(versioned_fname)
            if checking_for in v_direct_refs or checking_base in v_direct_refs:
                # Counter-check: would converting checking_base also create a
                # back-import of fname?  If so the cycle is mutual and the
                # lexicographic loser (larger fname) is the one that's blocked.
                if self._would_create_back_import(checking_base, versioned_fname, _stack):
                    continue  # mutual cycle — this hop is already handled on the other side
                self._back_import_cache[key] = True
                return True
        self._back_import_cache[key] = False
        return False

    def _is_explicit_file_root(self, fname: str) -> bool:
        """Return True only if *fname* has an explicit top-level ``$ref`` to a definition.

        Unlike :meth:`_is_file_root_type`, this does **not** apply the Redfish
        heuristic (base-stem match).  It is used to distinguish resource schemas
        with a definitive ``$ref: "#/definitions/Foo"`` (e.g.
        ``Container.v1_0_1.json``) from utility/library schemas that omit the
        top-level ``$ref`` (e.g. ``Resource.json``).
        """
        doc = self._load_cached_doc(fname)
        if doc is None:
            return False
        return "#/definitions/" in doc.get("$ref", "")

    def _external_defn_has_node_props(self, fname: str, type_name: str) -> bool:
        """Return True if the definition *type_name* in external file *fname* has
        any properties whose item or value type is a file-root node type.

        This handles the case where a non-root type (e.g. ``RedundantGroup`` in
        ``Redundancy.v1_7_0.json``) has list/single properties referencing other
        node types (e.g. lists of ``Resource.json#/definitions/Resource``).
        When converted independently, those properties become requirements, making
        the type a node type.  We need to detect this here so we can classify the
        type correctly when processing a referencing file.
        """
        doc = self._load_cached_doc(fname)
        if doc is None:
            return False
        defn = doc.get("definitions", {}).get(type_name, {})
        props = defn.get("properties", {})
        for pdef in props.values():
            # Single $ref directly on property, or list via items.$ref
            ref = pdef.get("$ref") or pdef.get("items", {}).get("$ref")
            if not ref:
                continue
            fp, tn = self._parse_ref(ref)
            if fp is not None and tn and self._is_file_root_type(Path(fp).name, tn):
                return True
        return False

    def _is_node_type_tosca(self, tosca_type: str) -> bool:
        """Return True if *tosca_type* refers to a TOSCA node type rather than a data type.

        - TOSCA primitives are never node types.
        - ``CommentedOut`` values are always node types (only created for confirmed
          file-root node types in circular-import losers).
        - Local (unqualified) types are checked against ``_node_type_names``.
        - Namespaced types (``namespace:TypeName``) are checked against the
          root definition of the corresponding external JSON Schema file; if the
          type is not the file root, also check whether its definition has any
          properties that would become requirements (i.e. it would be classified
          as a node type when the external file is independently converted).
        """
        if isinstance(tosca_type, CommentedOut):
            return True
        if tosca_type in self._TOSCA_PRIMITIVES:
            return False
        if ":" in tosca_type:
            namespace, type_name = tosca_type.split(":", 1)
            tosca_file = self._imports.get(namespace)
            if tosca_file is None:
                return False
            fname = Path(tosca_file).stem + ".json"
            if self._is_file_root_type(fname, type_name):
                return True
            return self._external_defn_has_node_props(fname, type_name)
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

    def _expand_links_prop(
        self,
        node_type_name: str,
        links_ref: str,
        root_schema: dict[str, Any],
        data_types: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Expand a Redfish ``Links`` property into TOSCA ``DependsOn`` requirements.

        Resolves the Links definition identified by *links_ref*, processes each
        of its properties, and returns a list of TOSCA requirement dicts for
        those that reference node types.  The requirements use
        ``core:DependsOn`` / ``core:Feature`` (or a derived relationship type
        if the Links definition also contains non-reference properties).

        Non-node-type properties from the Links definition are collected into a
        ``{NodeTypeName}DependsOn`` relationship type that derives from
        ``core:DependsOn`` and is stored in ``_relationship_types``.

        Also records the type name in ``_expanded_link_types`` so the now-
        unused Links data/node type can be removed after conversion.
        """
        _, type_name = self._parse_ref(links_ref)
        if type_name:
            self._expanded_link_types.add(type_name)

        resolved = self._resolve_for_merge(links_ref, root_schema)

        # Separate node-type refs (→ requirements) from plain properties.
        pending: list[tuple[str, str, list[Any], str | None]] = []  # (safe_name, node_type, count_range, description)
        extra_props: dict[str, Any] = {}

        for pname, pschema in resolved.get("properties", {}).items():
            safe_name = self.sanitize_name(pname)
            prop_def = self.schema_to_property(pname, pschema, [], root_schema, data_types)
            prop_type = prop_def.get("type", "")
            if prop_type == "list":
                entry_type = (prop_def.get("entry_schema") or {}).get("type", "")
                if not self._is_node_type_tosca(entry_type):
                    extra_props[safe_name] = prop_def
                    continue
                pending.append((safe_name, entry_type, [0, "UNBOUNDED"], prop_def.get("description")))
            elif self._is_node_type_tosca(prop_type):
                pending.append((safe_name, prop_type, [0, 1], prop_def.get("description")))
            else:
                extra_props[safe_name] = prop_def

        self._imports["core"] = "Core.yaml"

        # If there are extra (non-ref) properties, create a dedicated relationship
        # type that derives from core:DependsOn and carries those properties.
        if extra_props and pending:
            rel_type_name = f"{node_type_name}DependsOn"
            self._relationship_types[rel_type_name] = {
                "derived_from": "core:DependsOn",
                "properties": extra_props,
            }
            relationship = rel_type_name
        else:
            relationship = "core:DependsOn"

        requirements: list[dict[str, Any]] = []
        for safe_name, node_type, count_range, description in pending:
            req: dict[str, Any] = {
                "node": node_type,
                "capability": "core:Feature",
                "relationship": relationship,
                "count_range": count_range,
            }
            if description:
                req["description"] = description
            requirements.append({safe_name: req})
        return requirements

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
                # For list items (containment), force a namespaced import even
                # when the file is circular — node types cannot be inlined and
                # a collection must know its member type.  Register the import
                # directly to bypass _ref_to_tosca_type's no-import guard.
                # Guard: only bypass when the current file directly references
                # fname — if the ref was discovered through an inline-merged
                # schema, bypassing would create a circular TOSCA import.
                fname = Path(fp).name
                _current_refs_fname = fname in self._current_file_refs
                _current_is_explicit_root = self._is_explicit_file_root(self._current_filename)
                _unsafe_bypass = _current_refs_fname and not _current_is_explicit_root
                _back_import = (
                    not _unsafe_bypass
                    and _current_refs_fname
                    and self._would_create_back_import(fname, self._current_filename)
                )
                if self._is_file_root_type(fname, type_name or "") and not _unsafe_bypass and _current_refs_fname and not _back_import:
                    namespace = self._namespace_for(fp)
                    self._imports[namespace] = self._tosca_file_for(fp)
                    return f"{namespace}:{type_name}"
                # Loser in a symmetric circular dependency: emit the type reference
                # as a commented-out value so the intent is visible in the output.
                if _back_import and self._is_file_root_type(fname, type_name or ""):
                    return CommentedOut(f"{self._namespace_for(fp)}:{type_name}")
                # File-root node type discovered via an inlined external schema (not
                # a direct ref of the current file).  We cannot inline a node type as
                # a data type, and importing would be circular.  Emit CommentedOut.
                if (self._is_file_root_type(fname, type_name or "")
                        and not _unsafe_bypass and not _current_refs_fname):
                    return CommentedOut(f"{self._namespace_for(fp)}:{type_name}")
                resolved = self._resolve_for_merge(ref, root_schema)
                resolved = {**resolved, **{k: v for k, v in item_schema.items() if k != "$ref"}}
                return self._inline_type_or_primitive(type_name, resolved, root_schema, data_types)
            ret = self._ref_to_tosca_type(ref)
            # _ref_to_tosca_type may have just marked the file as no-import
            # on-the-fly (because _would_create_back_import returned True).
            # If the file was not directly referenced by the current schema,
            # the ref was discovered via an inlined external schema — emit
            # CommentedOut so the intent is visible rather than a bare type.
            fname = Path(fp).name if fp else ""
            if fname and fname in self._no_import_files and ":" not in ret:
                _current_refs_fname = fname in self._current_file_refs
                _current_is_explicit_root = self._is_explicit_file_root(self._current_filename)
                _unsafe_bypass = _current_refs_fname and not _current_is_explicit_root
                if (self._is_file_root_type(fname, type_name or "")
                        and not _unsafe_bypass and not _current_refs_fname):
                    return CommentedOut(f"{self._namespace_for(fp)}:{type_name}")
            return ret
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
            # If this type is a known node type (e.g. it is the current file's
            # root type, or was promoted by _mark_no_import), don't create a
            # duplicate data type entry — the main conversion pass will define
            # it as a node type via _add.
            if safe_name in self._node_type_names:
                return safe_name
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
            file_part, type_name_from_ref = self._parse_ref(ref)
            if file_part is not None and Path(file_part).name in self._no_import_files:
                # Referenced file is circular.  For file-root node types (e.g.
                # a resource collection referenced as a scalar property) we can
                # still force the import — the independently-generated YAML for
                # that file will not import the current file back, because the
                # current file directly refs it and is therefore blocked from
                # the other file's perspective.  This mirrors the same bypass
                # used in _resolve_entry_type for list items.
                #
                # Unsafe-bypass guard: skip the file-root bypass when the
                # referenced file directly refs the current file AND the current
                # file is not an explicit-root resource schema.  This prevents
                # utility library schemas (e.g. Resource.json, which refs
                # LogEntry.json directly) from importing the current file back
                # when processing LogEntry.json — that would create a cycle:
                # LogEntry.yaml → Resource.yaml → LogEntry.yaml.
                fname = Path(file_part).name
                # Does the current file directly ref fname? Use _current_file_refs
                # (pre-computed from the in-memory root schema) so this works even
                # when the source file is not present on disk.
                _current_refs_fname = fname in self._current_file_refs
                _current_is_explicit_root = self._is_explicit_file_root(self._current_filename)
                # Unsafe-bypass guard: when the current file is a utility library
                # (no explicit top-level $ref) that directly refs fname, then
                # fname's independently-generated YAML would also import the
                # current file back — bypassing would create a cycle.
                _unsafe_bypass = _current_refs_fname and not _current_is_explicit_root
                # Back-import guard: also block when independently converting
                # fname.yaml would eventually produce a TOSCA import of current.yaml
                # (e.g. fname has a versioned schema that refs current's base schema,
                # creating a multi-hop cycle like Sensor→v1_12_0→Control→v1_7_0→Sensor).
                _back_import = (
                    not _unsafe_bypass
                    and self._would_create_back_import(fname, self._current_filename)
                )
                if self._is_file_root_type(fname, type_name_from_ref or "") and not _unsafe_bypass and not _back_import:
                    namespace = self._namespace_for(file_part)
                    self._imports[namespace] = self._tosca_file_for(file_part)
                    ref_type_name = f"{namespace}:{type_name_from_ref}"
                    ref_schema = self.resolve_ref(ref, root_schema)
                    merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                    return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                # Loser in a symmetric circular dependency: emit the type reference
                # as a commented-out value so the intent is visible in the output.
                if _back_import and self._is_file_root_type(fname, type_name_from_ref or ""):
                    namespace = self._namespace_for(file_part)
                    ref_type_name = CommentedOut(f"{namespace}:{type_name_from_ref}")
                    ref_schema = self.resolve_ref(ref, root_schema)
                    merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                    return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                # Not a file-root type (or bypass blocked): check if fname
                # directly refs the current file — if so, a safe one-directional
                # import is possible (step-1 allow).
                # Example: Resource.json refs LogEntry.json directly, so when
                # converting LogEntry.json we can safely import Resource.yaml
                # because Resource.yaml itself won't import LogEntry.yaml back.
                # Guard: only allow when fname is NOT an explicit-root schema.
                # If fname is explicit-root (e.g. a versioned resource schema),
                # independently converting it will also import the current file
                # back (file-root bypass fires), creating a circular import.
                if not _unsafe_bypass and not _current_refs_fname and not self._is_explicit_file_root(fname) and self._current_filename in self._ref_fnames_for(fname):
                    # fname directly refs the current file → safe to import
                    namespace = self._namespace_for(file_part)
                    self._imports[namespace] = self._tosca_file_for(file_part)
                    ref_type_name = f"{namespace}:{type_name_from_ref}"
                    ref_schema = self.resolve_ref(ref, root_schema)
                    merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                    return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                # Inline the schema — resolve and fall through to normal type detection.
                # Use _resolve_for_merge (not resolve_ref) so that any local
                # #/... refs inside the resolved schema are rewritten to
                # filename#/... — otherwise they resolve against root_schema
                # where the types don't exist.
                ref_schema = self._resolve_for_merge(ref, root_schema)
                prop_schema = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                # fall through to inline processing below
            else:
                ref_type_name = self._ref_to_tosca_type(ref)
                # _ref_to_tosca_type may have just discovered this file is
                # circular (on-the-fly, not pre-scanned) and added it to
                # _no_import_files, returning only a bare type name.  Detect
                # this case and apply the same circular handling as above
                # (including the unsafe-bypass guard and step-1 allow).
                fname = Path(file_part).name if file_part else ""
                if fname and fname in self._no_import_files:
                    _current_refs_fname = fname in self._current_file_refs
                    _current_is_explicit_root = self._is_explicit_file_root(self._current_filename)
                    _unsafe_bypass = _current_refs_fname and not _current_is_explicit_root
                    # On-the-fly bypass: only fire when the current file directly
                    # refs fname (_current_refs_fname).  If the ref was discovered
                    # through an inline-merged schema (not a direct ref), we
                    # cannot guarantee the independently-generated YAML for fname
                    # won't import the current file back — so we inline instead.
                    if self._is_file_root_type(fname, type_name_from_ref or "") and not _unsafe_bypass and _current_refs_fname:
                        # Namespace was already registered by _ref_to_tosca_type
                        # only if it was not circular — here it was circular so
                        # ref_type_name is bare; rebuild with namespace.
                        namespace = self._namespace_for(file_part)
                        self._imports[namespace] = self._tosca_file_for(file_part)
                        ref_type_name = f"{namespace}:{type_name_from_ref}"
                        ref_schema = self.resolve_ref(ref, root_schema)
                        merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                        return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                    # On-the-fly loser: circular file-root type encountered via an
                    # inlined schema (not a direct ref of the current file).  A bare
                    # type name would be unresolvable, so emit as CommentedOut so the
                    # intent is visible in the output.
                    if self._is_file_root_type(fname, type_name_from_ref or "") and not _unsafe_bypass and not _current_refs_fname:
                        namespace = self._namespace_for(file_part)
                        ref_type_name = CommentedOut(f"{namespace}:{type_name_from_ref}")
                        ref_schema = self.resolve_ref(ref, root_schema)
                        merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                        return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                    # Step-1 allow: fname directly refs current file → safe import.
                    # Only when fname is not explicit-root: if fname is explicit-root,
                    # independently converting it would also import current back → cycle.
                    if not _unsafe_bypass and not _current_refs_fname and not self._is_explicit_file_root(fname) and self._current_filename in self._ref_fnames_for(fname):
                        namespace = self._namespace_for(file_part)
                        self._imports[namespace] = self._tosca_file_for(file_part)
                        ref_type_name = f"{namespace}:{type_name_from_ref}"
                        ref_schema = self.resolve_ref(ref, root_schema)
                        merged = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                        return self._build_simple_prop_def(ref_type_name, merged, prop_name, required_props)
                    # Not a file-root type and no safe import — inline the schema
                    # instead of using the bare type name from _ref_to_tosca_type.
                    ref_schema = self._resolve_for_merge(ref, root_schema)
                    prop_schema = {**ref_schema, **{k: v for k, v in prop_schema.items() if k != "$ref"}}
                    # fall through to inline processing below
                else:
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
            links_requirements: list[dict[str, Any]] = []
            regular_props: dict[str, Any] = {}
            for pname, pschema in properties_schema.items():
                # Detect the Redfish Links pattern: a property named "Links"
                # whose $ref resolves to a type also named "Links".  Expand
                # it into core:DependsOn requirements rather than a property.
                if (is_node_type and pname == "Links"
                        and isinstance(pschema, dict) and "$ref" in pschema
                        and self._parse_ref(pschema["$ref"])[1] == "Links"):
                    links_requirements.extend(
                        self._expand_links_prop(type_name, pschema["$ref"], root_schema, data_types)
                    )
                else:
                    regular_props[pname] = pschema

            all_props = {
                self.sanitize_name(pname): self.schema_to_property(
                    pname, pschema, required_list, root_schema, data_types
                )
                for pname, pschema in regular_props.items()
            }
            if is_node_type:
                # Properties referencing node types become TOSCA requirements.
                plain_props, requirements = self._split_props_and_requirements(all_props)
                requirements.extend(links_requirements)
                if plain_props:
                    tosca_type["properties"] = plain_props
                if requirements:
                    tosca_type["requirements"] = requirements
            else:
                tosca_type["properties"] = all_props

        # All node types expose a core:Feature capability so they can be targeted
        # by core:DependsOn requirements (e.g. from the Redfish Links pattern).
        if is_node_type:
            self._imports["core"] = "Core.yaml"
            tosca_type["capabilities"] = {"feature": {"type": "core:Feature"}}

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
            self._ref_fnames_cache = {}
            self._reachable_cache = {}
        self._base_path = base_path
        self._search_paths = new_search_paths
        self._imports = {}
        self._no_import_files = set()
        self._expanded_link_types = set()
        self._relationship_types = {}
        self._transitive_back_cache = {}
        self._back_import_cache = {}
        self._current_filename = (
            source_path.name if source_path
            else Path(root_schema.get("$id", "")).name
        )
        # Pre-compute direct refs from the in-memory root schema so that
        # _current_refs_fname checks work even when the source file is not
        # present on disk (where _ref_fnames_for would return empty).
        self._current_file_refs = frozenset(self._collect_ref_fnames(root_schema))
        self._node_type_names = set(node_type_names)
        self._collect_no_import_files(root_schema)
        data_types: dict[str, Any] = {}
        node_types: dict[str, Any] = {}

        def _add(tosca_name: str, schema: dict[str, Any], root: dict[str, Any]) -> None:
            if tosca_name in self._node_type_names:
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

        # Post-processing: data types whose derived_from resolves to a node type
        # must also be node types (TOSCA does not allow a data type to derive from
        # a node type).  This covers cases like Redundancy.yaml's RedundantGroup
        # which derives from rdndncy:RedundantGroup (a node type in v1_7_0.yaml).
        for name in list(data_types):
            derived_from = data_types[name].get("derived_from", "")
            if derived_from and self._is_node_type_tosca(derived_from):
                dtype = data_types.pop(name)
                node_type = dict(dtype)
                self._imports["core"] = "Core.yaml"
                if "capabilities" not in node_type:
                    node_type["capabilities"] = {
                        "feature": {"type": "core:Feature"},
                        "containable": {"type": "core:Containable"},
                    }
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

        # Remove Links container types that were expanded inline into parent
        # node types as DependsOn requirements — they are no longer referenced.
        for name in self._expanded_link_types:
            data_types.pop(name, None)
            node_types.pop(name, None)

        # Every node type can be the target of a core:Contains requirement,
        # so add the core:Containable capability to all of them.
        if node_types:
            self._imports["core"] = "Core.yaml"
            for nt in node_types.values():
                nt.setdefault("capabilities", {})["containable"] = {"type": "core:Containable"}

        return ConversionResult(
            data_types=data_types,
            node_types=node_types,
            imports=dict(self._imports),
            relationship_types=dict(self._relationship_types),
        )

    def build_tosca_document(
        self,
        data_types: dict[str, Any],
        node_types: dict[str, Any] | None = None,
        relationship_types: dict[str, Any] | None = None,
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
        if relationship_types:
            doc["relationship_types"] = relationship_types
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
    relationship_types: dict[str, Any] | None = None,
    imports: dict[str, str] | None = None,
) -> dict[str, Any]:
    return Converter().build_tosca_document(data_types, node_types, relationship_types, imports)
