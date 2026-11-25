# Changes

What changed in each release, and what to do about it.

## Unreleased

- **Several TOSCA files from one input.** `--split RULES` divides the schemas of
  one input file among several TOSCA files by name, with imports between them.
  Available on all four commands. Output without the option is unchanged.
- **Output as a profile.** `--profile NAME:VERSION` also writes `profile.yaml`,
  importing every file the run writes in dependency order, and `TOSCA.meta`,
  for any run: a split, a directory of schemas, or a single file. A warning
  names imported files the run did not write.
- **Relationship model.** `--relationship-model links|value` chooses how
  references to node types are converted. `links` is the existing behaviour:
  requirements, `feature` and `containable` capabilities, `<name>_caps.yaml`
  and the `Core.yaml` import. `value` treats a reference as an embedded value:
  a property whose schema is a node type's schema is typed with a companion
  data type `<Name>Value`, and none of the `links` additions are generated.
- **Extension metadata.** `--extension-metadata KEY` (repeatable) copies a
  schema key, typically a vendor extension such as `x-kubernetes-list-type`,
  into the `metadata` of the type or property it appears on. Values are kept
  as YAML lists and maps.
- **`readOnly` becomes an attribute.** A node-type property whose schema has
  `readOnly: true` is converted to a TOSCA attribute (without `required`)
  instead of a property, in every command. Data types are unchanged.
- **OpenAPI node types are resources.** A request body becomes a node type
  only when it is posted to a path that also supports `GET`. Bodies posted
  only to action paths (such as a Kubernetes `pods/{name}/binding`) become
  data types.
- **Kubernetes.** `oas2tosca --kubernetes` converts the Kubernetes OpenAPI
  specification into one file per GA API group, using packaged split rules,
  with `x-kubernetes-*` extensions as metadata, resource types without
  `apiVersion`/`kind` properties, and `status` as an attribute.
- **Property names keep the schema's spelling.** Property and attribute names
  are no longer sanitized: `cpu-count` stays `cpu-count` (it was `cpu_count`),
  and names such as `$ref` and `x-kubernetes-list-type` are kept. Type names
  are still sanitized. This changes YANG output for every leaf whose name
  contains `-`.
- **Untyped definitions derive from `string`.** A definition with no `type`
  and no structure (any JSON value) becomes a data type derived from `string`,
  as an untyped property already was, instead of an empty data type.
- **Redfish rules for Redfish only.** Versioned schema families (the latest
  `Name.vX_Y_Z` as the base type, one capability file per family), the
  `redfish.dmtf.org` file-root heuristic, and the `Links`/`Actions` expansion
  apply under `redfish2tosca` only. `any2tosca` on Redfish schema files now
  converts them as plain JSON Schema; use `redfish2tosca` for Redfish.
- **`--extension-metadata` works for Redfish.** The Redfish normalizer keeps
  `x-` keywords.
- **Python API: input formats and conventions.** `any2tosca.formats` holds the
  formats the commands use (`JSON_SCHEMA`, `OPENAPI`, `KUBERNETES`, `REDFISH`,
  `YANG`), each with its loader, normalizers and `Conventions`.
  `convert_schema(..., conventions=Conventions(relationship_model, redfish,
  extension_metadata))` converts with a format's conventions; without them it
  uses `links` and no Redfish rules.
- **`oas2tosca` now defaults to `value`.** Its output no longer has
  capabilities, requirements, `_caps.yaml` files or a `Core.yaml` import, and
  data types holding a node type's schema stay data types. Pass
  `--relationship-model links` for the previous output.
- **Fixed:** OpenAPI schema names containing dots (such as
  `io.k8s.api.apps.v1.Deployment`) produced no node types. They now do.

## 0.1.0

The first release published on PyPI, under the Apache 2.0 license. Four
commands convert schemas to TOSCA 2.0 node types and data types:
`any2tosca` (JSON Schema), `oas2tosca` (OpenAPI 2.0 and 3.x),
`redfish2tosca` (Redfish JSON Schema) and `yang2tosca` (YANG modules).
