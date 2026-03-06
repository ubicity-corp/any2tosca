"""
yaml_utils.py
-------------
YAML serialisation helpers for TOSCA output.
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["dump_tosca_yaml", "FoldedStr"]


class FoldedStr(str):
    """A str subclass that serialises with YAML folded block style (``>``)."""


# ── Custom representers ────────────────────────────────────────────────────────

def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _folded_str_representer(dumper: yaml.Dumper, data: FoldedStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


_dumper = yaml.Dumper
_dumper.add_representer(str, _str_representer)
_dumper.add_representer(FoldedStr, _folded_str_representer)


def dump_tosca_yaml(data: dict[str, Any]) -> str:
    """Serialise *data* to a TOSCA-friendly YAML string."""
    return yaml.dump(
        data,
        Dumper=_dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
