"""
tests/test_companions.py
------------------------
Unit tests for the by-value pre-pass (any2tosca.companions), the ``value``
relationship model in the converter, and the ``--relationship-model`` option.
"""

import json

import pytest
import yaml

from any2tosca.cli import main_oas, main_redfish
from any2tosca.companions import CompanionError, add_value_companions
from any2tosca.converter import Converter


def _ref(name, container="definitions"):
    return {"$ref": f"#/{container}/{name}"}


def _obj(**props):
    return {"type": "object", "properties": props}


ROOT = {"definitions": {
    "Claim": _obj(size={"type": "string"}),
    "Set": _obj(spec=_ref("SetSpec")),
    "SetSpec": _obj(templates={"type": "array", "items": _ref("Claim")}),
}}
NODES = {"Claim", "Set"}


# ── Pre-pass ──────────────────────────────────────────────────────────────────

def test_by_value_ref_points_at_companion():
    out = add_value_companions(ROOT, NODES)
    defs = out["definitions"]
    assert defs["SetSpec"]["properties"]["templates"]["items"] == _ref("ClaimValue")
    assert defs["ClaimValue"] == ROOT["definitions"]["Claim"]


def test_companion_only_for_referenced_node_types():
    out = add_value_companions(ROOT, NODES)
    assert "SetValue" not in out["definitions"]


def test_ref_to_data_type_is_unchanged():
    out = add_value_companions(ROOT, NODES)
    assert out["definitions"]["Set"]["properties"]["spec"] == _ref("SetSpec")


def test_node_type_property_ref_to_node_type_points_at_companion():
    root = {"definitions": {"A": _obj(b=_ref("B")), "B": _obj()}}
    out = add_value_companions(root, {"A", "B"})
    assert out["definitions"]["A"]["properties"]["b"] == _ref("BValue")


def test_node_type_inheritance_is_kept():
    root = {"definitions": {"Base": _obj(), "Derived": {"allOf": [_ref("Base")], **_obj()}}}
    out = add_value_companions(root, {"Base", "Derived"})
    assert out["definitions"]["Derived"]["allOf"] == [_ref("Base")]
    assert "BaseValue" not in out["definitions"]


def test_data_type_deriving_from_node_type_derives_from_companion():
    root = {"definitions": {"Base": _obj(), "Data": {"allOf": [_ref("Base")]}}}
    out = add_value_companions(root, {"Base"})
    assert out["definitions"]["Data"]["allOf"] == [_ref("BaseValue")]


def test_refs_inside_a_companion_are_processed():
    # Holder → A (by value) → companion AValue, whose ref to B is also by value.
    root = {"definitions": {
        "Holder": _obj(a=_ref("A")),
        "A": {"allOf": [_ref("B")], **_obj(b=_ref("B"))},
        "B": _obj(),
    }}
    out = add_value_companions(root, {"A", "B"})
    defs = out["definitions"]
    assert defs["Holder"]["properties"]["a"] == _ref("AValue")
    assert defs["A"]["allOf"] == [_ref("B")]                 # node type: inheritance
    assert defs["A"]["properties"]["b"] == _ref("BValue")    # node type: by value
    assert defs["AValue"]["allOf"] == [_ref("BValue")]       # companion: base is a companion
    assert defs["AValue"]["properties"]["b"] == _ref("BValue")


def test_deeper_fragment_is_kept():
    root = {"definitions": {"A": _obj(x={"type": "string"}),
                            "D": _obj(y={"$ref": "#/definitions/A/properties/x"})}}
    out = add_value_companions(root, {"A"})
    assert out["definitions"]["D"]["properties"]["y"] == {"$ref": "#/definitions/AValue/properties/x"}


def test_ignored_definitions_create_no_companions():
    root = {"definitions": {**ROOT["definitions"],
                            "ClaimList": _obj(items={"type": "array", "items": _ref("Set")})}}
    out = add_value_companions(root, NODES, ignore={"ClaimList"})
    assert "SetValue" not in out["definitions"]
    assert out["definitions"]["ClaimList"]["properties"]["items"]["items"] == _ref("Set")


def test_dollar_defs_container():
    root = {"$defs": {"N": _obj(), "D": _obj(n=_ref("N", "$defs"))}}
    out = add_value_companions(root, {"N"})
    assert out["$defs"]["D"]["properties"]["n"] == _ref("NValue", "$defs")
    assert "NValue" in out["$defs"]


def test_companion_name_collision_is_an_error():
    root = {"definitions": {**ROOT["definitions"], "ClaimValue": _obj()}}
    with pytest.raises(CompanionError, match="ClaimValue"):
        add_value_companions(root, NODES)


def test_source_schema_is_not_modified():
    before = json.dumps(ROOT, sort_keys=True)
    add_value_companions(ROOT, NODES)
    assert json.dumps(ROOT, sort_keys=True) == before


# ── Converter: value model ────────────────────────────────────────────────────

def _convert(model, root=None, nodes=NODES):
    root = add_value_companions(ROOT, nodes) if root is None else root
    return Converter().convert_schema(root, "x", nodes, relationship_model=model)


def test_value_model_adds_no_capabilities_or_core_import():
    result = _convert("value")
    assert "core" not in result.imports
    assert not result.capability_types
    assert all("capabilities" not in nt for nt in result.node_types.values())


def test_value_model_companion_is_a_data_type():
    result = _convert("value")
    assert "ClaimValue" in result.data_types
    templates = result.data_types["SetSpec"]["properties"]["templates"]
    assert templates["entry_schema"]["type"] == "ClaimValue"
    assert set(result.node_types) == {"Claim", "Set"}


def test_value_model_keeps_node_type_refs_as_properties():
    root = {"definitions": {"A": _obj(b=_ref("B")), "B": _obj()}}
    result = _convert("value", root, {"A", "B"})
    assert "requirements" not in result.node_types["A"]
    assert result.node_types["A"]["properties"]["b"]["type"] == "B"


def test_value_model_treats_links_as_an_ordinary_property():
    root = {"definitions": {"R": _obj(Links=_ref("Links")), "Links": _obj(x={"type": "string"})}}
    result = _convert("value", root, {"R"})
    assert result.node_types["R"]["properties"]["Links"]["type"] == "Links"
    assert "requirements" not in result.node_types["R"]


def test_links_model_is_unchanged():
    root = {"definitions": {"A": _obj(b=_ref("B")), "B": _obj()}}
    result = _convert("links", root, {"A", "B"})
    assert result.node_types["A"]["requirements"][0]["b"]["node"] == "B"
    assert result.imports["core"] == "Core.yaml"
    assert "AContainable" in result.capability_types


def test_unknown_relationship_model_is_rejected():
    with pytest.raises(ValueError, match="relationship model"):
        _convert("graph")


def test_dotted_node_type_names_match_sanitized_definitions():
    root = {"definitions": {"io.x.v1.Pod": _obj(name={"type": "string"})}}
    result = Converter().convert_schema(root, "x", {"io.x.v1.Pod"}, relationship_model="value")
    assert set(result.node_types) == {"io_x_v1_Pod"}


# ── CLI ───────────────────────────────────────────────────────────────────────

_OAS = {
    "swagger": "2.0",
    "info": {"title": "x", "version": "1"},
    "paths": {
        "/claims": {"get": {}, "post": {"parameters": [{"in": "body", "name": "b", "schema": _ref("Claim")}]}},
        "/sets": {"get": {}, "post": {"parameters": [{"in": "body", "name": "b", "schema": _ref("Set")}]}},
    },
    "definitions": ROOT["definitions"],
}


def test_oas2tosca_defaults_to_value_model(tmp_path):
    api = tmp_path / "api.json"
    api.write_text(json.dumps(_OAS))
    assert main_oas([str(api), "-o", str(tmp_path / "out")]) == 0
    doc = yaml.safe_load((tmp_path / "out" / "api.yaml").read_text())
    assert "imports" not in doc
    assert "ClaimValue" in doc["data_types"]
    assert not list((tmp_path / "out").glob("*_caps.yaml"))


def test_oas2tosca_links_model_on_request(tmp_path):
    api = tmp_path / "api.json"
    api.write_text(json.dumps(_OAS))
    assert main_oas([str(api), "--relationship-model", "links", "-o", str(tmp_path / "out")]) == 0
    doc = yaml.safe_load((tmp_path / "out" / "api.yaml").read_text())
    assert {"url": "Core.yaml", "namespace": "core"} in doc["imports"]
    assert "ClaimValue" not in doc.get("data_types", {})
    # The Redfish conventions promote the data type holding a node type.
    assert "requirements" in doc["node_types"]["SetSpec"]


def test_split_value_model_allows_core_namespace(tmp_path):
    api = tmp_path / "api.json"
    api.write_text(json.dumps(_OAS))
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"rules": [
        {"match": "Claim.*", "unit": "core"},
        {"match": "Set.*", "unit": "apps"},
    ]}))
    out = tmp_path / "out"
    assert main_oas([str(api), "--split", str(rules), "-o", str(out)]) == 0
    apps = yaml.safe_load((out / "apps.yaml").read_text())
    assert apps["imports"] == [{"url": "core.yaml", "namespace": "core"}]
    templates = apps["data_types"]["SetSpec"]["properties"]["templates"]
    assert templates["entry_schema"]["type"] == "core:ClaimValue"
    core = yaml.safe_load((out / "core.yaml").read_text())
    assert "ClaimValue" in core["data_types"] and "Claim" in core["node_types"]


def test_split_links_model_reserves_core_namespace(tmp_path, capsys):
    schema = tmp_path / "s.json"
    schema.write_text(json.dumps({"definitions": {"A": _obj()}}))
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"rules": [{"match": ".*", "unit": "core"}]}))
    assert main_redfish([str(schema), "--split", str(rules), "-o", str(tmp_path / "o")]) == 1
    assert "reserved" in capsys.readouterr().err
