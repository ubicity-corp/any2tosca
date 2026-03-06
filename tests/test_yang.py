"""
tests/test_yang.py
------------------
Unit tests for any2tosca.yang (YANGNormalizer) and the yang2tosca CLI.
"""

from pathlib import Path

import pytest
import yaml

from any2tosca.yang import YANGNormalizer
from any2tosca.cli import main_yang

# ── Shared YANG fixture ───────────────────────────────────────────────────────

YANG = """\
module test-server {
  namespace "urn:example:test-server";
  prefix ts;

  typedef PowerState {
    type enumeration {
      enum On;
      enum Off;
      enum PoweringOn;
    }
    description "Power state of the server";
  }

  typedef PortNumber {
    type uint16 {
      range "1..65535";
    }
    description "A valid TCP/UDP port number";
  }

  grouping AddressGroup {
    leaf ip {
      type string {
        pattern "[0-9.]+";
      }
      description "IP address";
    }
    leaf port {
      type PortNumber;
    }
  }

  container Server {
    description "A physical server";
    leaf name {
      type string;
      mandatory true;
      description "Server name";
    }
    leaf cpu-count {
      type uint32;
    }
    leaf-list tags {
      type string;
      description "Tags";
    }
    leaf power {
      type PowerState;
    }
    container address {
      uses AddressGroup;
    }
    list disk {
      key "id";
      description "Attached disks";
      leaf id {
        type string;
        mandatory true;
      }
      leaf size-gb {
        type uint32;
      }
    }
  }
}
"""


@pytest.fixture()
def yang_file(tmp_path: Path) -> Path:
    p = tmp_path / "test-server.yang"
    p.write_text(YANG)
    return p


# ── YANGNormalizer.normalize ──────────────────────────────────────────────────

def test_module_name(yang_file):
    n = YANGNormalizer()
    n.normalize(yang_file)
    assert n.module_name == "test-server"


def test_top_level_container_is_node_type(yang_file):
    n = YANGNormalizer()
    _, node_type_names = n.normalize(yang_file)
    assert "Server" in node_type_names


def test_typedef_in_definitions(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    assert "PowerState" in root["definitions"]
    assert "PortNumber" in root["definitions"]


def test_typedef_enumeration(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    ps = root["definitions"]["PowerState"]
    assert ps["type"] == "string"
    assert ps["enum"] == ["On", "Off", "PoweringOn"]


def test_typedef_description(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    assert root["definitions"]["PowerState"]["description"] == "Power state of the server"


def test_typedef_range_constraint(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    pn = root["definitions"]["PortNumber"]
    assert pn["type"] == "integer"
    assert pn["minimum"] == 1
    assert pn["maximum"] == 65535


def test_container_properties(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    props = root["definitions"]["Server"]["properties"]
    assert "name" in props
    assert "cpu-count" in props
    assert "power" in props


def test_leaf_string_type(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    assert root["definitions"]["Server"]["properties"]["name"]["type"] == "string"


def test_leaf_uint32_type(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    assert root["definitions"]["Server"]["properties"]["cpu-count"]["type"] == "integer"


def test_leaf_mandatory(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    assert "name" in root["definitions"]["Server"]["required"]


def test_leaf_ref_to_typedef(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    power = root["definitions"]["Server"]["properties"]["power"]
    assert power == {"$ref": "#/definitions/PowerState"}


def test_leaf_list_type(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    tags = root["definitions"]["Server"]["properties"]["tags"]
    assert tags["type"] == "array"
    assert tags["items"]["type"] == "string"


def test_nested_container(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    addr = root["definitions"]["Server"]["properties"]["address"]
    assert addr["type"] == "object"
    assert "ip" in addr["properties"]


def test_uses_expanded_in_container(yang_file):
    """grouping members appear in the container that uses them."""
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    addr = root["definitions"]["Server"]["properties"]["address"]
    assert "port" in addr["properties"]


def test_pattern_constraint(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    ip = root["definitions"]["Server"]["properties"]["address"]["properties"]["ip"]
    assert ip["pattern"] == "[0-9.]+"


def test_list_becomes_array(yang_file):
    n = YANGNormalizer()
    root, _ = n.normalize(yang_file)
    disk = root["definitions"]["Server"]["properties"]["disk"]
    assert disk["type"] == "array"
    assert disk["items"]["type"] == "object"
    assert "id" in disk["items"]["properties"]


# ── End-to-end via Converter ──────────────────────────────────────────────────

def test_end_to_end_node_type(yang_file):
    from any2tosca.converter import Converter
    n = YANGNormalizer()
    root, node_type_names = n.normalize(yang_file)
    result = Converter().convert_schema(root, n.module_name, node_type_names,
                                        base_path=yang_file.parent)
    assert "Server" in result.node_types
    assert result.node_types["Server"]["derived_from"] == "Root"


def test_end_to_end_data_type(yang_file):
    from any2tosca.converter import Converter
    n = YANGNormalizer()
    root, node_type_names = n.normalize(yang_file)
    result = Converter().convert_schema(root, n.module_name, node_type_names,
                                        base_path=yang_file.parent)
    assert "PowerState" in result.data_types
    assert result.data_types["PowerState"]["derived_from"] == "string"


def test_end_to_end_required_property(yang_file):
    from any2tosca.converter import Converter
    n = YANGNormalizer()
    root, node_type_names = n.normalize(yang_file)
    result = Converter().convert_schema(root, n.module_name, node_type_names,
                                        base_path=yang_file.parent)
    props = result.node_types["Server"]["properties"]
    assert props["name"]["required"] is True
    assert props["cpu_count"]["required"] is False


# ── CLI integration ───────────────────────────────────────────────────────────

def test_cli_yang(yang_file, tmp_path):
    out_dir = tmp_path / "out"
    rc = main_yang([str(yang_file), "-o", str(out_dir)])
    assert rc == 0
    out_file = out_dir / "test-server.yaml"
    assert out_file.exists()
    doc = yaml.safe_load(out_file.read_text())
    assert "Server" in doc["node_types"]
    assert "PowerState" in doc["data_types"]


def test_cli_yang_no_files(tmp_path):
    rc = main_yang([str(tmp_path / "nonexistent.yang")])
    assert rc != 0


def test_cli_yang_default_output(yang_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main_yang([str(yang_file)])
    assert rc == 0
    assert (tmp_path / "tosca" / "test-server.yaml").exists()
