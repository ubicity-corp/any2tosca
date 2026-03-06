# any2tosca

Convert industry-standard schema formats to
[TOSCA 2.0](https://docs.oasis-open.org/tosca/TOSCA/) node types and data
types (YAML).

| Tool | Input format |
|---|---|
| `any2tosca` | [JSON Schema](https://json-schema.org/) |
| `oas2tosca` | [OpenAPI](https://www.openapis.org/) 2.0 / 3.x |
| `redfish2tosca` | [Redfish](https://www.dmtf.org/standards/redfish) JSON Schema |
| `yang2tosca` | [YANG](https://datatracker.ietf.org/doc/html/rfc7950) modules |

## Installation

```bash
pip install any2tosca
```

Or install from source:

```bash
git clone https://github.com/ubicity-corp/any2tosca
cd any2tosca
pip install -e ".[dev]"
```

## CLI usage

Each tool follows the same pattern: one TOSCA file is written per input file,
using the same base name.  The `-o` flag specifies the output directory
(default: `tosca/`).

```bash
# JSON Schema
any2tosca my_schema.json
any2tosca schemas/ -o tosca/

# OpenAPI
oas2tosca openapi.yaml -o tosca/

# Redfish
redfish2tosca SimpleServer.v1_0_1.json -o tosca/

# YANG
yang2tosca server.yang -o tosca/

# Verbose output (lists converted types)
any2tosca schemas/ -v
```

When a schema file references other schemas in the same directory, the
generated TOSCA file includes an `imports` section with a `namespace` for
each dependency.

## Python API

### JSON Schema

```python
import json
from pathlib import Path
from any2tosca import convert_schema, build_tosca_document
from any2tosca.yaml_utils import dump_tosca_yaml

with open("my_schema.json") as f:
    root_schema = json.load(f)

result = convert_schema(root_schema, top_name="MyType",
                        base_path=Path("my_schema.json").parent)
tosca_doc = build_tosca_document(result.data_types, result.node_types,
                                 result.imports or None)
print(dump_tosca_yaml(tosca_doc))
```

### OpenAPI

```python
import json
from any2tosca.openapi import OpenAPIExtractor
from any2tosca import convert_schema, build_tosca_document
from any2tosca.yaml_utils import dump_tosca_yaml

with open("openapi.json") as f:
    doc = json.load(f)

root_schema, node_type_names = OpenAPIExtractor().extract(doc)
result = convert_schema(root_schema, "api", node_type_names)
print(dump_tosca_yaml(build_tosca_document(result.data_types, result.node_types)))
```

### Redfish

```python
import json
from pathlib import Path
from any2tosca.redfish import RedfishNormalizer
from any2tosca import convert_schema, build_tosca_document
from any2tosca.yaml_utils import dump_tosca_yaml

with open("SimpleServer.v1_0_1.json") as f:
    schema = json.load(f)

root_schema, node_type_names = RedfishNormalizer().normalize(schema)
result = convert_schema(root_schema, "SimpleServer", node_type_names,
                        base_path=Path("SimpleServer.v1_0_1.json").parent)
print(dump_tosca_yaml(build_tosca_document(result.data_types, result.node_types,
                                           result.imports or None)))
```

### YANG

```python
from pathlib import Path
from any2tosca.yang import YANGNormalizer
from any2tosca import convert_schema, build_tosca_document
from any2tosca.yaml_utils import dump_tosca_yaml

yang_path = Path("server.yang")
normalizer = YANGNormalizer()
root_schema, node_type_names = normalizer.normalize(yang_path)
result = convert_schema(root_schema, normalizer.module_name, node_type_names,
                        base_path=yang_path.parent)
print(dump_tosca_yaml(build_tosca_document(result.data_types, result.node_types)))
```

## Conversion reference

### JSON Schema → TOSCA

| JSON Schema | TOSCA |
|---|---|
| `type: string` | `type: string` |
| `type: integer` | `type: integer` |
| `type: number` | `type: float` |
| `type: boolean` | `type: boolean` |
| `type: object` | `data_type` with `properties` |
| `type: array` | `type: list` with `entry_schema` |
| `properties` | `properties` |
| `required` | `required: true/false` per property |
| `default` | `default` |
| `description` / `title` | `description` (folded block style) |
| `minimum` / `maximum` | `$greater_or_equal` / `$less_or_equal` |
| `exclusiveMinimum/Maximum` | `$greater_than` / `$less_than` |
| `minLength` / `maxLength` | `$min_length` / `$max_length` |
| `enum` | `$valid_values` |
| `pattern` | `$pattern` |
| `items` | `entry_schema` |
| `additionalProperties` | `entry_schema` |
| `$ref` (local) | resolved inline |
| `$ref` (external file) | `namespace:TypeName` + `imports` entry |
| `allOf` with single `$ref` | `derived_from` |
| `allOf` with mixed schemas | merged properties |
| `definitions` / `$defs` | top-level `data_types` entries |

Scalar/enum definitions derive from the corresponding TOSCA primitive type.
Complex (object) types emit no `derived_from` unless an `allOf` entry
supplies an explicit parent.

Schemas that represent resources (identified by OpenAPI POST bodies, the
Redfish top-level `$ref`, or top-level YANG containers) become `node_types`.
`derived_from` is only set when an `allOf` entry references another type.

### YANG → TOSCA

| YANG | TOSCA |
|---|---|
| `typedef` | `data_type` |
| `container` (top-level) | `node_type` |
| `list` (top-level) | `node_type` |
| `container` (nested) | inline `data_type` |
| `list` (nested) | `list` property with `entry_schema` |
| `leaf` | scalar property |
| `leaf-list` | `list` property |
| `grouping` / `uses` | inlined by pyang before conversion |
| `enumeration` | `$valid_values` constraint |
| `range` | `$greater_or_equal` / `$less_or_equal` |
| `length` | `$min_length` / `$max_length` |
| `pattern` | `$pattern` |
| `mandatory true` | `required: true` |
| Named typedef ref | `$ref` → resolved `data_type` |

YANG integer types (`int8`–`int64`, `uint8`–`uint64`) map to TOSCA `integer`;
`decimal64` maps to `float`; all string-like types map to `string`.

## Running tests

```bash
pytest
```

## License

MIT
