# Multi-Output Conversion

**Status:** Design draft, not implemented.

Multi-output conversion turns **one input document** into **several TOSCA files**
that import each other, optionally packaged as a single TOSCA profile. It is the
mechanism by which `any2tosca` generates profiles whose types are organized into
files by a naming convention in the source, such as the Kubernetes profile
(`io.kubernetes:1.35`), whose files follow the Kubernetes API groups
(`meta.yaml`, `core.yaml`, `apps.yaml`, ...).

It is the first of the capabilities `any2tosca` needs before it can regenerate the
Kubernetes profile, at which point the separate `oas2tosca` repository is deleted.
See [Relation to the Kubernetes profile](#relation-to-the-kubernetes-profile).

---

## Concepts

| Term | Meaning |
|---|---|
| **Unit** | One output TOSCA file. A unit has a name (`apps`), an output file (`apps.yaml`) and a namespace (`apps`) under which other units import it. |
| **Split rules** | An ordered list of rules that assigns each schema of the input to a unit, gives it its TOSCA type name, or skips it. |
| **Unit graph** | Directed graph with an edge `A → B` whenever a schema in unit `A` references a schema in unit `B`. Becomes the import graph of the output. |

Without split rules, conversion behaves exactly as today: one input file, one
output file.

---

## Approach: split the schema, then convert each unit

The input is split **before** conversion. After normalization (OpenAPI, Redfish,
YANG, or plain JSON Schema) the input is a single root schema whose named
definitions live under `definitions`. The splitter:

1. Applies the split rules to every definition, producing a unit and a type name
   for each one (or skipping it).
2. Builds one in-memory JSON Schema document per unit, `<unit>.json`, holding that
   unit's definitions under their new type names.
3. Rewrites every `$ref` to a definition:
   - same unit: `#/definitions/<type name>`
   - other unit: `<unit>.json#/definitions/<type name>`
   - skipped definition: see [Skipped definitions](#skipped-definitions).
4. Builds the unit graph from the rewritten refs and checks it (see
   [Cycles](#cycles)).
5. Converts each unit with `Converter.convert_schema`, giving the converter all
   unit documents as an in-memory document set.

The converter then sees an ordinary multi-file corpus. Cross-unit refs are
external refs, so the existing machinery produces `namespace:Type` references and
the matching `imports` entries. No code that emits type references (`derived_from`,
`type`, `entry_schema`, `key_schema`, requirement targets) changes.

The alternative, converting the whole document once and distributing the emitted
types into files afterwards, would have to find and rewrite every type reference
in the emitted TOSCA and recompute imports, duplicating what the converter already
does per file. Splitting the schema keeps a single code path for cross-file
references.

---

## Split rules

### Format

```yaml
rules:
  - match: '^io\.k8s\.apimachinery\.pkg\.apis\.meta\.v1\.(?P<name>[^.]+)$'
    unit: meta
  - match: '^io\.k8s\.apimachinery\.pkg\.util\.intstr\.(?P<name>[^.]+)$'
    unit: intstr
  - match: '^io\.k8s\.api\.(?P<unit>[a-z]+)\.v1\.(?P<name>[^.]+)$'
  - match: '.*'
    skip: true
```

- Rules are tried in order against the **definition name**. The first match wins.
- `match` is a Python regular expression, matched against the whole name.
- The **unit** comes from the rule's `unit` value, or else from the `unit` named
  group of the match. A rule must provide one or the other.
- The **type name** comes from the `name` named group, or else is the sanitized
  full definition name.
- `namespace` (optional) sets the unit's import namespace. The default is the unit
  name. When several rules give the same unit, their namespaces must agree.
- `skip: true` drops the definition from the output.
- A definition that matches no rule goes into the unit named by the top-level
  `default_unit` key. If `default_unit` is absent, it is an error.

Several rules may target the same unit, which gathers several name prefixes into
one file.

Because a rule can skip, the rules file is also the **schema filter**: keeping
only `v1` is a matter of writing `v1` into the patterns. No separate filter option
exists.

### Checks

The splitter stops with an error, before converting anything, when:

- two definitions get the same type name in the same unit (for example a `v1` and
  a `v1beta1` `Deployment` both sent to `apps`). The message lists both source
  names.
- a unit name or namespace is not a valid TOSCA name after sanitizing.
- a unit name matches the converter's versioned-file pattern (`Base.vX_Y_Z`).
  Unit documents must not trigger the versioned-file handling in
  `_collect_no_import_files` and `_ref_to_tosca_type`.
- under the `links` [relationship model](kubernetes.md#relationship-model), a
  unit's namespace is `core`. That namespace imports `Core.yaml` under `links`,
  so the unit's import would overwrite it. Under `value`, `core` is free (the
  Kubernetes `core` group).

### Skipped definitions

A kept definition that references a skipped one is an error by default: the
output would reference a type that does not exist. The error lists every
reference. `--allow-skipped-refs` downgrades it to a warning, and the referencing
property falls back to the property's JSON type (`map` for an object), which is
the same fallback the converter uses for an unresolvable `$ref`.

---

## Cycles

TOSCA 2.0 forbids circular imports, so the unit graph must be acyclic.

The splitter computes the strongly connected components of the unit graph. A
component with more than one unit is a cycle. By default this is an error that
names the units in the cycle and, for each edge in it, one definition that
creates the edge. With `--merge-cycles`, the units of each such component are
merged into one unit, named after the first of them in rule order, and the graph
is rebuilt.

Since the unit graph is acyclic when conversion starts, no unit document refers
back to the unit being converted. The converter's existing cycle avoidance
(`docs/circular-import-algorithm.md`, the "no-import" inlining) therefore never
triggers for split output. That algorithm is built for Redfish file corpora, where
the files are given and the cycles cannot be removed at the source. Here the
splitter owns the file boundaries, so it removes cycles where they arise instead
of inlining types into several files.

---

## Converter changes

The converter reads external schemas from disk today. Four changes let it convert
a set of in-memory units.

1. **In-memory documents.** `convert_schema` takes an optional
   `documents: Mapping[str, dict]`, filename to parsed schema. `_resolve_fname`,
   `_load_cached_doc` and `_ref_fnames_for` consult it before the search
   directories. `resolve_ref` resolves external refs against it even when
   `base_path` is `None`. The documents persist across `convert_schema` calls,
   like the on-disk caches.
2. **Node types across units.** `_is_node_type_tosca` decides whether a
   namespaced type is a node type by asking whether it is the *root type* of its
   file, which assumes one resource per file. A unit holds many node types. The
   splitter therefore passes the node-type names of **every** unit
   (`node_types_by_file: Mapping[str, set[str]]`), and `_is_node_type_tosca`
   checks that first. Without this, a property that refers to a node type in
   another unit would become a data-typed property instead of a requirement.
3. **Namespaces from the rules.** `_namespace_for` derives a namespace from the
   filename (at most 8 characters, vowels contracted, so `networking` becomes
   `ntwrkng`). An optional `namespaces: Mapping[str, str]`, filename to namespace,
   takes precedence over the derivation.
4. **Output naming.** `_write_file` names the output after the input file. It
   takes the output stem explicitly instead, so each unit is written as
   `<unit>.yaml`.

`node_type_names` keeps its meaning (the node types of the document being
converted). For a unit it is that unit's share of the node types the normalizer
returned.

---

## Output layout

Without `--profile`, the output directory receives one `<unit>.yaml` per unit.
Units import each other with `url:` plus `namespace:`, as the converter does today
for multi-file corpora.

With `--profile NAME:VERSION`, the output is also a loadable profile:

- `profile.yaml`: `tosca_definitions_version: tosca_2_0`, `profile: NAME:VERSION`,
  and an `imports` list of every unit file in **topological order** of the unit
  graph (dependencies first).
- `TOSCA.meta`: `CSAR-Version: 2.0` and `Entry-Definitions: profile.yaml`.
- The unit files carry no `profile:` keyword of their own.

The output is one profile with one file per unit, which is the layout of the
published Kubernetes profile. A separate profile per unit is not offered. It would
need a profile name and version per unit, and an import by profile name between
sibling profiles that are always released together.

---

## Command line

The options apply to every CLI (`any2tosca`, `oas2tosca`, `redfish2tosca`,
`yang2tosca`), because the split works on the normalized root schema whatever the
input format:

| Option | Meaning |
|---|---|
| `--split RULES.yaml` | Enable multi-output with the given split rules. |
| `--merge-cycles` | Merge each import cycle into one unit instead of failing. |
| `--allow-skipped-refs` | Warn instead of failing on references to skipped definitions. |
| `--profile NAME:VERSION` | Also write `profile.yaml` and `TOSCA.meta`. |

`--split` takes exactly one input file. The split is defined over one document's
definitions, and splitting several inputs into the same set of units would mix
two unrelated mechanisms for cross-file references.

The Kubernetes support ships its rules as a package resource, so the Kubernetes
CLI preset (a separate design) can use them without the user writing a rules
file.

---

## Behaviour this design does not change, but which matters for Kubernetes

`convert_schema` applies post-processing to **every** node type, whatever the
input format:

- it imports `Core.yaml` under namespace `core`,
- it adds a type-specific `Containable` capability to every node type,
- it writes a companion `<unit>_caps.yaml` holding those capability types.

These come from the Redfish conversion model. Split output inherits them per
unit. The Kubernetes profile does not use them, so they must become options before
`any2tosca` can regenerate that profile. The option that controls them is the
[relationship model](kubernetes.md#relationship-model).

---

## Testing

- **Splitter:** rule order and first match; `unit` and `name` named groups;
  `default_unit`; `skip`; type-name collision; invalid and versioned-looking unit
  names; ref rewriting within and across units; references to skipped
  definitions, with and without `--allow-skipped-refs`.
- **Cycles:** an acyclic graph passes unchanged; a two-unit cycle fails with the
  edge-creating definitions named; `--merge-cycles` merges it and rebuilds the
  graph.
- **Converter:** conversion from in-memory documents with no files on disk;
  a property referencing a node type in another unit becomes a requirement;
  namespace overrides appear in both the `imports` entries and the type
  references.
- **End to end:** a fixture cut from the Kubernetes OpenAPI with a handful of
  schemas from `meta`, `intstr`, `core` and `apps`, plus a `v1beta1` duplicate and
  an unmatched schema. Golden output checks the unit files, their imports,
  `profile.yaml` import order and `TOSCA.meta`.

---

## Relation to the Kubernetes profile

The backlog entry *Kubernetes profile: finish v2.0 migration + `in_range` fix via
`any2tosca`* lists what `oas2tosca` does that `any2tosca` must reproduce. This
design covers two of the items:

| oas2tosca feature | Covered by |
|---|---|
| Schema names parsed into group, version, kind | Split rules (`unit` and `name` named groups) |
| One profile per API group, with imports between groups | Units, unit graph, `--profile` |
| Only `v1` schemas | Split rules (patterns plus `skip`) |
| Resources become node types (`x-kubernetes-group-version-kind`) | Not covered: Kubernetes normalizer |
| Group-version-kind and `x-kubernetes-list-*` written to metadata | Not covered: extension-to-metadata option |

Once all five rows are covered, together with the options for the Redfish
post-processing above, `any2tosca` produces in one step what
`GENERATING.md` Part 1, Steps 2 and 3, and Part 2, Step 1, produce today
(generation with `oas2tosca`, copying the per-group files into place, and the
v1.3 → v2.0 conversion). The remaining hand-written steps of Part 2 (inheritance,
capabilities and requirements, lifecycle wiring) are outside this design.

---

## Open questions

- **Unintended merges.** Unit names come from the rules, so two prefixes can map
  to the same unit by accident. The Kubernetes rules avoid one such case by naming
  the `resource.k8s.io` group `dra` (it would otherwise share `resource` with
  `io.k8s.apimachinery.pkg.api.resource`;
  see [Kubernetes conversion](kubernetes.md#split-rules)). The collision check
  catches clashing type names, not a merge of two prefixes that share none. A
  `--list-units` dry run that prints each unit with the prefixes feeding it would
  make such merges visible.
