# any2tosca

any2tosca turns the schemas you already have into
[TOSCA 2.0](https://docs.oasis-open.org/tosca/TOSCA/) types. API definitions,
hardware models and network data models already describe the resources a
system is built from; any2tosca converts them into TOSCA data types and node
types, so the types a TOSCA profile needs are generated from the source of
truth rather than written by hand. The generated types can then be checked
with [tosca-core](https://github.com/ubicity-corp/tosca-core), the open-source
TOSCA parser and validator.

| Command | Input format |
|---|---|
| `any2tosca` | [JSON Schema](https://json-schema.org/) |
| `oas2tosca` | [OpenAPI](https://www.openapis.org/) 2.0 / 3.x |
| `redfish2tosca` | [Redfish](https://www.dmtf.org/standards/redfish) JSON Schema |
| `yang2tosca` | [YANG](https://datatracker.ietf.org/doc/html/rfc7950) modules |

## A quick example

Given `server.json`:

```json
{
  "title": "Server",
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "cpus": {"type": "integer", "minimum": 1, "maximum": 64},
    "disk": {"type": "string", "enum": ["ssd", "hdd"]}
  },
  "required": ["name", "cpus"]
}
```

`any2tosca server.json` writes `tosca/server.yaml`:

```yaml
tosca_definitions_version: tosca_2_0
description: >-
  Types generated from JSON Schema by any2tosca
data_types:
  Server:
    description: >-
      Server
    properties:
      name:
        type: string
        required: true
      cpus:
        type: integer
        required: true
        validation:
          $and:
          - $greater_or_equal:
            - $value
            - 1
          - $less_or_equal:
            - $value
            - 64
      disk:
        type: string
        required: false
        validation:
          $valid_values:
          - $value
          - - ssd
            - hdd
```

## Installation

any2tosca needs Python 3.10 or later:

```bash
pip install any2tosca
```

For development, install from a checkout with the test tools:

```bash
git clone https://github.com/ubicity-corp/any2tosca
cd any2tosca
pip install -e ".[dev]"
```

## Usage

Each command takes input files or directories and writes one TOSCA file per
input file, with the same base name, into the output directory:

```bash
any2tosca my_schema.json                 # writes tosca/my_schema.yaml
any2tosca schemas/ -o tosca/             # every schema in a directory
oas2tosca openapi.yaml -o tosca/
redfish2tosca ComputerSystem.v1_20_0.json -I /path/to/DMTF/json-schema -o tosca/
yang2tosca server.yang -o tosca/
```

| Option | |
|---|---|
| `-o DIR`, `--output DIR` | the output directory (default: `tosca/`) |
| `-I DIR`, `--include-dir DIR` | another directory to search for referenced schema files; repeatable |
| `-v`, `--verbose` | list the types as they are converted |
| `--extension-metadata KEY` | copy schema key `KEY` (typically a vendor extension, `x-...`) into the `metadata` of the type or property it appears on; repeatable |
| `--relationship-model links\|value` | how references to node types are converted (see [What it generates](#what-it-generates)); the default is `value` for `oas2tosca` and `links` for the others |
| `--profile NAME:VERSION` | also write `profile.yaml`, importing every file the run writes in dependency order, and `TOSCA.meta`, so the output directory is a profile; a warning names any imported file the run did not write (such as `Core.yaml`) |

A schema that references another file becomes a TOSCA file that imports the
other one's output, by `url` and with a `namespace`. Referenced files are
looked for beside the input file first, then in each `-I` directory. TOSCA does
not allow circular imports, so reference cycles between files are broken
during conversion ([docs/circular-import-algorithm.md](docs/circular-import-algorithm.md)).

### Splitting one input into several files

A single large input, such as an OpenAPI document whose schema names follow a
package convention, can be converted into several TOSCA files that import each
other. Split rules, in a YAML file, assign each schema to a file by its name:

```yaml
rules:
  - match: '.*List'                             # first matching rule wins
    skip: true
  - match: 'io\.k8s\.apimachinery\.pkg\.apis\.meta\.v1\.(?P<name>[^.]+)'
    unit: meta                                  # written to meta.yaml
  - match: 'io\.k8s\.api\.(?P<unit>[a-z]+)\.v1\.(?P<name>[^.]+)'
  - match: '.*'
    skip: true
```

The `unit` named group or the `unit` key names the output file; the `name`
named group gives the TOSCA type name (otherwise the full schema name is used).
A rule may set the file's import `namespace` (default: the unit name) or `skip`
the schemas it matches. Schemas that match no rule go to the unit named by
`default_unit`, and are an error without one.

```bash
oas2tosca api.json --split rules.yaml --profile io.example:1.0 -o profile/
```

| Option | |
|---|---|
| `--split RULES` | split the input using the rules in `RULES`; takes exactly one input file |
| `--merge-cycles` | merge files that would import each other in a cycle, instead of failing |
| `--allow-skipped-refs` | warn, instead of failing, when a converted type references a skipped schema |

Two schemas that get the same type name in one file, and files that import each
other in a cycle, are errors. See [docs/multi-output.md](docs/multi-output.md).

## What it generates

- **Data types** for objects, enumerations and constrained scalars. Object
  types are derived from a parent only where the schema names one (a single
  `$ref` in `allOf`, `anyOf` or `oneOf`); scalars derive from their TOSCA
  primitive type.
- **Node types** for the schemas that describe resources, as each format
  identifies them: OpenAPI request bodies of `POST` operations on paths that
  also support `GET`, the Redfish
  resource a schema file defines, and top-level YANG containers and lists.
  Plain JSON Schema produces data types only. A node-type property marked
  `readOnly: true`, a value the system sets rather than one the template
  supplies, becomes an **attribute** instead of a property.
- **Capabilities and requirements** that let node types be connected, under
  the `links` relationship model (the default of `any2tosca`, `redfish2tosca`
  and `yang2tosca`). Every node type gets a `feature` and a `containable`
  capability, whose types are written to a companion file, `<name>_caps.yaml`,
  so that other files can target the node type without importing it. A
  property whose value is another node type becomes a requirement, with a
  `Contains` or `DependsOn` relationship.
- **Embedded values** under the `value` relationship model (the default of
  `oas2tosca`), where a reference to a schema embeds a value of that schema
  rather than linking to another resource. A property whose schema is a node
  type's schema gets a companion data type, `<Name>Value`, with the same
  properties as the node type `<Name>`. No capabilities, requirements or
  `Core.yaml` import are generated.

For example, an OpenAPI document whose `POST /servers` takes a `Server`, and
whose `GET /servers` lists them, produces, with `--relationship-model links`:

```yaml
imports:
- url: Core.yaml
  namespace: core
- url: servers_caps.yaml
  namespace: srvrscps
node_types:
  Server:
    properties:
      name:
        type: string
        required: true
      cpus:
        type: integer
        required: false
        validation:
          $greater_or_equal:
          - $value
          - 1
    capabilities:
      feature:
        type: srvrscps:ServerFeature
      containable:
        type: srvrscps:ServerContainable
```

### The base types: `Core.yaml`

Node types build on four base types that any2tosca does not generate: the
`Feature` and `Containable` capability types and the `Contains` and
`DependsOn` relationship types. Output with node types under the `links`
relationship model imports them from a `Core.yaml` beside it, which you provide:

```yaml
tosca_definitions_version: tosca_2_0
relationship_types:
  Contains: {}
  DependsOn: {}
capability_types:
  Containable: {}
  Feature: {}
```

Importing these types from a named profile instead, with an option to choose
the profile, is planned.

## Using the output

The generated files are TOSCA type definitions. They declare no `profile:`
name, so they are used in one of two ways:

- **Imported** by a profile or service template of your own, through an
  `imports` entry with the file's `url`.
- **As a profile of their own:** add a `profile:` line naming it (for
  example `profile: com.example.servers:1.0`) to the main file, and it can be
  validated and onboarded as a profile.

[tosca-core](https://github.com/ubicity-corp/tosca-core) checks the result
against TOSCA's rules. A single file with a `profile:` line is validated
directly:

```bash
pip install tosca-core
tosca-validate tosca/server.yaml
```

Output of several files (node types, their `_caps.yaml` and `Core.yaml`) is
validated as a directory holding them, with a `TOSCA.meta` that names the main
file:

```
TOSCA-Meta-File-Version: 1.1
CSAR-Version: 2.0
Created-By: any2tosca
Entry-Definitions: servers.yaml
```

An option that writes the `profile:` line, and packages the output as a CSAR,
is planned.

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
| `anyOf` / `oneOf` with null alternative | flattened to non-null member |
| `anyOf` / `oneOf` with single `$ref` | `derived_from` |
| `anyOf` / `oneOf` with versioned `$refs` (same base name) | highest version → `derived_from` |
| `anyOf` / `oneOf` with versioned + unversioned `$refs` | highest versioned → `derived_from`, unversioned properties merged |
| `anyOf` / `oneOf` with multiple unversioned `$refs` | first `$ref` → `derived_from`, rest merged |
| `definitions` / `$defs` | top-level `data_types` entries |

[docs/jsonschema.md](docs/jsonschema.md) is a reference to the JSON Schema
keywords themselves.

### OpenAPI → TOSCA

`oas2tosca` converts the schemas of an OpenAPI 2.0 (`definitions`) or 3.x
(`components.schemas`) document with the JSON Schema rules above. A schema
used as the request body of a `POST` to a path that also supports `GET`, a
resource the API creates and can return, becomes a node type; the rest become
data types. A schema posted only to a path without `GET` is a request to
perform an action, and becomes a data type. References are converted by
value (the `value` relationship model): a property whose schema is a node
type's schema is typed with the companion data type `<Name>Value`.

### Kubernetes → TOSCA

`oas2tosca --kubernetes` converts the Kubernetes OpenAPI specification
(`api/openapi-spec/swagger.json` in the Kubernetes repository) into one TOSCA
file per GA API group, plus the shared `meta`, `resource`, `runtime` and
`intstr` packages and the `apiextensions` and `apiregistration` APIs:

```bash
oas2tosca --kubernetes swagger.json --profile io.kubernetes:1.35 -o io/kubernetes/1.35/
```

- Alpha and beta versions and the `*List` kinds are left out. The packaged
  split rules ([any2tosca/rules/kubernetes.yaml](any2tosca/rules/kubernetes.yaml))
  decide the files; `--split RULES` replaces them.
- Resources (schemas posted to a collection) become node types. Their
  `x-kubernetes-group-version-kind` becomes a `metadata` map, with `group: ''`
  for the core group; `apiVersion` and `kind`, which the type determines, are
  not properties; and `status` is an attribute.
- `x-kubernetes-list-type`, `x-kubernetes-list-map-keys` and
  `x-kubernetes-map-type` become property `metadata`.
- A resource embedded in another schema (a `StatefulSet`'s
  `volumeClaimTemplates`) is typed with its companion data type,
  `PersistentVolumeClaimValue`.

Design: [docs/kubernetes.md](docs/kubernetes.md).

### Redfish → TOSCA

`redfish2tosca` first normalizes a Redfish schema into plain JSON Schema,
dropping OData annotations and flattening nullable alternatives, then converts
it. The resource a schema file defines becomes a node type; a family of
versioned schemas (`Drive.v1_13_0.json`, `Drive.v1_14_0.json`, ...) becomes a
chain of `derived_from` types; `Links` become `DependsOn` requirements; and
`Actions` become an interface type. These Redfish rules apply under
`redfish2tosca` only; `any2tosca` converts the same files as plain JSON Schema.
[docs/redfish-conversion.md](docs/redfish-conversion.md) describes every step.

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

## Limitations

- **JSON Schema keywords left out of the conversion:** `const`, `format`,
  `multipleOf`, `minItems` / `maxItems`, `uniqueItems`, `patternProperties`,
  `if` / `then` / `else`, `not`, `writeOnly`, and OpenAPI's `discriminator`.
  The values they constrain are converted; the constraints they add are not.
  `readOnly` is converted on node types only (see
  [What it generates](#what-it-generates)); in data types it is left out.
- **Kubernetes `IntOrString`:** TOSCA has no union type, so `IntOrString`
  fields are strings and do not take an integer value.

## Python API

Each command is also available from Python. These functions and classes are
not yet a stable API: they may change in any release before 1.0.

### JSON Schema

```python
import json
from pathlib import Path
from any2tosca import convert_schema, build_tosca_document
from any2tosca.yaml_utils import dump_tosca_yaml

with open("my_schema.json") as f:
    root_schema = json.load(f)

result = convert_schema(root_schema, "MyType",
                        base_path=Path("my_schema.json").parent)
tosca_doc = build_tosca_document(result.data_types, result.node_types,
                                 imports=result.imports or None)
print(dump_tosca_yaml(tosca_doc))
```

### OpenAPI

Each format in `any2tosca.formats` (`JSON_SCHEMA`, `OPENAPI`, `KUBERNETES`,
`REDFISH`, `YANG`) loads and normalizes an input file and carries the
conventions its command converts with:

```python
from pathlib import Path
from any2tosca import convert_schema, build_tosca_document
from any2tosca.companions import add_value_companions
from any2tosca.formats import OPENAPI
from any2tosca.yaml_utils import dump_tosca_yaml

path = Path("openapi.json")
normalized = OPENAPI.normalize(OPENAPI.load(path), path)
# OpenAPI converts references by value: point them at <Name>Value data types.
root_schema = add_value_companions(normalized.root_schema, normalized.node_type_names)
result = convert_schema(root_schema, normalized.top_level_name, normalized.node_type_names,
                        conventions=OPENAPI.conventions)
print(dump_tosca_yaml(build_tosca_document(result.data_types, result.node_types)))
```

### Redfish

```python
from pathlib import Path
from any2tosca import convert_schema, build_tosca_document
from any2tosca.formats import REDFISH
from any2tosca.yaml_utils import dump_tosca_yaml

path = Path("SimpleServer.v1_0_1.json")
normalized = REDFISH.normalize(REDFISH.load(path), path)
# The Redfish conventions: links between resources, versioned schema families.
result = convert_schema(normalized.root_schema, normalized.top_level_name,
                        normalized.node_type_names, base_path=path.parent,
                        source_path=path, conventions=REDFISH.conventions)
print(dump_tosca_yaml(build_tosca_document(result.data_types, result.node_types,
                                           imports=result.imports or None)))
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

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

## Contributing

Report a problem or ask a question by opening an issue. Changes come as pull
requests, with a test that pins what they fix and `pytest` passing. Report a
security vulnerability privately, through the repository's Security tab,
rather than in an issue.

## License

Apache License 2.0; see [LICENSE](LICENSE).
