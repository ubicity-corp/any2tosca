"""
tests/test_split.py
-------------------
Unit tests for any2tosca.split and the multi-output CLI options.
"""

import json
from pathlib import Path

import pytest
import yaml

from any2tosca.cli import main_json, main_oas
from any2tosca.split import SplitError, load_rules, split_schema


def _rules(*rules, default_unit=None):
    data = {"rules": list(rules)}
    if default_unit is not None:
        data["default_unit"] = default_unit
    return load_rules(data)


def _obj(**props):
    return {"type": "object", "properties": props}


def _ref(name):
    return {"$ref": f"#/definitions/{name}"}


ROOT = {
    "definitions": {
        "io.x.meta.v1.ObjectMeta": _obj(name={"type": "string"}),
        "io.x.apps.v1.Deployment": _obj(
            metadata=_ref("io.x.meta.v1.ObjectMeta"),
            spec=_ref("io.x.apps.v1.DeploymentSpec"),
        ),
        "io.x.apps.v1.DeploymentSpec": _obj(replicas={"type": "integer"}),
        "io.x.apps.v1.DeploymentList": _obj(items={"type": "array", "items": _ref("io.x.apps.v1.Deployment")}),
        "io.x.apps.v1beta1.Deployment": _obj(),
    }
}

GROUP_RULES = [
    {"match": r".*List", "skip": True},
    {"match": r"io\.x\.meta\.v1\.(?P<name>[^.]+)", "unit": "meta"},
    {"match": r"io\.x\.(?P<unit>[a-z]+)\.v1\.(?P<name>[^.]+)"},
    {"match": r".*", "skip": True},
]


def _units(result):
    return {u.name: u for u in result.units}


# ── Rules ─────────────────────────────────────────────────────────────────────

def test_named_groups_give_unit_and_type_name():
    result = split_schema(ROOT, _rules(*GROUP_RULES))
    units = _units(result)
    assert set(units) == {"meta", "apps"}
    assert set(units["apps"].document["definitions"]) == {"Deployment", "DeploymentSpec"}
    assert set(units["meta"].document["definitions"]) == {"ObjectMeta"}


def test_first_matching_rule_wins():
    rules = _rules({"match": r".*Spec", "unit": "specs"}, *GROUP_RULES)
    units = _units(split_schema(ROOT, rules))
    # Without a name group the type name is the sanitized definition name.
    assert set(units["specs"].document["definitions"]) == {"io_x_apps_v1_DeploymentSpec"}
    assert "DeploymentSpec" not in units["apps"].document["definitions"]


def test_skip_drops_definitions():
    units = _units(split_schema(ROOT, _rules(*GROUP_RULES)))
    all_defs = {n for u in units.values() for n in u.document["definitions"]}
    assert "DeploymentList" not in all_defs


def test_default_unit_takes_unmatched_definitions():
    rules = _rules(*GROUP_RULES[:3], default_unit="other")
    units = _units(split_schema(ROOT, rules))
    assert set(units["other"].document["definitions"]) == {"io_x_apps_v1beta1_Deployment"}


def test_unmatched_without_default_unit_is_an_error():
    with pytest.raises(SplitError, match="match no rule"):
        split_schema(ROOT, _rules(*GROUP_RULES[:3]))


def test_rule_without_unit_is_rejected():
    with pytest.raises(SplitError, match="give a 'unit'"):
        _rules({"match": r"(?P<name>.*)"})


def test_unknown_rule_key_is_rejected():
    with pytest.raises(SplitError, match="unknown keys"):
        _rules({"match": ".*", "unit": "u", "colour": "red"})


def test_load_rules_from_file(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"rules": GROUP_RULES}))
    assert len(load_rules(path).rules) == 4


# ── Checks ────────────────────────────────────────────────────────────────────

def test_type_name_collision_is_an_error():
    rules = _rules(
        {"match": r".*List", "skip": True},
        {"match": r"io\.x\.meta\.v1\.(?P<name>[^.]+)", "unit": "meta"},
        {"match": r"io\.x\.(?P<unit>[a-z]+)\.v1(beta1)?\.(?P<name>[^.]+)"},
    )
    with pytest.raises(SplitError, match="io.x.apps.v1.Deployment, io.x.apps.v1beta1.Deployment"):
        split_schema(ROOT, rules)


def test_invalid_unit_name_is_an_error():
    rules = _rules({"match": r"io\.x\.meta.*", "unit": "my-meta"}, {"match": ".*", "skip": True})
    with pytest.raises(SplitError, match="not a valid TOSCA name"):
        split_schema(ROOT, rules)


def test_versioned_looking_unit_name_is_an_error():
    root = {"definitions": {"A": _obj()}}
    with pytest.raises(SplitError):
        split_schema(root, _rules({"match": ".*", "unit": "Drive.v1_2_3"}))


def test_reserved_namespace_is_an_error():
    rules = _rules({"match": r"io\.x\.meta.*", "unit": "core"}, {"match": ".*", "skip": True})
    with pytest.raises(SplitError, match="reserved"):
        split_schema(ROOT, rules, reserved_namespaces={"core"})


def test_namespace_override_avoids_reserved_name():
    rules = _rules({"match": r"io\.x\.meta.*", "unit": "core", "namespace": "kcore"},
                   {"match": ".*", "skip": True})
    result = split_schema(ROOT, rules, reserved_namespaces={"core"})
    assert result.namespaces == {"core.json": "kcore"}


def test_conflicting_namespaces_for_one_unit_are_an_error():
    rules = _rules(
        {"match": r".*Meta", "unit": "u", "namespace": "a"},
        {"match": r".*Spec", "unit": "u", "namespace": "b"},
        {"match": ".*", "skip": True},
    )
    with pytest.raises(SplitError, match="two namespaces"):
        split_schema(ROOT, rules)


# ── Reference rewriting ───────────────────────────────────────────────────────

def test_refs_within_a_unit_stay_local():
    units = _units(split_schema(ROOT, _rules(*GROUP_RULES)))
    spec = units["apps"].document["definitions"]["Deployment"]["properties"]["spec"]
    assert spec == {"$ref": "#/definitions/DeploymentSpec"}


def test_refs_across_units_name_the_unit_file():
    units = _units(split_schema(ROOT, _rules(*GROUP_RULES)))
    meta = units["apps"].document["definitions"]["Deployment"]["properties"]["metadata"]
    assert meta == {"$ref": "meta.json#/definitions/ObjectMeta"}


def test_deeper_fragment_is_kept():
    root = {"definitions": {
        "io.x.meta.v1.A": _obj(b={"type": "string"}),
        "io.x.apps.v1.C": _obj(d={"$ref": "#/definitions/io.x.meta.v1.A/properties/b"}),
    }}
    units = _units(split_schema(root, _rules(*GROUP_RULES)))
    d = units["apps"].document["definitions"]["C"]["properties"]["d"]
    assert d == {"$ref": "meta.json#/definitions/A/properties/b"}


def test_source_schema_is_not_modified():
    before = json.dumps(ROOT, sort_keys=True)
    split_schema(ROOT, _rules(*GROUP_RULES))
    assert json.dumps(ROOT, sort_keys=True) == before


def test_node_type_names_follow_their_definitions():
    result = split_schema(ROOT, _rules(*GROUP_RULES), {"io.x.apps.v1.Deployment"})
    assert result.node_types_by_file == {"meta.json": set(), "apps.json": {"Deployment"}}


# ── Skipped definitions ───────────────────────────────────────────────────────

_SKIPPED_REF_ROOT = {"definitions": {
    "io.x.apps.v1.A": _obj(old=_ref("io.x.apps.v1beta1.Old"), keep={"type": "string"}),
    "io.x.apps.v1beta1.Old": {"type": "string", "enum": ["x"]},
}}


def test_ref_to_skipped_definition_is_an_error():
    with pytest.raises(SplitError, match="io.x.apps.v1.A references io.x.apps.v1beta1.Old"):
        split_schema(_SKIPPED_REF_ROOT, _rules(*GROUP_RULES))


def test_allow_skipped_refs_warns_and_falls_back_to_json_type():
    result = split_schema(_SKIPPED_REF_ROOT, _rules(*GROUP_RULES), allow_skipped_refs=True)
    assert result.warnings and "io.x.apps.v1beta1.Old" in result.warnings[0]
    old = _units(result)["apps"].document["definitions"]["A"]["properties"]["old"]
    assert old == {"type": "string"}


# ── Cycles and order ──────────────────────────────────────────────────────────

_CYCLE_ROOT = {"definitions": {
    "io.x.a.v1.A": _obj(b=_ref("io.x.b.v1.B")),
    "io.x.b.v1.B": _obj(a=_ref("io.x.a.v1.A")),
    "io.x.c.v1.C": _obj(a=_ref("io.x.a.v1.A")),
}}


def test_units_are_ordered_after_their_imports():
    units = [u.name for u in split_schema(ROOT, _rules(*GROUP_RULES)).units]
    assert units.index("meta") < units.index("apps")


def test_import_cycle_is_an_error_naming_the_edges():
    with pytest.raises(SplitError) as exc:
        split_schema(_CYCLE_ROOT, _rules(*GROUP_RULES))
    message = str(exc.value)
    assert "a -> b: io.x.a.v1.A references io.x.b.v1.B" in message
    assert "b -> a: io.x.b.v1.B references io.x.a.v1.A" in message


def test_merge_cycles_merges_into_first_unit():
    result = split_schema(_CYCLE_ROOT, _rules(*GROUP_RULES), merge_cycles=True)
    units = _units(result)
    assert set(units) == {"a", "c"}
    assert set(units["a"].document["definitions"]) == {"A", "B"}
    assert units["a"].document["definitions"]["A"]["properties"]["b"] == {"$ref": "#/definitions/B"}
    assert [u.name for u in result.units] == ["a", "c"]


# ── CLI ───────────────────────────────────────────────────────────────────────

_OAS = {
    "swagger": "2.0",
    "info": {"title": "x", "version": "1"},
    "paths": {"/deployments": {"get": {}, "post": {"parameters": [
        {"in": "body", "name": "body", "schema": {"$ref": "#/definitions/io.x.apps.v1.Deployment"}},
    ]}}},
    "definitions": ROOT["definitions"],
}


def _write_inputs(tmp_path):
    api = tmp_path / "api.json"
    api.write_text(json.dumps(_OAS))
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"rules": GROUP_RULES}))
    return api, rules


def test_cli_split_writes_units_and_profile(tmp_path):
    api, rules = _write_inputs(tmp_path)
    out = tmp_path / "out"
    rc = main_oas([str(api), "--split", str(rules), "--profile", "io.x:1.0", "-o", str(out)])
    assert rc == 0
    assert (out / "meta.yaml").exists() and (out / "apps.yaml").exists()
    profile = yaml.safe_load((out / "profile.yaml").read_text())
    assert profile["profile"] == "io.x:1.0"
    assert profile["imports"] == ["meta.yaml", "apps.yaml"]
    assert "Entry-Definitions: profile.yaml" in (out / "TOSCA.meta").read_text()
    apps = yaml.safe_load((out / "apps.yaml").read_text())
    assert "Deployment" in apps["node_types"]
    assert {"url": "meta.yaml", "namespace": "meta"} in apps["imports"]
    deployment = apps["node_types"]["Deployment"]
    assert deployment["properties"]["metadata"]["type"] == "meta:ObjectMeta"
    assert deployment["properties"]["spec"]["type"] == "DeploymentSpec"


def test_cli_split_error_returns_1(tmp_path, capsys):
    api, _ = _write_inputs(tmp_path)
    rules = tmp_path / "bad.yaml"
    rules.write_text(yaml.safe_dump({"rules": GROUP_RULES[1:3]}))
    assert main_oas([str(api), "--split", str(rules), "-o", str(tmp_path / "out")]) == 1
    assert "match no rule" in capsys.readouterr().err


def test_cli_split_takes_one_input(tmp_path, capsys):
    api, rules = _write_inputs(tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(_OAS))
    assert main_oas([str(api), str(other), "--split", str(rules), "-o", str(tmp_path / "o")]) == 1
    assert "exactly one input" in capsys.readouterr().err


# ── --profile without --split ─────────────────────────────────────────────────

def _write_schemas(tmp_path, schemas):
    src = tmp_path / "src"
    src.mkdir()
    for name, schema in schemas.items():
        (src / name).write_text(json.dumps(schema))
    return src


def test_profile_for_a_multi_file_run_imports_in_dependency_order(tmp_path):
    src = _write_schemas(tmp_path, {
        "a.json": {"title": "A", "type": "object",
                   "properties": {"b": {"$ref": "b.json"}}},
        "b.json": {"title": "B", "type": "object", "properties": {"x": {"type": "string"}}},
    })
    out = tmp_path / "out"
    assert main_json([str(src), "--profile", "io.x:1.0", "-o", str(out)]) == 0
    profile = yaml.safe_load((out / "profile.yaml").read_text())
    assert profile["profile"] == "io.x:1.0"
    assert profile["imports"] == ["b.yaml", "a.yaml"]
    assert "Entry-Definitions: profile.yaml" in (out / "TOSCA.meta").read_text()


def test_profile_for_a_single_file(tmp_path):
    src = _write_schemas(tmp_path, {"s.json": {"title": "S", "type": "object", "properties": {}}})
    out = tmp_path / "out"
    assert main_json([str(src / "s.json"), "--profile", "io.x:1.0", "-o", str(out)]) == 0
    assert yaml.safe_load((out / "profile.yaml").read_text())["imports"] == ["s.yaml"]


def test_profile_warns_about_imports_the_run_did_not_write(tmp_path, capsys):
    from any2tosca.cli import main_redfish
    src = _write_schemas(tmp_path, {
        "Fan.json": {"$ref": "#/definitions/Fan", "definitions": {"Fan": {
            "type": "object", "properties": {"Speed": {"type": "integer"}}}}},
    })
    out = tmp_path / "out"
    assert main_redfish([str(src), "--profile", "org.dmtf:1.0", "-o", str(out)]) == 0
    assert yaml.safe_load((out / "profile.yaml").read_text())["imports"] == ["Fan.yaml"]
    err = capsys.readouterr().err
    assert "Core.yaml" in err                 # supplied by the user under the links model
    assert "Fan_caps.yaml" not in err         # written by this run


def test_profile_takes_name_and_version(tmp_path, capsys):
    src = _write_schemas(tmp_path, {"s.json": {"title": "S", "type": "object", "properties": {}}})
    assert main_json([str(src), "--profile", "io.x", "-o", str(tmp_path / "o")]) == 1
    assert "NAME:VERSION" in capsys.readouterr().err


def test_property_named_dollar_ref_is_kept():
    root = {"definitions": {"io.x.apps.v1.Props": _obj(**{
        "$ref": {"type": "string"}, "other": _ref("io.x.apps.v1.Other")}),
        "io.x.apps.v1.Other": _obj()}}
    props = _units(split_schema(root, _rules(*GROUP_RULES)))["apps"].document["definitions"]["Props"]["properties"]
    assert props["$ref"] == {"type": "string"}
    assert props["other"] == {"$ref": "#/definitions/Other"}
