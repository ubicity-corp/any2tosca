# Kubernetes Conversion

**Status:** Design draft, not implemented.

`oas2tosca --kubernetes` converts the Kubernetes OpenAPI specification
(`api/openapi-spec/swagger.json` in the Kubernetes repository) into the generated
part of the Kubernetes profile (`io.kubernetes:<minor release>`): one TOSCA 2.0
file per API group, packaged as one profile, with node types for the API
resources and data types for everything else.

It builds on [multi-output conversion](multi-output.md), which supplies the
per-group files and the profile packaging. This document covers the rest: which
schemas become node types, how references between resources are converted, which
Kubernetes extensions reach the output, and the Kubernetes-specific adjustments to
resource types.

Once both designs are implemented, `any2tosca` produces in one step what
`GENERATING.md` (in the profile) Part 1 and Part 2, Steps 1 to 3, produce today
from `oas2tosca` output plus hand edits, and the separate `oas2tosca` repository is
deleted.

The numbers below are from the 1.37 specification, the current stable release:
862 definitions, 203 of them carrying `x-kubernetes-group-version-kind`. The 1.35
specification, from which the published profile was generated, gives the same
results with smaller counts.

---

## Design overview

Most of the work is general OpenAPI behaviour, used by every OpenAPI conversion.
A small Kubernetes module supplies only what depends on Kubernetes conventions.

| Piece | Where | Kubernetes-specific? |
|---|---|---|
| [Node types: resources you can create and read back](#node-types) | `OpenAPIExtractor` | No |
| [References are by value](#references-are-by-value) | Schema pre-pass and `Converter` option, the default for OpenAPI | No |
| [Extensions copied to metadata](#extensions-to-metadata) | `Converter` | No (Kubernetes picks the list) |
| [`readOnly` properties become attributes](#readonly-becomes-attribute) | `Converter` | No |
| [Split rules for API groups](#split-rules) | `any2tosca/rules/kubernetes.yaml` | Yes |
| [Resource adjustments](#resource-adjustments) | `KubernetesNormalizer` | Yes |

---

## Node types

**Rule:** a schema becomes a node type when it is the body of a `POST` to a path
that also supports `GET`. That is a resource collection: clients create entries in
it and read them back. A schema posted only to a path with no `GET` is a request
to perform an action, not a resource, and becomes a data type.

This is a general change to `OpenAPIExtractor`, which today takes every `POST`
body. It applies to every OpenAPI conversion, with or without `--kubernetes`.

The 1.37 numbers:

- 101 schemas are `POST` bodies. Every one carries a group-version-kind, and none
  is a `*List` kind.
- 9 of the 101 are posted only to action paths: `Binding`, `Eviction` and
  `TokenRequest` (the `binding`, `eviction` and `token` subresources), and
  `SelfSubjectReview`, `TokenReview`, `LocalSubjectAccessReview`,
  `SelfSubjectAccessReview`, `SelfSubjectRulesReview` and `SubjectAccessReview`.
  The rule makes them data types.
- The remaining 92 are the resource kinds.

Group-version-kind is **not** used to pick node types. It also appears on the 95
`*List` kinds, on `DeleteOptions` and `WatchEvent` (each with one group-version-kind
entry per API group), and on `Scale`, `Status`, `APIGroup`, `APIVersions` and
`ComponentStatus`. None of these is a resource a template deploys. `oas2tosca`
uses request bodies as well, not group-version-kind. It logs the extension and
does not act on it.

---

## References are by value

In OpenAPI a `$ref` embeds the referenced schema's value: the JSON at that
position *is* an instance of the schema. The converter instead treats a reference
to a node-type schema as a relationship. A data type with such a property is
promoted to a node type, and the property becomes a requirement. That follows the
Redfish model, where a reference to another resource is a link (`@odata.id`).

For OpenAPI input, references are by value:

- A property that references a node-type schema stays a property. Its type is a
  **companion data type**: a data type generated from the same schema as the
  node type, named `<Kind>Value`. It is generated only when such a reference
  exists, in the same unit as the node type.
- No data type is promoted to a node type because of the properties it holds.

Companions are made by a **pre-pass over the schema**, before the split and
before conversion. For every `$ref` to a node-type schema, except one in the
`allOf` of another node-type schema (which is inheritance), the pre-pass adds a
definition named after the node-type definition with `Value` appended
(`io.k8s.api.core.v1.PersistentVolumeClaimValue`), a copy of the node-type schema,
and points the `$ref` at it. A companion name that already exists as a definition is an error. After the
pre-pass, no schema references a node-type schema by value. The split rules place
the companion with its node type, because both names share the same prefix.

In 1.37 the one case outside the `*List` kinds is `StatefulSetSpec.volumeClaimTemplates`,
a list of `PersistentVolumeClaim`. It becomes `entry_schema: core:PersistentVolumeClaimValue`.
The published profile has `entry_schema: core:PersistentVolumeClaim`, a node type
as a list entry, which a schema definition does not allow.

### Relationship model

By-value references are one of several Redfish conventions that `convert_schema`
applies to every input today. They all become one converter option,
`relationship_model`:

| Behaviour | `links` | `value` |
|---|---|---|
| A property of a node type that references a node type becomes a requirement | yes | never arises: the pre-pass leaves no such reference |
| A data type holding such a property is promoted to a node type | yes | never arises, as above |
| A data type derived from a node type becomes a node type with `Feature` and `Containable` capabilities | yes | never arises: the pre-pass points the base at `<Kind>Value` |
| Every node type gets a type-specific `Feature` capability | yes | no |
| Every node type gets a type-specific `Containable` capability | yes | no |
| `Core.yaml` imported under namespace `core` | yes | no |
| `<unit>_caps.yaml` written with the capability types | yes | no |
| Redfish `Links` and `Actions` properties expanded into requirements and an interface | yes | no: ordinary properties |

`links` is today's behaviour, unchanged, and the default of `any2tosca`,
`redfish2tosca` and `yang2tosca`. `value` is the default of `oas2tosca`. Each CLI
takes `--relationship-model links|value` to override its default. The by-value
pre-pass runs when the model is `value`.

---

## Extensions to metadata

A converter option, `extension_metadata`, lists vendor extension keys (`x-...`) to
copy into TOSCA `metadata`:

- from a definition schema into the type's `metadata`,
- from a property schema into the property's `metadata`.

Values are copied as YAML values, not stringified. The published profile already
uses list and map values in metadata (`x-kubernetes-list-map-keys: ['type']`), and
it onboards.

The Kubernetes list:

| Extension | 1.37 uses | Placed on | Meaning |
|---|---|---|---|
| `x-kubernetes-group-version-kind` | 203 | node types | API group, version and kind of the resource |
| `x-kubernetes-list-type` | 458 | list properties | `atomic`, `set` or `map`: how the list merges |
| `x-kubernetes-list-map-keys` | 109 | list properties | the keys that identify an entry of a `map` list |
| `x-kubernetes-map-type` | 47 | map properties | `atomic` or `granular`: how the map merges |

`x-kubernetes-map-type` is new compared to `oas2tosca`. It is the map counterpart
of `x-kubernetes-list-type`, and it is listed so that the merge semantics of every
collection are described.

Not copied: `x-kubernetes-patch-strategy` and `x-kubernetes-patch-merge-key`
(strategic-merge-patch hints that `x-kubernetes-list-type` and
`x-kubernetes-list-map-keys` supersede), and `x-kubernetes-unions`,
`x-kubernetes-embedded-resource`, `x-kubernetes-int-or-string`,
`x-kubernetes-preserve-unknown-fields` and `x-kubernetes-validations` (describing
union members, embedded objects and server-side validation, none of which a TOSCA
type expresses).

The specification gives group-version-kind as a list. For a node type it always
has one entry, and the normalizer replaces the list by that entry, so the metadata
is a map, as in the published profile. The core group is the empty string and is
written as `group: ''`, which makes the `GENERATING.md` Step 2 "empty values"
hand fix unnecessary.

---

## `readOnly` becomes attribute

A converter change for all input formats: a property of a node type whose schema
has `readOnly: true` is emitted as an **attribute** instead of a property. Data
types have no attributes, so in a data type it stays a property, as in a
companion `<Kind>Value`. In JSON
Schema, `readOnly` marks a value the server sets and the client does not supply,
which is exactly a TOSCA attribute that is not a property. The converter emits no
attributes today, and the README lists `readOnly` among the keywords it ignores.

The Kubernetes specification does not mark anything `readOnly`. The normalizer
sets it on the `status` property of every resource (see
[Resource adjustments](#resource-adjustments)).

---

## Split rules

`any2tosca/rules/kubernetes.yaml`, shipped as a package resource:

| Definitions | Unit | Notes |
|---|---|---|
| `io.k8s.apimachinery.pkg.apis.meta.v1.*` | `meta` | `ObjectMeta`, `LabelSelector`, ... |
| `io.k8s.apimachinery.pkg.api.resource.*` | `resource` | `Quantity` |
| `io.k8s.apimachinery.pkg.runtime.*` | `runtime` | `RawExtension` |
| `io.k8s.apimachinery.pkg.util.intstr.*` | `intstr` | `IntOrString` |
| `io.k8s.api.resource.v1.*` | `dra` | the `resource.k8s.io` group (dynamic resource allocation) |
| `io.k8s.api.autoscaling.v1.HorizontalPodAutoscaler*`, `...v1.CrossVersionObjectReference` | skipped | superseded by `autoscaling/v2` |
| `io.k8s.api.autoscaling.v1.*`, `io.k8s.api.autoscaling.v2.*` | `autoscaling` | |
| `io.k8s.api.<group>.v1.*` | `<group>` | every other GA group |
| `io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.*` | `apiextensions` | `CustomResourceDefinition` |
| `io.k8s.kube-aggregator.pkg.apis.apiregistration.v1.*` | `apiregistration` | `APIService` |
| any name ending in `List` | skipped | list responses, not deployable |
| everything else | skipped | alpha and beta versions, `io.k8s.apimachinery.pkg.version.Info` |

Rules are first-match, so the `List` and autoscaling rules come before the group
rules. The type name is the last segment of the definition name.

Two clashes in 1.37 decide rule order and unit names:

- The `resource.k8s.io` group (`io.k8s.api.resource.v1`) and the apimachinery
  `resource` package (`Quantity`) would both map to `resource`. The package keeps
  `resource`, which existing profile files import as `resource:Quantity`, and the
  group becomes `dra`.
- `autoscaling` is the only group with two GA versions. `v2` holds the current
  `HorizontalPodAutoscaler`; `v1` holds `Scale`, which exists only there. Both
  versions go into one unit, with the five `v1` HPA definitions skipped.

The same kind name in two groups (`Event` in `core` and `events`, `TokenRequest`
in `authentication` and `storage`, `ResourceClaim` in `core` and `dra`) is not a
clash: they land in different units.

The rules cover every GA group: 19 in 1.35, 20 in 1.37. The published profile aggregates five (`core`,
`apps`, `storage`, `networking`, `rbac`) and lists `batch` as remaining work.

---

## Resource adjustments

`KubernetesNormalizer` (`any2tosca/kubernetes.py`) runs after `OpenAPIExtractor`
and before the split. For each **node-type** schema it:

1. Replaces the `x-kubernetes-group-version-kind` list by its single entry.
2. Removes the `apiVersion` and `kind` properties. The node type determines
   both, and the group-version-kind metadata states them. Data types keep theirs:
   in `ObjectReference` or `OwnerReference`, `kind` is a real field.
3. Sets `readOnly: true` on the `status` property, so it becomes an attribute.

Steps 2 and 3 are the generated half of `GENERATING.md` Part 2, Step 3. The rest
of that step (the `Resource` base type and its `cluster` requirement) stays
hand-written.

The normalizer returns the extractor's `(root_schema, node_type_names)` modified,
the same interface `RedfishNormalizer` has. It adds no node types of its own.

---

## Command line

```bash
oas2tosca --kubernetes swagger.json --profile io.kubernetes:1.35 -o io/kubernetes/1.35/
```

`--kubernetes` sets:

- `--split` to the packaged Kubernetes rules,
- the relationship model to `value` (already the `oas2tosca` default),
- `extension_metadata` to the four keys above,
- `KubernetesNormalizer` after `OpenAPIExtractor`.

It is an explicit flag rather than detection from the input, so that the output of
a plain `oas2tosca` run never depends on what the document happens to contain.

---

## What regeneration changes in the published profile

Comparing the result of the new generator with the published profile:

| Area | Change |
|---|---|
| Version | TOSCA 2.0 directly; Part 2, Step 1 goes away. |
| `in_range` | Not emitted. The converter writes `minimum` and `maximum` as separate `$greater_or_equal` and `$less_or_equal` clauses. The published `$in_range` validations came from `oas2tosca` turning `format: int32` / `int64` into ranges; the converter ignores `format`, so they disappear. |
| Empty values | `group: ''` instead of `group: {}`. |
| `PersistentVolumeClaim` in `StatefulSetSpec` | Becomes `PersistentVolumeClaimValue`. |
| Action requests | `Binding` becomes a data type; it is a node type in the published `core.yaml`. |
| `PodTemplate` | Stays a node type (it is a resource collection). Part 2, Step 2 turns it into a data type by hand. |
| Groups | Every GA group (19 at 1.35) instead of 5. |
| `status` | An attribute, `apiVersion` and `kind` gone: the generated half of Part 2, Step 3. |

---

## Testing

- **Extractor:** a `POST` body on a path with `GET` is a node type; one on a
  `POST`-only path is a data type; both for OpenAPI 2.0 body parameters and 3.x
  `requestBody`. The existing fixtures in `tests/test_openapi.py` have `POST`-only
  paths and gain a `GET`, so their node-type expectations still hold.
- **By value:** a data type holding a node-type schema stays a data type and
  references `<Kind>Value`; the companion is generated once, in the node type's
  unit; no companion without such a reference; the `links` model unchanged
  (the Redfish tests keep passing).
- **Metadata:** listed keys copied at type and property level, values kept as YAML
  lists and maps; unlisted keys ignored.
- **`readOnly`:** emitted under `attributes`, absent from `properties`.
- **Normalizer:** group-version-kind collapsed to a map with `group: ''` for core;
  `apiVersion` and `kind` removed from node types only; `status` marked `readOnly`.
- **End to end:** the trimmed Kubernetes fixture from the multi-output tests,
  extended with `StatefulSet`, `PersistentVolumeClaim`, `Binding`, an autoscaling
  `v1` and `v2` pair, a `resource.k8s.io` kind and a `*List` kind. Golden output.
- **Baseline comparison:** the unmodified `oas2tosca` output for 1.35 is kept in the
  community repository (`tosca-community-contributions`, `profiles/io/k8s/`, one
  profile per API group). Converting the 1.35 specification and comparing the
  result with it, unit by unit, shows every difference in the generated part
  of the profile without the hand-written post-processing in the way. The
  differences should be exactly those listed under
  [What regeneration changes](#what-regeneration-changes-in-the-published-profile),
  plus the TOSCA 1.3 to 2.0 syntax.
- **Full spec:** converting the real `swagger.json` succeeds and writes one unit
  file per GA group plus six library and extension units (25 at 1.35, 26 at 1.37),
  and the resulting `profile.yaml` onboards with the `ubicity` CLI. Run by hand,
  not in CI, because the input is 3.8 MB and fetched from upstream.

---

## Regeneration plan

1. **1.35 first.** Regenerate `io.kubernetes:1.35` and compare with the baseline
   (see [Testing](#testing)). The regenerated files replace the generated part of
   the published 1.35 profile; the hand-written post-processing is reapplied on
   top, following `GENERATING.md`.
2. **Then 1.37**, the current stable release, as a new profile version
   `io.kubernetes:1.37`. The split rules hold for both versions; only the counts
   differ.
