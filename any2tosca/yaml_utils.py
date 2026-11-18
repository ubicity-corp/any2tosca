"""
yaml_utils.py
-------------
YAML serialisation helpers for TOSCA output.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import re
from typing import Any

import yaml

__all__ = ["dump_tosca_yaml", "FoldedStr", "CommentedOut"]


class FoldedStr(str):
    """A str subclass that serialises with YAML folded block style (``>``)."""


class CommentedOut(str):
    """A string value that should be emitted as a commented-out YAML line.

    When a dict entry has a ``CommentedOut`` value the entire ``key: value``
    line is rendered as a YAML comment: ``# key: value``.  Used to annotate
    circular-import type references that cannot be resolved without creating
    a cycle (e.g. mutually-referencing Redfish schema families).
    """


# ── Custom representers ────────────────────────────────────────────────────────

def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _folded_str_representer(dumper: yaml.Dumper, data: FoldedStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


def _commented_out_representer(dumper: yaml.Dumper, data: CommentedOut) -> yaml.Node:
    """Emit with the local tag ``!co`` so post-processing can detect and comment out."""
    return dumper.represent_scalar("!co", str(data))


_dumper = yaml.Dumper
_dumper.add_representer(str, _str_representer)
_dumper.add_representer(FoldedStr, _folded_str_representer)
_dumper.add_representer(CommentedOut, _commented_out_representer)

# Regex that matches a YAML line whose value was tagged !co by the representer.
# Captures: (indent)(key)(value-possibly-quoted)
_CO_RE = re.compile(
    r"^(\s+)([^:\n]+):\s+!co\s+(.*?)\s*$",
    re.MULTILINE,
)


def _uncomment_line(m: re.Match[str]) -> str:
    """Replace ``indent key: !co value`` with ``indent # key: value``."""
    val = m.group(3)
    # Strip surrounding single or double quotes that PyYAML may have added
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return f"{m.group(1)}# {m.group(2)}: {val}"


def dump_tosca_yaml(data: dict[str, Any]) -> str:
    """Serialise *data* to a TOSCA-friendly YAML string."""
    raw = yaml.dump(
        data,
        Dumper=_dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return _CO_RE.sub(_uncomment_line, raw)
