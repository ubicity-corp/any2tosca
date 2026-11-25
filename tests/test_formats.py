"""
tests/test_formats.py
---------------------
``any2tosca --format``: every format reachable from one command, converting as
the format's own command does.
"""

import filecmp
import json

import pytest
import yaml

from any2tosca.cli import main_json, main_oas, main_redfish, main_yang
from any2tosca.formats import FORMATS

from tests.test_kubernetes import SPEC as KUBERNETES_SPEC
from tests.test_yang import YANG

OPENAPI = {
    "swagger": "2.0",
    "info": {"title": "servers", "version": "1"},
    "paths": {"/servers": {"get": {}, "post": {"parameters": [
        {"in": "body", "name": "b", "schema": {"$ref": "#/definitions/Server"}}]}}},
    "definitions": {"Server": {"type": "object", "properties": {"name": {"type": "string"}}}},
}
REDFISH = {
    "$ref": "#/definitions/Fan",
    "definitions": {"Fan": {"type": "object", "properties": {"Speed": {"type": "integer"}}}},
}


def _same_output(a, b):
    cmp = filecmp.dircmp(a, b)
    assert not cmp.left_only and not cmp.right_only and not cmp.diff_files, (
        cmp.left_only, cmp.right_only, cmp.diff_files)


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_format_names_every_format():
    assert sorted(FORMATS) == ["jsonschema", "kubernetes", "openapi", "redfish", "yang"]


@pytest.mark.parametrize("fmt,name,content,command,extra", [
    ("openapi", "api.json", OPENAPI, main_oas, []),
    ("redfish", "Fan.json", REDFISH, main_redfish, []),
    ("yang", "test-server.yang", YANG, main_yang, []),
    ("kubernetes", "swagger.json", KUBERNETES_SPEC, main_oas, ["--kubernetes"]),
])
def test_format_converts_as_the_format_command_does(tmp_path, fmt, name, content, command, extra):
    path = _write(tmp_path, name, content)
    assert command([*extra, str(path), "-o", str(tmp_path / "command")]) == 0
    assert main_json(["--format", fmt, str(path), "-o", str(tmp_path / "format")]) == 0
    _same_output(tmp_path / "command", tmp_path / "format")


def test_default_format_is_json_schema(tmp_path):
    path = _write(tmp_path, "s.json", {"title": "S", "type": "object", "properties": {}})
    assert main_json([str(path), "-o", str(tmp_path / "a")]) == 0
    assert main_json(["--format", "jsonschema", str(path), "-o", str(tmp_path / "b")]) == 0
    _same_output(tmp_path / "a", tmp_path / "b")


def test_relationship_model_defaults_to_the_formats(tmp_path):
    path = _write(tmp_path, "Fan.json", REDFISH)
    assert main_json(["--format", "redfish", str(path), "-o", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "Fan_caps.yaml").exists()          # links: capabilities
    doc = yaml.safe_load((tmp_path / "out" / "Fan.yaml").read_text())
    assert {"url": "Core.yaml", "namespace": "core"} in doc["imports"]


def test_explicit_relationship_model_overrides_the_formats(tmp_path):
    path = _write(tmp_path, "Fan.json", REDFISH)
    assert main_json(["--format", "redfish", "--relationship-model", "value",
                      str(path), "-o", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "out" / "Fan_caps.yaml").exists()


def test_kubernetes_format_requires_value_model(tmp_path, capsys):
    path = _write(tmp_path, "swagger.json", KUBERNETES_SPEC)
    assert main_json(["--format", "kubernetes", "--relationship-model", "links",
                      str(path), "-o", str(tmp_path / "out")]) == 1
    assert "requires the value relationship model" in capsys.readouterr().err


def test_unknown_format_is_refused(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main_json(["--format", "swagger", "x.json"])
    assert exc.value.code == 2
