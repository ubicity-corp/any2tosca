"""
kubernetes.py
-------------
Kubernetes conventions for converting the Kubernetes OpenAPI specification
(``oas2tosca --kubernetes``).  See ``docs/kubernetes.md``.

``KubernetesNormalizer`` runs after ``OpenAPIExtractor`` and adjusts the
resource (node-type) schemas: the group-version-kind becomes a single map,
the ``apiVersion`` and ``kind`` properties go (the node type determines them),
and ``status`` is marked ``readOnly`` so that it converts to an attribute.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import copy
from collections.abc import Collection
from pathlib import Path
from typing import Any

__all__ = ["EXTENSION_METADATA", "GVK", "RULES_PATH", "KubernetesNormalizer"]

#: The group-version-kind extension.
GVK = "x-kubernetes-group-version-kind"

#: The extensions copied into TOSCA metadata.
EXTENSION_METADATA: tuple[str, ...] = (
    GVK,
    "x-kubernetes-list-type",
    "x-kubernetes-list-map-keys",
    "x-kubernetes-map-type",
)

#: The packaged split rules: one unit per GA API group.
RULES_PATH: Path = Path(__file__).parent / "rules" / "kubernetes.yaml"

#: Properties of a resource that its node type determines.
_TYPE_DETERMINED = ("apiVersion", "kind")


class KubernetesNormalizer:
    """
    Adjusts the resource schemas of a Kubernetes OpenAPI root schema.

    Usage::

        root_schema, node_type_names = OpenAPIExtractor().extract(spec)
        root_schema, node_type_names = KubernetesNormalizer().normalize(
            root_schema, node_type_names)
    """

    def normalize(
        self, root_schema: dict[str, Any], node_type_names: Collection[str]
    ) -> tuple[dict[str, Any], set[str]]:
        """
        Return a copy of *root_schema* with every node-type schema adjusted,
        and the node-type names (unchanged).

        For each node-type definition:

        1. a single-entry ``x-kubernetes-group-version-kind`` list is replaced
           by its entry, so the metadata is a map;
        2. the ``apiVersion`` and ``kind`` properties are removed (also from
           ``required``);
        3. the ``status`` property is marked ``readOnly: true``.
        """
        names = set(node_type_names)
        result = copy.deepcopy(root_schema)
        definitions = result.get("definitions") or {}
        for name in names:
            schema = definitions.get(name)
            if isinstance(schema, dict):
                self._adjust(schema)
        return result, names

    @staticmethod
    def _adjust(schema: dict[str, Any]) -> None:
        gvk = schema.get(GVK)
        if isinstance(gvk, list) and len(gvk) == 1:
            schema[GVK] = gvk[0]

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for prop in _TYPE_DETERMINED:
                properties.pop(prop, None)
            status = properties.get("status")
            if isinstance(status, dict):
                status["readOnly"] = True
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [p for p in required if p not in _TYPE_DETERMINED]
            if not schema["required"]:
                del schema["required"]
