"""
tests/test_converter.py
-----------------------
Unit tests for json2tosca.converter and json2tosca.cli.
"""

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from json2tosca.converter import (
    build_tosca_document,
    convert_schema,
    resolve_ref,
    sanitize_name,
    build_constraints,
    schema_to_property,
)
from json2tosca.cli import main


# ── sanitize_name ─────────────────────────────────────────────────────────────

def test_sanitize_name_spaces():
    assert sanitize_name("my name") == "my_name"

def test_sanitize_name_dashes():
    assert sanitize_name("my-name") == "my_name"

def test_sanitize_name_dots():
    assert sanitize_name("my.name") == "my_name"

def test_sanitize_name_no_change():
    assert sanitize_name("myname") == "myname"


# ── resolve_ref ───────────────────────────────────────────────────────────────

ROOT = {
    "$defs": {
        "Foo": {"type": "string"},
    },
    "definitions": {
        "Bar": {"type": "integer"},
    },
}

def test_resolve_ref_defs():
    assert resolve_ref("#/$defs/Foo", ROOT) == {"type": "string"}

def test_resolve_ref_definitions():
    assert resolve_ref("#/definitions/Bar", ROOT) == {"type": "integer"}

def test_resolve_ref_missing():
    assert resolve_ref("#/$defs/Missing", ROOT) == {}

def test_resolve_ref_external():
    assert resolve_ref("http://example.com/other.json", ROOT) == {}


# ── build_constraints ─────────────────────────────────────────────────────────

def test_constraints_numeric():
    c = build_constraints({"minimum": 1, "maximum": 100})
    assert {"greater_or_equal": 1} in c
    assert {"less_or_equal": 100} in c

def test_constraints_exclusive():
    c = build_constraints({"exclusiveMinimum": 0, "exclusiveMaximum": 10})
    assert {"greater_than": 0} in c
    assert {"less_than": 10} in c

def test_constraints_string():
    c = build_constraints({"minLength": 2, "maxLength": 50, "pattern": "^[a-z]+$"})
    assert {"min_length": 2} in c
    assert {"max_length": 50} in c
    assert {"pattern": "^[a-z]+$"} in c

def test_constraints_enum():
    c = build_constraints({"enum": ["a", "b", "c"]})
    assert {"valid_values": ["a", "b", "c"]} in c

def test_constraints_empty():
    assert build_constraints({}) == []


# ── schema_to_property ────────────────────────────────────────────────────────

def test_property_required():
    p = schema_to_property("name", {"type": "string"}, ["name"], {})
    assert p["required"] is True

def test_property_optional():
    p = schema_to_property("name", {"type": "string"}, [], {})
    assert p["required"] is False

def test_property_default():
    p = schema_to_property("role", {"type": "string", "default": "viewer"}, [], {})
    assert p["default"] == "viewer"

def test_property_list_entry_schema():
    p = schema_to_property("tags", {"type": "array", "items": {"type": "string"}}, [], {})
    assert p["type"] == "list"
    assert p["entry_schema"]["type"] == "string"

def test_property_map_entry_schema():
    p = schema_to_property(
        "meta",
        {"type": "object", "additionalProperties": {"type": "string"}},
        [],
        {},
    )
    assert p["type"] == "map"
    assert p["entry_schema"]["type"] == "string"

def test_property_ref_resolved():
    root = {"$defs": {"Addr": {"type": "string", "description": "An address"}}}
    p = schema_to_property("address", {"$ref": "#/$defs/Addr"}, [], root)
    assert p["type"] == "string"
    assert p["description"] == "An address"


# ── convert_schema ────────────────────────────────────────────────────────────

SIMPLE_SCHEMA = {
    "title": "Person",
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "Full name"},
        "age":  {"type": "integer", "minimum": 0},
    },
}

def test_convert_schema_top_level():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    assert "Person" in result

def test_convert_schema_properties():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    props = result["Person"]["properties"]
    assert "name" in props
    assert "age" in props

def test_convert_schema_required_flag():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    assert result["Person"]["properties"]["name"]["required"] is True
    assert result["Person"]["properties"]["age"]["required"] is False

def test_convert_schema_definitions():
    schema = {
        "type": "object",
        "properties": {"addr": {"$ref": "#/definitions/Address"}},
        "definitions": {
            "Address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            }
        },
    }
    result = convert_schema(schema, "Root")
    assert "Address" in result

def test_convert_schema_scalar_def():
    schema = {
        "definitions": {
            "DiskType": {"type": "string", "enum": ["ssd", "hdd"]}
        }
    }
    result = convert_schema(schema, "Unused")
    dt = result["DiskType"]
    assert dt["derived_from"] == "string"
    assert {"valid_values": ["ssd", "hdd"]} in dt["constraints"]


# ── build_tosca_document ──────────────────────────────────────────────────────

def test_build_tosca_document_structure():
    doc = build_tosca_document({"Foo": {"derived_from": "tosca.datatypes.Root"}})
    assert doc["tosca_definitions_version"] == "tosca_simple_yaml_1_3"
    assert "Foo" in doc["data_types"]


# ── CLI integration tests ─────────────────────────────────────────────────────

@pytest.fixture()
def tmp_schema(tmp_path: Path) -> Path:
    schema = {
        "title": "Widget",
        "type": "object",
        "required": ["id"],
        "properties": {
            "id":    {"type": "integer"},
            "label": {"type": "string", "maxLength": 80},
        },
    }
    p = tmp_path / "widget.json"
    p.write_text(json.dumps(schema))
    return p


def test_cli_single_file(tmp_schema: Path, tmp_path: Path):
    out = tmp_path / "out.yaml"
    rc = main([str(tmp_schema), "-o", str(out)])
    assert rc == 0
    assert out.exists()
    doc = yaml.safe_load(out.read_text())
    assert "Widget" in doc["data_types"]


def test_cli_directory(tmp_path: Path):
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    for name in ("alpha", "beta"):
        s = {"title": name.capitalize(), "type": "object",
             "properties": {"x": {"type": "string"}}}
        (schema_dir / f"{name}.json").write_text(json.dumps(s))

    out = tmp_path / "combined.yaml"
    rc = main([str(schema_dir), "-o", str(out)])
    assert rc == 0
    doc = yaml.safe_load(out.read_text())
    assert "Alpha" in doc["data_types"]
    assert "Beta" in doc["data_types"]


def test_cli_no_files(tmp_path: Path):
    rc = main([str(tmp_path / "nonexistent.json")])
    assert rc != 0


def test_cli_bad_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json }")
    out = tmp_path / "out.yaml"
    rc = main([str(bad), "-o", str(out)])
    assert rc != 0
