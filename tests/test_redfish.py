"""
tests/test_redfish.py
---------------------
Tests for the RedfishNormalizer pre-processor.
"""

import pytest
from json2tosca.redfish import RedfishNormalizer
from json2tosca.converter import Converter

n = RedfishNormalizer()

# ── Minimal Redfish schema ────────────────────────────────────────────────────

REDFISH_DRIVE = {
    "$schema": "http://redfish.dmtf.org/schemas/v1/redfish-schema-v1.json",
    "$ref": "#/definitions/Drive",
    "definitions": {
        "Drive": {
            "type": "object",
            "description": "A storage drive.",
            "properties": {
                "@odata.id":   {"type": "string"},
                "@odata.type": {"type": "string"},
                "Id":          {"type": "string", "x-longDescription": "Unique identifier."},
                "Name":        {"type": "string"},
                "CapacityBytes": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}]
                },
            },
        },
        "Links": {
            "type": "object",
            "properties": {
                "Volumes": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


# ── Resource name detection ───────────────────────────────────────────────────

def test_resource_name_extracted():
    _, node_type_names = n.normalize(REDFISH_DRIVE)
    assert "Drive" in node_type_names


def test_non_resource_not_node_type():
    _, node_type_names = n.normalize(REDFISH_DRIVE)
    assert "Links" not in node_type_names


def test_no_top_ref_no_node_types():
    schema = {"definitions": {"Foo": {"type": "object"}}}
    _, node_type_names = n.normalize(schema)
    assert len(node_type_names) == 0


# ── OData property filtering ──────────────────────────────────────────────────

def test_odata_properties_removed():
    root, _ = n.normalize(REDFISH_DRIVE)
    props = root["definitions"]["Drive"]["properties"]
    assert "@odata.id" not in props
    assert "@odata.type" not in props


def test_data_properties_kept():
    root, _ = n.normalize(REDFISH_DRIVE)
    props = root["definitions"]["Drive"]["properties"]
    assert "Id" in props
    assert "Name" in props


# ── anyOf / null flattening ───────────────────────────────────────────────────

def test_anyof_null_flattened():
    root, _ = n.normalize(REDFISH_DRIVE)
    capacity = root["definitions"]["Drive"]["properties"]["CapacityBytes"]
    assert "anyOf" not in capacity
    assert capacity["type"] == "integer"


# ── x- extension handling ─────────────────────────────────────────────────────

def test_x_extensions_dropped():
    root, _ = n.normalize(REDFISH_DRIVE)
    id_prop = root["definitions"]["Drive"]["properties"]["Id"]
    assert "x-longDescription" not in id_prop


def test_x_longdescription_promoted_when_no_description():
    schema = {
        "$ref": "#/definitions/Foo",
        "definitions": {
            "Foo": {
                "type": "object",
                "x-longDescription": "A long description.",
                "properties": {"val": {"type": "string"}},
            }
        },
    }
    root, _ = n.normalize(schema)
    assert root["definitions"]["Foo"]["description"] == "A long description."


def test_x_longdescription_not_promoted_when_description_exists():
    schema = {
        "$ref": "#/definitions/Foo",
        "definitions": {
            "Foo": {
                "type": "object",
                "description": "Short.",
                "x-longDescription": "Long.",
                "properties": {},
            }
        },
    }
    root, _ = n.normalize(schema)
    assert root["definitions"]["Foo"]["description"] == "Short."


# ── Empty properties after filtering ─────────────────────────────────────────

def test_all_odata_properties_removed_drops_properties_key():
    schema = {
        "$ref": "#/definitions/ODataOnly",
        "definitions": {
            "ODataOnly": {
                "type": "object",
                "properties": {
                    "@odata.id": {"type": "string"},
                    "@odata.type": {"type": "string"},
                },
            }
        },
    }
    root, _ = n.normalize(schema)
    assert "properties" not in root["definitions"]["ODataOnly"]


# ── End-to-end with Converter ─────────────────────────────────────────────────

def test_end_to_end():
    root, node_type_names = n.normalize(REDFISH_DRIVE)
    result = Converter().convert_schema(root, "drive", node_type_names)

    assert "Drive" in result.node_types
    assert result.node_types["Drive"]["derived_from"] == "tosca.nodes.Root"
    assert "Links" in result.data_types

    drive_props = result.node_types["Drive"]["properties"]
    assert "Id" in drive_props
    assert "Name" in drive_props
    assert "@odata.id" not in drive_props
    assert "CapacityBytes" in drive_props
    assert drive_props["CapacityBytes"]["type"] == "integer"
