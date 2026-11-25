"""
tests/test_redfish.py
---------------------
Tests for the RedfishNormalizer pre-processor.
"""

import pytest
from any2tosca.redfish import RedfishNormalizer
from any2tosca.converter import Conventions, Converter
from any2tosca.converter import convert_schema as _convert_schema

REDFISH = Conventions(redfish=True)


def _rf_convert(*args, **kwargs):
    """Convert with the Redfish conventions, as redfish2tosca does."""
    return _convert_schema(*args, conventions=REDFISH, **kwargs)

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


def test_anyof_null_flattened_inside_array_items():
    """anyOf/null flattening must also apply inside array items, not just top-level properties."""
    schema = {
        "$ref": "#/definitions/Container",
        "definitions": {
            "Container": {
                "type": "object",
                "properties": {
                    "MountPoints": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"$ref": "#/definitions/MountPoint"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "MountPoint": {"type": "object", "properties": {"Target": {"type": "string"}}},
        },
    }
    root, node_type_names = n.normalize(schema)
    items = root["definitions"]["Container"]["properties"]["MountPoints"]["items"]
    assert "anyOf" not in items
    assert items == {"$ref": "#/definitions/MountPoint"}

    # End-to-end: entry_schema must resolve to MountPoint, not string
    result = _rf_convert(root, "Container", node_type_names)
    mount_points = result.node_types["Container"].get("properties", {}).get("MountPoints") or \
                   result.data_types.get("Container", {}).get("properties", {}).get("MountPoints")
    assert mount_points is not None
    assert mount_points.get("entry_schema", {}).get("type") == "MountPoint"


# ── x- extension handling ─────────────────────────────────────────────────────

def test_x_extensions_kept():
    root, _ = n.normalize(REDFISH_DRIVE)
    id_prop = root["definitions"]["Drive"]["properties"]["Id"]
    assert id_prop["x-longDescription"] == "Unique identifier."


def test_extension_metadata_option_works_for_redfish(tmp_path):
    import json
    import yaml
    from any2tosca.cli import main_redfish
    schema = {
        "$ref": "#/definitions/Fan",
        "definitions": {"Fan": {"type": "object", "x-vendor": "acme",
                                "properties": {"Speed": {"type": "integer", "x-units": "RPM"}}}},
    }
    (tmp_path / "Fan.json").write_text(json.dumps(schema))
    out = tmp_path / "out"
    assert main_redfish([str(tmp_path / "Fan.json"), "--extension-metadata", "x-units",
                         "-o", str(out)]) == 0
    fan = yaml.safe_load((out / "Fan.yaml").read_text())["node_types"]["Fan"]
    assert fan["properties"]["Speed"]["metadata"] == {"x-units": "RPM"}
    assert "metadata" not in fan     # x-vendor not requested


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


# ── Flat schema (no definitions section) ─────────────────────────────────────

REDFISH_FLAT = {
    "$schema": "http://redfish.dmtf.org/schemas/v1/redfish-schema.v1_0_0.json",
    "$id": "http://example.com/redfish/v1/Resource.json",
    "title": "#Resource.Resource",
    "type": "object",
    "description": "Base Redfish resource",
    "properties": {
        "@odata.id":   {"type": "string"},
        "@odata.type": {"type": "string"},
        "Id":          {"type": "string"},
        "Name":        {"type": "string"},
    },
    "required": ["Id"],
}


def test_flat_schema_resource_name_from_title():
    _, node_type_names = n.normalize(REDFISH_FLAT)
    assert "Resource" in node_type_names


def test_flat_schema_odata_properties_removed():
    root, _ = n.normalize(REDFISH_FLAT)
    props = root["properties"]
    assert "@odata.id" not in props
    assert "@odata.type" not in props
    assert "Id" in props
    assert "Name" in props


def test_flat_schema_type_preserved():
    root, _ = n.normalize(REDFISH_FLAT)
    assert root.get("type") == "object"


def test_flat_schema_end_to_end():
    root, node_type_names = n.normalize(REDFISH_FLAT)
    result = _rf_convert(root, "Resource", node_type_names)
    assert "Resource" in result.node_types
    assert "derived_from" not in result.node_types["Resource"]
    props = result.node_types["Resource"]["properties"]
    assert "Id" in props
    assert "Name" in props


# ── End-to-end with Converter ─────────────────────────────────────────────────

def test_end_to_end():
    root, node_type_names = n.normalize(REDFISH_DRIVE)
    result = _rf_convert(root, "drive", node_type_names)

    assert "Drive" in result.node_types
    assert "derived_from" not in result.node_types["Drive"]
    assert "Links" in result.data_types

    drive_props = result.node_types["Drive"]["properties"]
    assert "Id" in drive_props
    assert "Name" in drive_props
    assert "@odata.id" not in drive_props
    assert "CapacityBytes" in drive_props
    assert drive_props["CapacityBytes"]["type"] == "integer"


# ── Redfish Links pattern expansion ───────────────────────────────────────────

# Schema with a node type that has a Links property referencing a Links def.
# The Links def contains a node-type reference (OtherResource) and a plain
# property (Oem).  Expected: Links expands into a DependsOn requirement;
# no Links property or Links type remains.
REDFISH_WITH_LINKS = {
    "$ref": "#/definitions/Widget",
    "definitions": {
        "Widget": {
            "type": "object",
            "description": "A widget resource.",
            "properties": {
                "@odata.id":   {"type": "string"},
                "Id":          {"type": "string"},
                "Name":        {"type": "string"},
                "Links": {"$ref": "#/definitions/Links"},
            },
        },
        "Links": {
            "type": "object",
            "description": "Related resources.",
            "properties": {
                "RelatedThing": {"$ref": "#/definitions/Thing"},
                "Oem":          {"type": "object"},
            },
        },
        "Thing": {
            "type": "object",
            "description": "A related resource.",
            "properties": {
                "@odata.id": {"type": "string"},
                "Id":        {"type": "string"},
            },
        },
    },
}


def test_links_property_expanded_to_requirements():
    """Links property is expanded into DependsOn requirements, not kept as property."""
    root, node_type_names = n.normalize(REDFISH_WITH_LINKS)
    # Manually add Thing as a node type (simulating Redfish resource detection)
    node_type_names = node_type_names | {"Thing", "Widget"}
    result = _rf_convert(root, "Widget", node_type_names)

    widget = result.node_types["Widget"]
    # Links property must not appear
    assert "Links" not in widget.get("properties", {})
    # RelatedThing must appear as a DependsOn requirement using the custom relationship type
    reqs = {k: v for req in widget.get("requirements", []) for k, v in req.items()}
    assert "RelatedThing" in reqs
    assert reqs["RelatedThing"]["node"] == "Thing"
    assert reqs["RelatedThing"]["relationship"] == "WidgetDependsOn"
    # Links type must not remain in output
    assert "Links" not in result.data_types
    assert "Links" not in result.node_types
    # All node types must expose a type-specific Feature capability
    assert widget.get("capabilities", {}).get("feature", {}).get("type") == "caps:WidgetFeature"
    assert result.node_types["Thing"].get("capabilities", {}).get("feature", {}).get("type") == "caps:ThingFeature"
    # Corresponding capability types must be in the result
    assert "WidgetFeature" in result.capability_types
    assert "ThingFeature" in result.capability_types
    # A WidgetDependsOn relationship type must be defined with the Oem property
    assert "WidgetDependsOn" in result.relationship_types
    wdo = result.relationship_types["WidgetDependsOn"]
    assert wdo["derived_from"] == "core:DependsOn"
    assert "Oem" in wdo["properties"]


# ── Redfish Actions pattern ────────────────────────────────────────────────────

# Schema with a node type that has an Actions property.
# The Actions def contains two action entries:
#   "#ComputerSystem.Reset" → Reset definition (with parameters and properties)
#   "Oem"                   → OemActions definition (plain object, no operations)
REDFISH_WITH_ACTIONS = {
    "$ref": "#/definitions/ComputerSystem",
    "definitions": {
        "ComputerSystem": {
            "type": "object",
            "description": "A computer system.",
            "properties": {
                "Id":      {"type": "string"},
                "Actions": {"$ref": "#/definitions/Actions"},
            },
        },
        "Actions": {
            "type": "object",
            "description": "The available actions for this resource.",
            "properties": {
                "#ComputerSystem.Reset": {"$ref": "#/definitions/Reset"},
                "Oem":                  {"$ref": "#/definitions/OemActions"},
            },
        },
        "Reset": {
            "type": "object",
            "description": "This action resets the system.",
            "parameters": {
                "ResetType": {
                    "type": "string",
                    "description": "The type of reset.",
                    "requiredParameter": True,
                },
            },
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Link to invoke action",
                },
                "title": {
                    "type": "string",
                    "description": "Friendly action name",
                },
            },
        },
        "OemActions": {
            "type": "object",
            "description": "OEM extensions.",
        },
    },
}


def test_actions_property_converted_to_interface_type():
    """Actions property is extracted into a TOSCA interface type, not kept as a property."""
    root, node_type_names = n.normalize(REDFISH_WITH_ACTIONS)
    result = _rf_convert(root, "ComputerSystem", node_type_names)

    assert "ComputerSystem" in result.node_types
    cs = result.node_types["ComputerSystem"]

    # Actions must not appear as a property on the node type
    assert "Actions" not in cs.get("properties", {})

    # An interface named "Actions" must appear on the node type, referencing the interface type
    interfaces = cs.get("interfaces", {})
    assert "Actions" in interfaces, f"Expected 'Actions' interface, got: {interfaces}"
    assert interfaces["Actions"]["type"] == "ComputerSystemActions"

    # The ComputerSystemActions interface type must be defined
    assert "ComputerSystemActions" in result.interface_types
    iface = result.interface_types["ComputerSystemActions"]

    # The Reset operation must be present
    ops = iface.get("operations", {})
    assert "Reset" in ops, f"Expected 'Reset' operation, got: {list(ops)}"
    reset_op = ops["Reset"]

    # description from action definition
    assert "resets the system" in reset_op.get("description", "")

    # parameters → inputs
    inputs = reset_op.get("inputs", {})
    assert "ResetType" in inputs
    rt = inputs["ResetType"]
    assert rt["type"] == "string"
    assert rt["required"] is True
    assert "reset" in rt.get("description", "").lower()


def test_actions_container_and_action_defs_removed_from_output():
    """Actions, OemActions, and action definition types must not appear in data/node types."""
    root, node_type_names = n.normalize(REDFISH_WITH_ACTIONS)
    result = _rf_convert(root, "ComputerSystem", node_type_names)

    # Actions container type removed
    assert "Actions" not in result.data_types
    assert "Actions" not in result.node_types
    # OemActions removed
    assert "OemActions" not in result.data_types
    assert "OemActions" not in result.node_types
    # Individual action definition type removed
    assert "Reset" not in result.data_types
    assert "Reset" not in result.node_types


def test_actions_with_external_param_ref(tmp_path):
    """Action parameters that $ref an external type produce a namespaced input type."""
    import json
    from pathlib import Path

    (tmp_path / "ResetType.json").write_text(json.dumps({
        "$id": "http://example.com/ResetType.json",
        "definitions": {
            "ResetType": {
                "type": "string",
                "enum": ["On", "ForceOff", "GracefulShutdown"],
            },
        },
    }))

    schema = {
        "$id": "http://example.com/System.json",
        "$ref": "#/definitions/System",
        "definitions": {
            "System": {
                "type": "object",
                "properties": {
                    "Actions": {"$ref": "#/definitions/Actions"},
                },
            },
            "Actions": {
                "type": "object",
                "properties": {
                    "#System.Reset": {"$ref": "#/definitions/Reset"},
                },
            },
            "Reset": {
                "type": "object",
                "description": "Reset the system.",
                "parameters": {
                    "ResetType": {
                        "$ref": "http://example.com/ResetType.json#/definitions/ResetType",
                        "description": "The reset type.",
                        "requiredParameter": True,
                    },
                },
            },
        },
    }
    root, node_type_names = n.normalize(schema)
    result = _rf_convert(root, "System", node_type_names,
                            base_path=tmp_path,
                            source_path=tmp_path / "System.json")

    assert "SystemActions" in result.interface_types
    reset_inputs = result.interface_types["SystemActions"]["operations"]["Reset"]["inputs"]
    assert "ResetType" in reset_inputs
    # Type must be namespaced (external ref was resolved and import recorded)
    assert ":" in reset_inputs["ResetType"]["type"], (
        f"Expected namespaced type, got: {reset_inputs['ResetType']['type']!r}"
    )
    # The external file must appear in imports
    assert any("ResetType" in v for v in result.imports.values()), (
        f"ResetType.yaml not in imports: {result.imports}"
    )
