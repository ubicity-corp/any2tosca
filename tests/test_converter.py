"""
tests/test_converter.py
-----------------------
Unit tests for any2tosca.converter and any2tosca.cli.
"""

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from any2tosca.converter import Converter, ConversionResult, build_tosca_document, convert_schema
from any2tosca.cli import main_json

c = Converter()


# ── sanitize_name ─────────────────────────────────────────────────────────────

def test_sanitize_name_spaces():
    assert c.sanitize_name("my name") == "my_name"

def test_sanitize_name_dashes():
    assert c.sanitize_name("my-name") == "my_name"

def test_sanitize_name_dots():
    assert c.sanitize_name("my.name") == "my_name"

def test_sanitize_name_no_change():
    assert c.sanitize_name("myname") == "myname"


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
    assert c.resolve_ref("#/$defs/Foo", ROOT) == {"type": "string"}

def test_resolve_ref_definitions():
    assert c.resolve_ref("#/definitions/Bar", ROOT) == {"type": "integer"}

def test_resolve_ref_missing():
    assert c.resolve_ref("#/$defs/Missing", ROOT) == {}

def test_resolve_ref_external():
    assert c.resolve_ref("http://example.com/other.json", ROOT) == {}


# ── build_validation ──────────────────────────────────────────────────────────

def test_validation_numeric():
    v = c.build_validation({"minimum": 1, "maximum": 100})
    assert v == {"$and": [
        {"$greater_or_equal": ["$value", 1]},
        {"$less_or_equal": ["$value", 100]},
    ]}

def test_validation_exclusive():
    v = c.build_validation({"exclusiveMinimum": 0, "exclusiveMaximum": 10})
    assert v == {"$and": [
        {"$greater_than": ["$value", 0]},
        {"$less_than": ["$value", 10]},
    ]}

def test_validation_string():
    v = c.build_validation({"minLength": 2, "maxLength": 50, "pattern": "^[a-z]+$"})
    assert v == {"$and": [
        {"$greater_or_equal": [{"$length": ["$value"]}, 2]},
        {"$less_or_equal": [{"$length": ["$value"]}, 50]},
        {"$matches": ["$value", "^[a-z]+$"]},
    ]}

def test_validation_enum():
    v = c.build_validation({"enum": ["a", "b", "c"]})
    assert v == {"$valid_values": ["$value", ["a", "b", "c"]]}

def test_validation_single_clause():
    v = c.build_validation({"minimum": 0})
    assert v == {"$greater_or_equal": ["$value", 0]}

def test_validation_empty():
    assert c.build_validation({}) is None


# ── schema_to_property ────────────────────────────────────────────────────────

def test_property_required():
    p = c.schema_to_property("name", {"type": "string"}, ["name"], {}, {})
    assert p["required"] is True

def test_property_optional():
    p = c.schema_to_property("name", {"type": "string"}, [], {}, {})
    assert p["required"] is False

def test_property_default():
    p = c.schema_to_property("role", {"type": "string", "default": "viewer"}, [], {}, {})
    assert p["default"] == "viewer"

def test_property_list_entry_schema():
    p = c.schema_to_property("tags", {"type": "array", "items": {"type": "string"}}, [], {}, {})
    assert p["type"] == "list"
    assert p["entry_schema"]["type"] == "string"

def test_property_map_entry_schema():
    p = c.schema_to_property(
        "meta",
        {"type": "object", "additionalProperties": {"type": "string"}},
        [],
        {},
        {},
    )
    assert p["type"] == "map"
    assert p["entry_schema"]["type"] == "string"

def test_property_ref_uses_type_name():
    root = {"$defs": {"Addr": {"type": "object", "description": "An address"}}}
    p = c.schema_to_property("address", {"$ref": "#/$defs/Addr"}, [], root, {})
    assert p["type"] == "Addr"
    assert p["description"] == "An address"

def test_property_inline_object_creates_data_type():
    collected: dict = {}
    p = c.schema_to_property(
        "location",
        {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}},
        [],
        {},
        collected,
    )
    assert p["type"] == "location"
    assert "location" in collected

def test_property_list_ref_entry_schema():
    root = {"$defs": {"Tag": {"type": "string"}}}
    p = c.schema_to_property("tags", {"type": "array", "items": {"$ref": "#/$defs/Tag"}}, [], root, {})
    assert p["type"] == "list"
    assert p["entry_schema"]["type"] == "Tag"

def test_property_map_ref_entry_schema():
    root = {"$defs": {"Val": {"type": "string"}}}
    p = c.schema_to_property(
        "lookup",
        {"type": "object", "additionalProperties": {"$ref": "#/$defs/Val"}},
        [],
        root,
        {},
    )
    assert p["type"] == "map"
    assert p["entry_schema"]["type"] == "Val"


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

def test_convert_schema_returns_result():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    assert isinstance(result, ConversionResult)

def test_convert_schema_top_level():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    assert "Person" in result.data_types

def test_convert_schema_properties():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    props = result.data_types["Person"]["properties"]
    assert "name" in props
    assert "age" in props

def test_convert_schema_required_flag():
    result = convert_schema(SIMPLE_SCHEMA, "Person")
    assert result.data_types["Person"]["properties"]["name"]["required"] is True
    assert result.data_types["Person"]["properties"]["age"]["required"] is False

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
    assert "Address" in result.data_types

def test_convert_schema_scalar_def():
    schema = {
        "definitions": {
            "DiskType": {"type": "string", "enum": ["ssd", "hdd"]}
        }
    }
    result = convert_schema(schema, "Unused")
    dt = result.data_types["DiskType"]
    assert dt["derived_from"] == "string"
    assert dt["validation"] == {"$valid_values": ["$value", ["ssd", "hdd"]]}

def test_convert_schema_node_type():
    schema = {
        "definitions": {
            "Server": {"type": "object", "properties": {"cpu": {"type": "integer"}}},
            "OsConfig": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    }
    result = convert_schema(schema, "Unused", node_type_names={"Server"})
    assert "Server" in result.node_types
    assert "derived_from" not in result.node_types["Server"]
    assert "Server" not in result.data_types
    assert "OsConfig" in result.data_types

def test_convert_schema_node_type_has_properties():
    schema = {
        "type": "object",
        "properties": {"cores": {"type": "integer"}},
    }
    result = convert_schema(schema, "Compute", node_type_names={"Compute"})
    assert "cores" in result.node_types["Compute"]["properties"]


# ── build_tosca_document ──────────────────────────────────────────────────────

def test_build_tosca_document_data_types():
    doc = build_tosca_document({"Foo": {"derived_from": "tosca.datatypes.Root"}})
    assert doc["tosca_definitions_version"] == "tosca_2_0"
    assert "Foo" in doc["data_types"]
    assert "node_types" not in doc

def test_build_tosca_document_node_types():
    doc = build_tosca_document({}, {"Server": {"derived_from": "Root"}})
    assert "Server" in doc["node_types"]
    assert "data_types" not in doc

def test_build_tosca_document_both():
    doc = build_tosca_document(
        {"Cfg": {}},
        {"Server": {"derived_from": "Root"}},
    )
    assert "data_types" in doc
    assert "node_types" in doc


# ── allOf tests ───────────────────────────────────────────────────────────────

def test_allof_inheritance():
    """allOf with a pure $ref sub-schema maps to derived_from."""
    schema = {
        "definitions": {
            "Base": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Child": {
                "allOf": [
                    {"$ref": "#/definitions/Base"},
                    {"properties": {"name": {"type": "string"}}},
                ]
            },
        }
    }
    result = c.convert_schema(schema, "top")
    child = result.data_types["Child"]
    assert child["derived_from"] == "Base"
    assert "name" in child["properties"]


def test_allof_merges_properties():
    """allOf without a pure $ref merges all properties into one type."""
    schema = {
        "definitions": {
            "Combined": {
                "allOf": [
                    {"properties": {"a": {"type": "string"}}},
                    {"properties": {"b": {"type": "integer"}}},
                ]
            }
        }
    }
    result = c.convert_schema(schema, "top")
    props = result.data_types["Combined"]["properties"]
    assert "a" in props
    assert "b" in props


def test_allof_property():
    """A property schema using allOf with a pure $ref uses the ref type."""
    schema = {
        "definitions": {
            "Addr": {"type": "object", "properties": {"street": {"type": "string"}}},
            "Person": {
                "type": "object",
                "properties": {
                    "home": {"allOf": [{"$ref": "#/definitions/Addr"}, {"description": "Home address"}]},
                },
            },
        }
    }
    result = c.convert_schema(schema, "top")
    prop = result.data_types["Person"]["properties"]["home"]
    assert prop["type"] == "Addr"
    assert prop["description"] == "Home address"


def test_allof_required_merged():
    """required lists from allOf sub-schemas are merged and deduplicated."""
    schema = {
        "definitions": {
            "Doc": {
                "allOf": [
                    {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                    {"properties": {"b": {"type": "string"}}, "required": ["a", "b"]},
                ]
            }
        }
    }
    result = c.convert_schema(schema, "top")
    props = result.data_types["Doc"]["properties"]
    assert props["a"]["required"] is True
    assert props["b"]["required"] is True


# ── anyOf / oneOf tests ───────────────────────────────────────────────────────

def test_anyof_nullable_flattened():
    """anyOf: [X, {type: null}] is flattened to X."""
    schema = {
        "definitions": {
            "Thing": {
                "type": "object",
                "properties": {
                    "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            }
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["Thing"]["properties"]["name"]["type"] == "string"


def test_anyof_single_ref_becomes_type():
    """anyOf with one non-null $ref is treated like a bare $ref."""
    schema = {
        "definitions": {
            "Tag": {"type": "string"},
            "Thing": {
                "type": "object",
                "properties": {
                    "tag": {"anyOf": [{"$ref": "#/definitions/Tag"}, {"type": "null"}]},
                },
            },
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["Thing"]["properties"]["tag"]["type"] == "Tag"


def test_anyof_pure_ref_becomes_derived_from():
    """anyOf with a single pure $ref at the type level becomes derived_from."""
    schema = {
        "definitions": {
            "Base": {"type": "object", "properties": {"id": {"type": "string"}}},
            "Child": {"anyOf": [{"$ref": "#/definitions/Base"}]},
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["Child"]["derived_from"] == "Base"


def test_anyof_multiple_refs_merges_properties(capsys):
    """anyOf with multiple $refs: first becomes derived_from, rest are merged."""
    schema = {
        "definitions": {
            "A": {"type": "object", "properties": {"x": {"type": "string"}}},
            "B": {"type": "object", "properties": {"y": {"type": "integer"}}},
            "AB": {"anyOf": [{"$ref": "#/definitions/A"}, {"$ref": "#/definitions/B"}]},
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["AB"]["derived_from"] == "A"
    assert "y" in result.data_types["AB"]["properties"]
    captured = capsys.readouterr()
    assert "anyOf" in captured.err


def test_anyof_mixed_merges_properties():
    """anyOf with $ref + inline properties: ref → derived_from, props merged."""
    schema = {
        "definitions": {
            "Base": {"type": "object", "properties": {"id": {"type": "string"}}},
            "Extended": {
                "anyOf": [
                    {"$ref": "#/definitions/Base"},
                    {"type": "object", "properties": {"extra": {"type": "boolean"}}},
                ]
            },
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["Extended"]["derived_from"] == "Base"
    assert "extra" in result.data_types["Extended"]["properties"]


def test_anyof_versioned_refs_picks_latest(tmp_path: Path):
    """anyOf with versioned $refs to the same resource picks the most recent version."""
    for ver, props in [("v1_0_0", {"id": {"type": "string"}}),
                       ("v1_2_0", {"id": {"type": "string"}, "name": {"type": "string"}}),
                       ("v1_1_0", {"id": {"type": "string"}, "desc": {"type": "string"}})]:
        schema = {"type": "object", "properties": props}
        (tmp_path / f"Resource.{ver}.json").write_text(
            json.dumps({"definitions": {"Resource": schema}})
        )

    schema = {
        "definitions": {
            "Thing": {
                "anyOf": [
                    {"$ref": f"Resource.{v}.json#/definitions/Resource"}
                    for v in ("v1_0_0", "v1_2_0", "v1_1_0")
                ]
            }
        }
    }
    result = c.convert_schema(schema, "top", base_path=tmp_path)
    # Should pick v1_2_0 (highest version) — it has both id and name
    assert result.data_types["Thing"]["derived_from"] == "resource:Resource"
    assert "resource" in result.imports
    assert "v1_2_0" in result.imports["resource"]


def test_oneof_nullable_flattened():
    """oneOf: [X, {type: null}] is flattened to X (same as anyOf)."""
    schema = {
        "definitions": {
            "Thing": {
                "type": "object",
                "properties": {
                    "count": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
                },
            }
        }
    }
    result = convert_schema(schema, "top")
    assert result.data_types["Thing"]["properties"]["count"]["type"] == "integer"


# ── External $ref tests ───────────────────────────────────────────────────────

def test_external_ref_property_type(tmp_path: Path):
    """A $ref to another file produces a namespaced TOSCA type and records an import."""
    addr_schema = {"type": "object", "properties": {"street": {"type": "string"}}}
    (tmp_path / "Address.json").write_text(json.dumps({"definitions": {"Address": addr_schema}}))

    schema = {
        "definitions": {
            "Person": {
                "type": "object",
                "properties": {
                    "home": {"$ref": "Address.json#/definitions/Address"},
                },
            }
        }
    }
    result = c.convert_schema(schema, "top", base_path=tmp_path)
    prop = result.data_types["Person"]["properties"]["home"]
    assert prop["type"] == "address:Address"
    assert "address" in result.imports
    assert result.imports["address"] == "Address.yaml"


def test_external_ref_without_base_path():
    """Without base_path, external $refs still produce a namespaced type and record an import."""
    schema = {
        "definitions": {
            "Person": {
                "type": "object",
                "properties": {
                    "home": {"$ref": "Address.json#/definitions/Address"},
                },
            }
        }
    }
    result = c.convert_schema(schema, "top")
    prop = result.data_types["Person"]["properties"]["home"]
    assert prop["type"] == "address:Address"
    assert result.imports == {"address": "Address.yaml"}


def test_external_ref_http_uri_unmatched():
    """HTTP URI $refs with no matching $id in the registry fall back to 'string'."""
    schema = {
        "definitions": {
            "Thing": {
                "type": "object",
                "properties": {
                    "id": {"$ref": "http://example.com/schemas/Id.json#/definitions/Id"},
                },
            }
        }
    }
    result = c.convert_schema(schema, "top")
    prop = result.data_types["Thing"]["properties"]["id"]
    assert prop["type"] == "string"
    assert result.imports == {}


def test_external_ref_http_uri_filename_fallback(tmp_path: Path):
    """HTTP URI $refs fall back to filename match when $id doesn't match the URI."""
    # File exists but its $id differs from the URI used in the $ref
    id_schema = {"$id": "urn:example:different-id", "definitions": {"IdRef": {"type": "string"}}}
    (tmp_path / "odata-v4.json").write_text(json.dumps(id_schema))

    schema = {
        "definitions": {
            "Thing": {
                "type": "object",
                "properties": {
                    "id": {"$ref": "http://redfish.dmtf.org/schemas/v1/odata-v4.json#/definitions/IdRef"},
                },
            }
        }
    }
    result = c.convert_schema(schema, "top", base_path=tmp_path)
    prop = result.data_types["Thing"]["properties"]["id"]
    assert prop["type"] == "odatav4:IdRef"
    assert "odatav4" in result.imports


def test_build_tosca_document_with_imports():
    """imports dict is rendered as a TOSCA imports list."""
    doc = Converter().build_tosca_document(
        {"Foo": {}},
        imports={"bar": "bar.yaml"},
    )
    assert doc["imports"] == [{"url": "bar.yaml", "namespace": "bar"}]


def test_anyof_versioned_plus_unversioned(tmp_path: Path):
    """anyOf with versioned + unversioned refs: versioned becomes derived_from.

    Mimics the Redfish anyOf pattern where idRef is the unversioned fallback
    and Container.v1_0_0.json is the actual resource type.  The versioned ref
    should become derived_from; the unversioned one is merged (typically has
    no extra properties, but its import is still recorded).
    """
    id_ref = tmp_path / "idRef.json"
    id_ref.write_text(json.dumps({"$id": "idRef.json", "type": "object",
                                   "properties": {"@odata.id": {"type": "string"}}}))

    ext = tmp_path / "Container.v1_0_0.json"
    ext.write_text(json.dumps({
        "$id": "Container.v1_0_0.json",
        "type": "object",
        "properties": {"Name": {"type": "string"}},
    }))
    schema = {
        "title": "Container",
        "type": "object",
        "anyOf": [
            {"$ref": "idRef.json"},
            {"$ref": "Container.v1_0_0.json"},
        ],
    }
    result = convert_schema(schema, "Container", node_type_names={"Container"},
                            base_path=tmp_path)
    node = result.node_types.get("Container", {})
    # Versioned ref → derived_from
    assert "containerv100" in node.get("derived_from", "").lower() or \
           "Container" in node.get("derived_from", "")
    # Unversioned ref properties (odata.id) are merged in
    assert any("odata" in k.lower() or "id" in k.lower()
               for k in node.get("properties", {}))
    # Versioned file is recorded as an import (derived_from references it)
    assert any("Container.v1_0_0" in v or "containerv100" in v.lower()
               for v in result.imports.values())


def test_anyof_versioned_ref_local_refs_rewritten(tmp_path: Path):
    """Local #/... refs in unversioned-ref properties are rewritten.

    When the unversioned ref's properties contain #/definitions/... sub-refs,
    they must be rewritten to ExternalFile.json#/definitions/... so the TOSCA
    output uses namespaced types rather than unresolvable local names.
    """
    id_ref = tmp_path / "idRef.json"
    id_ref.write_text(json.dumps({
        "$id": "idRef.json",
        "type": "object",
        "properties": {
            "context": {"$ref": "#/definitions/Context"},
        },
        "definitions": {
            "Context": {"type": "object", "properties": {"uri": {"type": "string"}}},
        },
    }))
    ext = tmp_path / "Resource.v1_0_0.json"
    ext.write_text(json.dumps({
        "$id": "Resource.v1_0_0.json",
        "type": "object",
        "properties": {"Name": {"type": "string"}},
    }))
    schema = {
        "title": "Resource",
        "type": "object",
        "anyOf": [
            {"$ref": "idRef.json"},
            {"$ref": "Resource.v1_0_0.json"},
        ],
    }
    result = convert_schema(schema, "Resource", node_type_names={"Resource"},
                            base_path=tmp_path)
    node = result.node_types.get("Resource", {})
    context_type = node.get("properties", {}).get("context", {}).get("type", "")
    assert ":" in context_type, \
        f"Expected namespaced type for 'context', got: {context_type!r}"


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
    out_dir = tmp_path / "out"
    rc = main_json([str(tmp_schema), "-o", str(out_dir)])
    assert rc == 0
    out_file = out_dir / "widget.yaml"
    assert out_file.exists()
    doc = yaml.safe_load(out_file.read_text())
    assert "Widget" in doc["data_types"]


def test_cli_directory(tmp_path: Path):
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    for name in ("alpha", "beta"):
        s = {"title": name.capitalize(), "type": "object",
             "properties": {"x": {"type": "string"}}}
        (schema_dir / f"{name}.json").write_text(json.dumps(s))

    out_dir = tmp_path / "out"
    rc = main_json([str(schema_dir), "-o", str(out_dir)])
    assert rc == 0
    # Each input file produces its own TOSCA file
    alpha_doc = yaml.safe_load((out_dir / "alpha.yaml").read_text())
    beta_doc  = yaml.safe_load((out_dir / "beta.yaml").read_text())
    assert "Alpha" in alpha_doc["data_types"]
    assert "Beta"  in beta_doc["data_types"]


def test_cli_default_output_dir(tmp_schema: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main_json([str(tmp_schema)])
    assert rc == 0
    assert (tmp_path / "tosca" / "widget.yaml").exists()


def test_cli_no_files(tmp_path: Path):
    rc = main_json([str(tmp_path / "nonexistent.json")])
    assert rc != 0


def test_cli_bad_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json }")
    out_dir = tmp_path / "out"
    rc = main_json([str(bad), "-o", str(out_dir)])
    assert rc != 0
