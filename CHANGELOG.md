# Changes

What changed in each release, and what to do about it.

## Unreleased

- **Several TOSCA files from one input.** `--split RULES` divides the schemas of
  one input file among several TOSCA files by name, with imports between them;
  `--profile NAME:VERSION` also writes `profile.yaml` and `TOSCA.meta`. Available
  on all four commands. Output without these options is unchanged.
- **Relationship model.** `--relationship-model links|value` chooses how
  references to node types are converted. `links` is the existing behaviour:
  requirements, `feature` and `containable` capabilities, `<name>_caps.yaml`
  and the `Core.yaml` import. `value` treats a reference as an embedded value:
  a property whose schema is a node type's schema is typed with a companion
  data type `<Name>Value`, and none of the `links` additions are generated.
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
