"""
formats.py
----------
Input formats.  Every command runs the same pipeline (``cli.run``): load each
input file, normalize it to a root JSON Schema, then split and convert it.  An
``InputFormat`` holds only what differs: which files a directory contributes,
how a file is loaded, the normalizers that turn it into JSON Schema, and the
format's defaults.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .converter import Conventions
from .kubernetes import EXTENSION_METADATA as KUBERNETES_EXTENSION_METADATA
from .kubernetes import RULES_PATH as KUBERNETES_RULES_PATH
from .kubernetes import KubernetesNormalizer
from .openapi import OpenAPIExtractor
from .redfish import RedfishNormalizer
from .yang import YANGNormalizer

__all__ = [
    "Normalized", "InputFormat",
    "JSON_SCHEMA", "REDFISH", "OPENAPI", "KUBERNETES", "YANG", "FORMATS",
]


@dataclass(frozen=True)
class Normalized:
    """An input file as JSON Schema: what the splitter and the converter take."""
    root_schema: dict[str, Any]
    node_type_names: frozenset[str]
    top_level_name: str


#: A normalizer step.  The first step of a format receives the loaded document;
#: each later step receives the previous step's ``Normalized``.  Both also get
#: the input path.
Normalizer = Callable[[Any, Path], Normalized]


@dataclass(frozen=True)
class InputFormat:
    """What one input format adds to the general pipeline."""
    name: str                                   # format identifier
    label: str                                  # how messages name the input files
    extensions: tuple[str, ...]                 # the files a directory contributes
    load: Callable[[Path], Any]                 # file → document
    normalizers: tuple[Normalizer, ...]         # document → Normalized, in order
    parse_errors: tuple[type[BaseException], ...]  # load/normalize errors reported as parse errors
    conventions: Conventions = Conventions()    # relationship model (the --relationship-model
                                                # default), Redfish rules, extension metadata
                                                # (added to --extension-metadata)
    required_relationship_model: str | None = None  # the only model the format accepts
    split_rules: Path | None = None             # used when --split is not given
    allow_skipped_refs: bool = False            # references to skipped definitions only warn

    def normalize(self, document: Any, path: Path) -> Normalized:
        """Run the format's normalizers on a loaded document."""
        result: Any = document
        for step in self.normalizers:
            result = step(result, path)
        return result


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_json_or_yaml(path: Path) -> Any:
    """Load a JSON file, or a YAML file by its extension."""
    with open(path, "r", encoding="utf-8") as fh:
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(fh)
        return json.load(fh)


def load_path(path: Path) -> Path:
    """For formats whose normalizer reads the file itself."""
    return path


_JSON_ERRORS = (json.JSONDecodeError, yaml.YAMLError)


# ── Normalizer steps ──────────────────────────────────────────────────────────

def _json_schema(document: Any, path: Path) -> Normalized:
    """Plain JSON Schema: the document is the root schema; no node types."""
    return Normalized(document, frozenset(), document.get("title") or path.stem)


def _redfish(document: Any, path: Path) -> Normalized:
    root, names = RedfishNormalizer().normalize(document)
    return Normalized(root, frozenset(names), next(iter(names), None) or path.stem)


def _openapi(document: Any, path: Path) -> Normalized:
    root, names = OpenAPIExtractor().extract(document)
    title = (document.get("info") or {}).get("title")
    return Normalized(root, frozenset(names), title or path.stem)


def _kubernetes(previous: Normalized, path: Path) -> Normalized:
    root, names = KubernetesNormalizer().normalize(previous.root_schema, previous.node_type_names)
    return Normalized(root, frozenset(names), previous.top_level_name)


def _yang(path: Path, _: Path) -> Normalized:
    normalizer = YANGNormalizer()
    root, names = normalizer.normalize(path)
    return Normalized(root, frozenset(names), normalizer.module_name or path.stem)


# ── The formats ───────────────────────────────────────────────────────────────

JSON_SCHEMA = InputFormat(
    name="jsonschema", label="JSON Schema", extensions=(".json",),
    load=load_json_or_yaml, normalizers=(_json_schema,), parse_errors=_JSON_ERRORS,
    conventions=Conventions(relationship_model="value"),
)

REDFISH = InputFormat(
    name="redfish", label="Redfish JSON Schema", extensions=(".json",),
    load=load_json_or_yaml, normalizers=(_redfish,), parse_errors=_JSON_ERRORS,
    conventions=Conventions(relationship_model="links", redfish=True),
)

OPENAPI = InputFormat(
    name="openapi", label="OpenAPI", extensions=(".json", ".yaml", ".yml"),
    load=load_json_or_yaml, normalizers=(_openapi,), parse_errors=_JSON_ERRORS,
    conventions=Conventions(relationship_model="value"),
)

KUBERNETES = InputFormat(
    name="kubernetes", label="OpenAPI", extensions=(".json", ".yaml", ".yml"),
    load=load_json_or_yaml, normalizers=(_openapi, _kubernetes), parse_errors=_JSON_ERRORS,
    conventions=Conventions(relationship_model="value",
                            extension_metadata=KUBERNETES_EXTENSION_METADATA),
    required_relationship_model="value", split_rules=KUBERNETES_RULES_PATH,
    # A GA type may reference alpha or beta types the rules skip (1.37: batch/v1
    # JobSchedulingConfiguration uses scheduling.k8s.io/v1alpha3 types); those
    # fields fall back to their JSON type.
    allow_skipped_refs=True,
)

YANG = InputFormat(
    name="yang", label="YANG", extensions=(".yang",),
    load=load_path, normalizers=(_yang,), parse_errors=(Exception,),
    conventions=Conventions(relationship_model="value"),
)

#: Every format by name: the values of ``any2tosca --format``.
FORMATS: dict[str, InputFormat] = {
    fmt.name: fmt for fmt in (JSON_SCHEMA, OPENAPI, KUBERNETES, REDFISH, YANG)
}
