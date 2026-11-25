# any2tosca

any2tosca turns the schemas you already have into
[TOSCA 2.0](https://docs.oasis-open.org/tosca/TOSCA/) types. API definitions,
hardware models and network data models already describe the resources a
system is built from; any2tosca converts them into TOSCA data types and node
types, so the types a TOSCA profile needs are generated from the source of
truth rather than written by hand. The generated types can then be checked
with [tosca-core](https://github.com/ubicity-corp/tosca-core), the open-source
TOSCA parser and validator.

| Command | Input |
|---|---|
| `any2tosca` | [JSON Schema](https://json-schema.org/) |
| `oas2tosca` | [OpenAPI](https://www.openapis.org/) 2.0 / 3.x |
| `oas2tosca --kubernetes` | the [Kubernetes](https://kubernetes.io/) OpenAPI specification |
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
| `--profile NAME:VERSION` | also package the output as a profile (see [Output as a profile](#output-as-a-profile)) |
| `--relationship-model links\|value` | how references to node types are converted (see [Relationship models](#relationship-models)); the default is `links` for `redfish2tosca` and `value` for the others |
| `--extension-metadata KEY` | copy schema key `KEY` (typically a vendor extension, `x-...`) into the `metadata` of the type or property it appears on; repeatable |

### Several files

A schema that references another file becomes a TOSCA file that imports the
other one's output, by `url` and with a `namespace`. Referenced files are
looked for beside the input file first, then in each `-I` directory. TOSCA does
not allow circular imports, so reference cycles between files are broken
during conversion ([docs/circular-import-algorithm.md](docs/circular-import-algorithm.md)).

### Output as a profile

`--profile NAME:VERSION` also writes `profile.yaml`, which declares the profile
and imports every file the run writes in dependency order, and a `TOSCA.meta`
that names it as the entry definitions. The output directory is then a
profile that can be validated and onboarded as a whole. A warning names any
file the output imports but the run did not write, such as the `Core.yaml` of
the `links` relationship model; place it beside `profile.yaml`.

```bash
yang2tosca models/ --profile com.example.network:1.0 -o profile/
```

[tosca-core](https://github.com/ubicity-corp/tosca-core) checks the result
against TOSCA's rules:

```bash
pip install tosca-core
tosca-validate profile/
```

Without `--profile`, the generated files declare no profile name and are meant
to be imported by a profile or service template of your own.

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
other in a cycle, are errors. See [docs/splitting.md](docs/splitting.md).

## What it generates

- **Data types** for objects, enumerations and constrained scalars. An object
  type derives from a parent only where the schema names one (a single `$ref`
  in `allOf`, `anyOf` or `oneOf`); a scalar type derives from its TOSCA
  primitive type, and a schema that accepts any JSON value derives from
  `string`.
- **Node types** for the schemas that describe resources, as each
  [input format](#input-formats) identifies them. Plain JSON Schema produces
  data types only.
- **Attributes** for node-type properties marked `readOnly: true`: values the
  system sets rather than ones a template supplies. In data types they stay
  properties.
- **Property names as the schema spells them**, including `-` and `.`. Type
  names have spaces, `-` and `.` replaced by `_`.

### Relationship models

A schema can refer to a node type's schema in two ways, and
`--relationship-model` chooses how such references are converted.

**`value`** (the default of `any2tosca`, `oas2tosca` and `yang2tosca`): a
reference embeds a value of the referenced schema. A property whose schema is
a node type's schema is typed with a companion data type, `<Name>Value`, with
the same properties as the node type `<Name>`. For an OpenAPI document whose
`POST /servers` takes a `Server` and whose `GET /servers` lists them, `oas2tosca`
writes:

```yaml
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
    attributes:
      state:
        type: string
```

**`links`** (the default of `redfish2tosca`): a reference to another resource
is a relationship. A property whose value is another node type becomes a
requirement, with a `Contains` or `DependsOn` relationship, and every node type
gets a `feature` and a `containable` capability that requirements can target.
The capability types are written to a companion file, `<name>_caps.yaml`, so
that other files can target a node type without importing it. The same
document with `--relationship-model links`:

```yaml
imports:
- url: Core.yaml
  namespace: core
- url: servers_caps.yaml
  namespace: srvrscps
node_types:
  Server:
    properties:
      ...                      # as above
    attributes:
      state:
        type: string
    capabilities:
      feature:
        type: srvrscps:ServerFeature
      containable:
        type: srvrscps:ServerContainable
```

### The base types: `Core.yaml`

Under the `links` model, node types build on four base types that any2tosca
does not generate: the `Feature` and `Containable` capability types and the
`Contains` and `DependsOn` relationship types. The output imports them from a
`Core.yaml` beside it, which you provide:

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

## Input formats

### JSON Schema

`any2tosca` converts plain JSON Schema:

| JSON Schema | TOSCA |
|---|---|
| `type: string` | `type: string` |
| `type: integer` | `type: integer` |
| `type: number` | `type: float` |
| `type: boolean` | `type: boolean` |
| `type: object` | `data_type` with `properties` |
| `type: array` | `type: list` with `entry_schema` |
| no `type`, no structure | `data_type` derived from `string` |
| `properties` | `properties` |
| `required` | `required: true/false` per property |
| `default` | `default` |
| `description` / `title` | `description` (folded block style) |
| `readOnly: true` (node types) | attribute |
| `minimum` / `maximum` | `$greater_or_equal` / `$less_or_equal` |
| `exclusiveMinimum` / `exclusiveMaximum` | `$greater_than` / `$less_than` |
| `minLength` / `maxLength` | `$greater_or_equal` / `$less_or_equal` on `$length: [$value]` |
| `pattern` | `$matches` |
| `enum` | `$valid_values` |
| `items` | `entry_schema` |
| `additionalProperties` | `type: map` with `entry_schema` |
| `$ref` (local) | the referenced type, by name |
| `$ref` (external file) | `namespace:TypeName` and an `imports` entry |
| `allOf` with a single `$ref` | `derived_from` |
| `allOf` with mixed schemas | merged properties |
| `anyOf` / `oneOf` with a `null` alternative | the non-null member |
| `anyOf` / `oneOf` with a single `$ref` | `derived_from` |
| `anyOf` / `oneOf` with several `$ref`s | first `$ref` → `derived_from`, the others' properties merged |
| `definitions` / `$defs` | top-level `data_types` entries |

The [JSON Schema reference](https://json-schema.org/understanding-json-schema/reference)
describes the keywords themselves.

### OpenAPI

`oas2tosca` converts the schemas of an OpenAPI 2.0 (`definitions`) or 3.x
(`components.schemas`) document with the JSON Schema rules above. A schema
used as the request body of a `POST` to a path that also supports `GET`, a
resource the API creates and can return, becomes a node type; the rest become
data types. A schema posted only to a path without `GET` is a request to
perform an action, and becomes a data type.

### Kubernetes

`oas2tosca --kubernetes` converts the Kubernetes OpenAPI specification
(`api/openapi-spec/swagger.json` in the Kubernetes repository) into one TOSCA
file per GA API group, plus the shared `meta`, `resource`, `runtime` and
`intstr` packages and the `apiextensions` and `apiregistration` APIs:

```bash
oas2tosca --kubernetes swagger.json --profile io.kubernetes:1.37 -o io/kubernetes/1.37/
```

- Alpha and beta versions and the `*List` kinds are left out. The packaged
  split rules ([any2tosca/rules/kubernetes.yaml](any2tosca/rules/kubernetes.yaml))
  decide the files; `--split RULES` replaces them. A GA field whose type is an
  alpha or beta type falls back to its JSON type (`map`, `string`, ...), with a
  warning.
- Resources (schemas posted to a collection) become node types. Their
  `x-kubernetes-group-version-kind` becomes a `metadata` map, with `group: ''`
  for the core group; `apiVersion` and `kind`, which the type determines, are
  not properties; and `status` is an attribute.
- `x-kubernetes-list-type`, `x-kubernetes-list-map-keys` and
  `x-kubernetes-map-type` become property `metadata`.
- A resource embedded in another schema (a `StatefulSet`'s
  `volumeClaimTemplates`) is typed with its companion data type,
  `PersistentVolumeClaimValue`.

[docs/kubernetes.md](docs/kubernetes.md) describes the conversion in full.

### Redfish

`redfish2tosca` first normalizes a Redfish schema into plain JSON Schema,
dropping OData annotations, flattening nullable alternatives and promoting
`x-longDescription` to `description`, then converts it with the Redfish rules:

- The resource a schema file defines becomes a node type.
- A family of versioned schemas (`Drive.v1_13_0.json`, `Drive.v1_14_0.json`, ...)
  becomes a chain of `derived_from` types: an `anyOf` / `oneOf` of versioned
  `$ref`s derives from the highest version, with the properties of unversioned
  members merged in. All versions share one capability file (`Drive_caps.yaml`).
- `Links` become `DependsOn` requirements, and `Actions` become an interface
  type.

These rules apply under `redfish2tosca` only; `any2tosca` converts the same
files as plain JSON Schema.
[docs/redfish-conversion.md](docs/redfish-conversion.md) describes every step.

### YANG

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
| `enumeration` | `$valid_values` |
| `range` | `$greater_or_equal` / `$less_or_equal` |
| `length` | `$greater_or_equal` / `$less_or_equal` on `$length: [$value]` |
| `pattern` | `$matches` |
| `mandatory true` | `required: true` |
| named typedef reference | the typedef's `data_type`, by name |
| `leafref` | `string` |

YANG integer types (`int8`–`int64`, `uint8`–`uint64`) map to TOSCA `integer`;
`decimal64` maps to `float`; all string-like types map to `string`.

## Limitations

- **JSON Schema keywords left out of the conversion:** `const`, `format`,
  `multipleOf`, `minItems` / `maxItems`, `uniqueItems`, `patternProperties`,
  `if` / `then` / `else`, `not`, `writeOnly`, and OpenAPI's `discriminator`.
  The values they constrain are converted; the constraints they add are not.
  `readOnly` is converted on node types only.
- **Unions:** an `anyOf` / `oneOf` of several `$ref`s becomes one type, so the
  choice between the alternatives is lost.
- **Kubernetes `IntOrString`:** TOSCA has no union type, so `IntOrString`
  fields are strings and do not take an integer value.

## Python API

Each command is also available from Python. Every input format in
`any2tosca.formats` (`JSON_SCHEMA`, `OPENAPI`, `KUBERNETES`, `REDFISH`, `YANG`)
loads and normalizes an input file and carries the conventions its command
converts with, so the same code converts any of them as the command does:

```python
from pathlib import Path
from any2tosca import convert_schema, build_tosca_document
from any2tosca.companions import add_value_companions
from any2tosca.formats import OPENAPI   # or JSON_SCHEMA, REDFISH, YANG
from any2tosca.yaml_utils import dump_tosca_yaml

fmt, path = OPENAPI, Path("servers.yaml")
normalized = fmt.normalize(fmt.load(path), path)
root_schema = normalized.root_schema
if fmt.conventions.relationship_model == "value":
    # References embed values: point those to node-type schemas at <Name>Value types.
    root_schema = add_value_companions(root_schema, normalized.node_type_names)
result = convert_schema(root_schema, normalized.top_level_name, normalized.node_type_names,
                        base_path=path.parent, source_path=path,
                        conventions=fmt.conventions)
print(dump_tosca_yaml(build_tosca_document(
    result.data_types, result.node_types, result.relationship_types or None,
    result.interface_types or None, imports=result.imports or None)))
```

Under the `links` model, `result.capability_types` holds the capability types
for `result.caps_file`; `any2tosca.converter.build_caps_document` builds that file's document.
The Kubernetes format also splits its input; `any2tosca.split.split_schema`
does that, as `oas2tosca --kubernetes` does. These functions and classes are
not yet a stable API: they may change in any release before 1.0.

## Development

```bash
git clone https://github.com/ubicity-corp/any2tosca
cd any2tosca
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
