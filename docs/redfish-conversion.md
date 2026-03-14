# Redfish Schema Conversion

This document describes how `redfish2tosca` converts a Redfish JSON Schema
document into TOSCA 2.0 type definitions.

---

## Overview

Conversion happens in two sequential stages:

```
Redfish JSON Schema (.json)
        │
        ▼
  RedfishNormalizer.normalize()          [redfish.py]
        │
        ▼
  Plain JSON Schema dict
  + set of node-type names
        │
        ▼
  Converter.convert_schema()             [converter.py]
        │
        ▼
  ConversionResult
  ├── data_types      (TOSCA data_types)
  ├── node_types      (TOSCA node_types)
  ├── relationship_types
  ├── interface_types
  └── imports
        │
        ▼
  build_tosca_document()  →  TOSCA YAML file
```

---

## Stage 1 — Normalization (`RedfishNormalizer`)

`RedfishNormalizer.normalize()` strips Redfish-specific quirks and returns a
plain JSON Schema dict that the generic `Converter` can process.

### 1.1 Identify the resource type

Two layouts are handled:

| Layout | Mechanism | Example files |
|---|---|---|
| **Definitions-based** (typical) | Top-level `"$ref": "#/definitions/TypeName"` → `TypeName` is the resource | `Drive.json`, `ComputerSystem.json` |
| **Flat** (utility schemas) | No `definitions` section; resource name derived from `"title"` field | `Resource.json` |

For flat schemas the title is parsed as `"#Namespace.TypeName"` or
`"#Namespace.vX_Y_Z"` — the last non-version segment is taken as the type name.

The identified name is placed in the returned `node_type_names` set so the
converter promotes it to a TOSCA node type rather than a data type.

### 1.2 Drop OData and Redfish annotation properties

Properties whose names begin with `@odata.` or `@Redfish.` are removed from
every `properties` dict throughout the document. These are protocol-level
annotations (`@odata.id`, `@odata.type`, `@Redfish.ActionInfo`, …) that carry
no schema semantics.

### 1.3 Promote `x-longDescription`

Redfish uses `x-longDescription` for normative text when a shorter
`description` already exists. The normalizer promotes `x-longDescription` to
`description` wherever `description` is absent:

```json
{ "x-longDescription": "The unique identifier …" }
→
{ "description": "The unique identifier …" }
```

This promotion happens at both the definition level and inside every property
schema recursively.

### 1.4 Drop all other `x-*` extension keywords

All remaining Redfish extension keywords (`x-longDescription` after promotion,
`x-units`, `x-readonly`, `x-requiredOnCreate`, …) are dropped. Only standard
JSON Schema keywords and the Redfish `parameters` keyword (used for Actions,
see §2.5) are preserved.

### 1.5 Flatten nullable `anyOf` patterns

Redfish uses `anyOf: [{$ref: "…"}, {type: null}]` to express an optional
typed value. When the non-null alternative is the only non-null member, the
pattern is flattened:

```json
{ "anyOf": [{"$ref": "…/ResetType"}, {"type": "null"}] }
→
{ "$ref": "…/ResetType" }
```

The outer schema's other keywords (e.g. `description`) are merged into the
result. This flattening is applied recursively throughout all property schemas,
including inside `items` schemas of array-typed properties.

---

## Stage 2 — Conversion (`Converter`)

The normalized schema is processed by the generic `Converter`. This section
describes the Redfish-specific decisions that apply on top of the general
conversion logic.

### 2.1 Resource type → TOSCA node type

The definition named in `node_type_names` (identified in §1.1) is converted as
a **TOSCA node type** rather than a data type. Node types differ from data
types in the following ways:

- Properties that reference other node types are emitted as TOSCA
  `requirements` rather than plain properties (see §2.3 and §2.4).
- A `capabilities.feature: {type: {caps_ns}:{TypeName}Feature}` capability is
  added so the node type can be targeted by `core:DependsOn` requirements.
- A `capabilities.containable: {type: {caps_ns}:{TypeName}Containable}`
  capability is added so the node type can be a target of `core:Contains`
  requirements.

The `{TypeName}Feature` and `{TypeName}Containable` capability types are
defined in a companion **`{BaseName}_caps.yaml`** file that only imports
`Core.yaml`. This companion file is written alongside the main TOSCA output
file. Requirements that target a node type import only the small caps file
rather than the full resource file, which avoids circular TOSCA imports through
capability/requirement chains. All versioned schemas for the same resource
(e.g. `Drive.v1_13_0.json`, `Drive.v1_14_0.json`) share the same
`Drive_caps.yaml` file with identical capability type definitions.

Any other definition in the same file that is later found to have
node-type-referencing properties is also promoted to a node type during
post-processing (see §2.7).

### 2.2 Versioned schema families → `derived_from`

Redfish resource schemas come in pairs:

- **Unversioned stub** (`Drive.json`) — contains `anyOf` references to all
  known versioned variants and has no properties of its own.
- **Versioned schema** (`Drive.v1_14_0.json`) — defines the actual properties
  for a specific schema version.

When the converter encounters an `anyOf` whose members are versioned file refs
(`Drive.v1_14_0.json`, `Drive.v1_13_0.json`, …), it:

1. Groups refs by base name.
2. Picks the **highest version** as the `derived_from` parent for the TOSCA
   type (e.g. `drive_v1_14_0:Drive`).
3. Merges properties from all other versioned refs inline (they contribute
   additional properties but are not parent types).

The versioned YAML (e.g. `Drive.v1_14_0.yaml`) is recorded as a TOSCA import.

**Circular versioned refs** — when the winning versioned file itself contains a
`$ref` back to the unversioned stub (e.g. `Drive.v1_14_0.json` references
`Drive.json#/definitions/OemDrive`), the file-root bypass is attempted: the
import is still recorded if the current file is an explicit-root schema and the
versioned file does not itself reference the current file's root type back.
See `docs/circular-import-algorithm.md` for full details.

### 2.3 Links property → `core:DependsOn` requirements

A property named `Links` whose `$ref` resolves to a definition also named
`Links` is treated specially:

```
Links (property) → removed from node type properties
Links (definition) → resolved and each property examined:
    property refs a node type → core:DependsOn requirement on the parent node type
    property is plain data     → kept in a derived relationship type
```

**Relationship types** — if the `Links` definition mixes node-type references
with plain data properties (e.g. an `Oem` object), a `{NodeTypeName}DependsOn`
relationship type is created that `derived_from: core:DependsOn` and carries
those extra properties. All node-type-referencing Links properties then use
this derived relationship type.

```yaml
# Input (after normalization):
Links:
  type: object
  properties:
    Chassis: {$ref: Chassis.json#/definitions/Chassis}
    Oem:     {type: object}

# Output on the node type:
requirements:
  - Chassis:
      node: chassis:Chassis
      capability: chassiscaps:ChassisFeature   # from Chassis_caps.yaml
      relationship: DrivesDependsOn
      count_range: [0, 1]

# In relationship_types:
DrivesDependsOn:
  derived_from: core:DependsOn
  properties:
    Oem: {type: map, required: false}
```

The `Links` definition type is removed from `data_types` / `node_types` after
expansion.

For **list-valued** Links properties (e.g. `Chassis: [{$ref: …}]`), the
`count_range` is `[0, UNBOUNDED]`.

### 2.4 Node-type property promotion

After per-definition conversion, the converter scans all generated data types.
Any data type with a property whose type resolves to a node type (a
`namespace:TypeName` where `TypeName` is a file-root type, or a type listed in
`_node_type_names`) is promoted to a node type — because TOSCA data types
cannot contain node-type references.

The promoted type's node-type-referencing properties become requirements; any
remaining plain properties stay as `properties`.

Additionally, data types whose `derived_from` resolves to a node type are
promoted: TOSCA forbids a data type deriving from a node type.

### 2.5 Actions property → TOSCA interface type

A property named `Actions` whose `$ref` resolves to a definition also named
`Actions` is converted into a TOSCA **interface type**:

```
Actions (property)    → removed from node type properties
Actions (definition)  → each property examined:
    "#TypeName.OpName"  → operation named OpName
    "Oem"               → skipped (OemActions type also removed)
```

**Operation name** — the action property name `"#ComputerSystem.Reset"` yields
the operation name `Reset` (the segment after the last `.`).

**Per-operation mapping:**

| Redfish source | TOSCA target |
|---|---|
| Action definition `description` / `x-longDescription` | Operation `description` |
| Action definition `parameters` | Operation `inputs` |
| Parameter `$ref` | Input `type` (resolved via `_ref_to_tosca_type`; records import) |
| Parameter `description` / `x-longDescription` | Input `description` |
| Parameter `requiredParameter: true` | Input `required: true` |

**Generated interface type name:** `{NodeTypeName}Actions`

```yaml
# Example output:
interface_types:
  ComputerSystemActions:
    operations:
      Reset:
        description: This action resets the system.
        inputs:
          ResetType:
            type: resource:ResetType
            description: The type of reset.
            required: true

node_types:
  ComputerSystem:
    interfaces:
      Actions:
        type: ComputerSystemActions
```

The `Actions` container definition, `OemActions`, and all individual action
definitions (e.g. `Reset`) are removed from `data_types` / `node_types` after
extraction.

### 2.6 External `$ref` resolution and TOSCA imports

Redfish schemas extensively cross-reference each other via HTTP URIs:

```json
{ "$ref": "http://redfish.dmtf.org/schemas/v1/Resource.json#/definitions/Status" }
```

The converter resolves these by filename. For each external file referenced:

1. The file is looked up in `base_path` and any `-I` search directories.
2. A short TOSCA namespace is derived from the filename by stripping the
   version suffix, splitting on PascalCase boundaries, and contracting vowels
   to fit within 8 characters (e.g. `NetworkDeviceFunction` → `ntwrkdvc`).
3. The import is recorded as `namespace → StemName.yaml`.
4. The type reference is emitted as `namespace:TypeName`.

If the external file would create a circular TOSCA import, it is suppressed
according to the algorithm in `docs/circular-import-algorithm.md`.

### 2.7 Post-processing passes

After all definitions are converted, four passes clean up the output:

| Pass | What it does |
|---|---|
| **Data→node promotion (property-based)** | Any data type with a property referencing a node type is promoted to a node type; those properties become requirements. |
| **Data→node promotion (derived_from-based)** | Any data type whose `derived_from` resolves to a node type is promoted to a node type. |
| **Required-property relaxation guard** | Child type properties that would relax a `required: true` constraint inherited from an ancestor are removed (TOSCA forbids constraint relaxation). |
| **Expanded-type cleanup** | Types consumed by Links expansion (Links, OemActions, …) and Actions extraction (Actions, OemActions, Reset, …) are removed from the output. |

---

## Output structure

Each Redfish input file produces **two** TOSCA 2.0 YAML files (only non-empty
sections are written):

### Main file (`ComputerSystem.yaml`)

```yaml
tosca_definitions_version: tosca_2_0
description: Types generated from JSON Schema by any2tosca

imports:
  - url: Core.yaml
    namespace: core
  - url: ComputerSystem_caps.yaml
    namespace: computersystemcaps
  - url: Resource.yaml
    namespace: resource
  # … one entry per referenced external schema

data_types:
  Status:          {…}
  ResetType:       {…}
  # … simple/enum types and complex types with no node-type refs

relationship_types:
  DrivesDependsOn:
    derived_from: core:DependsOn
    properties: {…}

interface_types:
  ComputerSystemActions:
    operations:
      Reset: {…}

node_types:
  ComputerSystem:
    description: …
    derived_from: computersystemvone_xx_x:ComputerSystem
    properties:    {…}
    requirements:  [{Chassis: …}, {ManagedBy: …}]
    capabilities:
      feature:     {type: computersystemcaps:ComputerSystemFeature}
      containable: {type: computersystemcaps:ComputerSystemContainable}
    interfaces:
      Actions:     {type: ComputerSystemActions}
```

### Companion caps file (`ComputerSystem_caps.yaml`)

```yaml
tosca_definitions_version: tosca_2_0
description: Capability types generated from JSON Schema by any2tosca

imports:
  - url: Core.yaml
    namespace: core

capability_types:
  ComputerSystemFeature:
    derived_from: core:Feature
  ComputerSystemContainable:
    derived_from: core:Containable
```

Other TOSCA files that need to express a requirement targeting `ComputerSystem`
import only `ComputerSystem_caps.yaml` (not the full `ComputerSystem.yaml`),
which prevents circular TOSCA imports through requirement/capability chains.

---

## Type-mapping reference

### JSON Schema → TOSCA primitives

| JSON Schema `type` | TOSCA type |
|---|---|
| `string` | `string` |
| `integer` | `integer` |
| `number` | `float` |
| `boolean` | `boolean` |
| `array` | `list` |
| `object` (with `additionalProperties`) | `map` |
| `null` | `string` |

### Structural patterns

| JSON Schema pattern | TOSCA output |
|---|---|
| Object with `properties` | `data_types` entry with `properties` |
| Object named in `node_type_names` | `node_types` entry |
| `anyOf` with versioned refs | `derived_from` (highest version) + merged inline props |
| `anyOf` with single unversioned ref | Properties merged inline; no `derived_from` |
| `allOf` members | Properties and metadata merged; first ref → `derived_from` |
| `enum` | `data_types` entry with `derived_from: string` + `validation.$valid_values` |
| `minimum`/`maximum`/`pattern`/… | `validation` expression on the property or data type |
| `$ref` to external file (non-circular) | `namespace:TypeName`; import recorded |
| `$ref` to circular file | Inlined or `# namespace:TypeName` (commented out) |
| `Links` property | `core:DependsOn` requirements; `Links` type removed |
| `Actions` property | Interface type + operations; `Actions`/action types removed |

---

## Known omissions

- **`additionalProperties: false`** — not enforced in TOSCA; ignored.
- **`readOnly` / `writeOnly`** — no TOSCA equivalent; dropped.
- **`$schema` / `$id`** — used only for `$ref` resolution; not emitted.
- **OData annotation properties** (`@odata.*`, `@Redfish.*`) — dropped.
- **`Oem` extension objects** — converted as plain `map` properties or dropped
  from Links/Actions expansion.
- **`anyOf` union semantics** — when multiple `$ref` alternatives are present,
  their property union cannot be represented faithfully in TOSCA; a warning is
  emitted and properties are merged (last wins).
