# json2tosca

Convert [JSON Schema](https://json-schema.org/) documents to
[TOSCA](https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/)
data types (YAML).

## Installation

```bash
pip install json2tosca
```

Or install from source:

```bash
git clone https://github.com/your-org/json2tosca
cd json2tosca
pip install -e ".[dev]"
```

## CLI usage

```bash
# Single file → <name>.tosca.yaml
json2tosca my_schema.json

# Single file with explicit output path
json2tosca my_schema.json -o types.yaml

# Multiple files merged into one output
json2tosca a.json b.json -o combined.yaml

# Entire directory of .json files
json2tosca schemas/ -o tosca_types.yaml

# Verbose output
json2tosca schemas/ -v -o tosca_types.yaml
```

## Python API

```python
import json
from json2tosca import convert_schema, build_tosca_document
from json2tosca.yaml_utils import dump_tosca_yaml

with open("my_schema.json") as f:
    root_schema = json.load(f)

top_name = root_schema.get("title", "MyType")
data_types = convert_schema(root_schema, top_name)
tosca_doc  = build_tosca_document(data_types)
yaml_str   = dump_tosca_yaml(tosca_doc)

print(yaml_str)
```

## Conversion reference

| JSON Schema keyword | TOSCA equivalent |
|---|---|
| `type: string` | `type: string` |
| `type: integer` | `type: integer` |
| `type: number` | `type: float` |
| `type: boolean` | `type: boolean` |
| `type: object` | `type: map` |
| `type: array` | `type: list` |
| `properties` | `properties` |
| `required` | `required: true/false` per property |
| `default` | `default` |
| `description` / `title` | `description` |
| `minimum` / `maximum` | `greater_or_equal` / `less_or_equal` |
| `exclusiveMinimum/Maximum` | `greater_than` / `less_than` |
| `minLength` / `maxLength` | `min_length` / `max_length` |
| `enum` | `valid_values` |
| `pattern` | `pattern` |
| `items` | `entry_schema` |
| `additionalProperties` | `entry_schema` |
| `$ref` (local only) | resolved inline |
| `definitions` / `$defs` | top-level `data_types` entries |

## Running tests

```bash
pytest
```

## License

MIT
