# Converting the Kubernetes specification

`oas2tosca --kubernetes` converts the Kubernetes OpenAPI specification
(`api/openapi-spec/swagger.json` in the Kubernetes repository, for the release
you want) into a TOSCA profile of Kubernetes resource types: one file per API
group, node types for the resources and data types for everything else.

```bash
oas2tosca --kubernetes swagger.json --profile io.kubernetes:1.37 -o io/kubernetes/1.37/
```

`--kubernetes` takes exactly one input file. It is the same as `oas2tosca` with:

- `--split` set to the packaged rules,
  [any2tosca/rules/kubernetes.yaml](../any2tosca/rules/kubernetes.yaml); an
  explicit `--split RULES` replaces them;
- `--allow-skipped-refs`;
- `--extension-metadata` for the four `x-kubernetes-*` keys below;
- the `value` relationship model, which it requires;
- the resource adjustments below, applied after the OpenAPI schemas are read.

## Files

The packaged rules ([splitting.md](splitting.md) describes rules) write:

| File | Schemas |
|---|---|
| `meta.yaml` | `io.k8s.apimachinery.pkg.apis.meta.v1` (`ObjectMeta`, `LabelSelector`, ...) |
| `resource.yaml` | `io.k8s.apimachinery.pkg.api.resource` (`Quantity`) |
| `runtime.yaml` | `io.k8s.apimachinery.pkg.runtime` (`RawExtension`) |
| `intstr.yaml` | `io.k8s.apimachinery.pkg.util.intstr` (`IntOrString`) |
| `<group>.yaml` | every GA API group: `io.k8s.api.<group>.v1` (`core.yaml`, `apps.yaml`, `batch.yaml`, ...) |
| `dra.yaml` | the `resource.k8s.io` group (`io.k8s.api.resource.v1`), named so it does not share `resource.yaml` |
| `autoscaling.yaml` | `autoscaling` `v2`, and `Scale` from `v1`; `v1`'s `HorizontalPodAutoscaler` is left out |
| `apiextensions.yaml` | `CustomResourceDefinition` |
| `apiregistration.yaml` | `APIService` |

Alpha and beta versions, the `*List` kinds and `io.k8s.apimachinery.pkg.version`
are left out. A type name is the last segment of the schema name
(`io.k8s.api.apps.v1.Deployment` → `Deployment`). With `--profile`, `profile.yaml`
imports every file, each after the files it imports.

A GA field whose type is an alpha or beta type falls back to its JSON type
(`map`, `string`, ...), with a warning naming the schema.

## Node types

A schema becomes a node type when the specification creates it with a `POST`
to a path that also supports `GET`: a resource collection. A schema posted only
to a path without `GET` is a request to perform an action and becomes a data
type: `Binding`, `Eviction` and `TokenRequest` (subresources), and the access
reviews. `Scale` is a data type too.

Each resource node type:

- carries its `x-kubernetes-group-version-kind` as metadata, as a map:

  ```yaml
  metadata:
    x-kubernetes-group-version-kind:
      group: apps          # '' for the core group
      kind: Deployment
      version: v1
  ```

  A schema the specification gives several group-version-kinds (`DeleteOptions`,
  `WatchEvent`) keeps them as a list;
- has no `apiVersion` or `kind` property, since the type determines both;
- declares `status` as an attribute, since Kubernetes sets it.

## Metadata from extensions

| Extension | On | Meaning |
|---|---|---|
| `x-kubernetes-group-version-kind` | node types | the resource's API group, version and kind |
| `x-kubernetes-list-type` | list properties | `atomic`, `set` or `map`: how the list merges |
| `x-kubernetes-list-map-keys` | list properties | the keys that identify an entry of a `map` list |
| `x-kubernetes-map-type` | map properties | `atomic` or `granular`: how the map merges |

Other `x-kubernetes-*` extensions are not copied.

## Resources inside other schemas

A schema that holds a resource as a value, such as a `StatefulSet`'s
`volumeClaimTemplates` (a list of `PersistentVolumeClaim`), refers to a
companion data type with the resource's properties, `<Kind>Value`
(`core:PersistentVolumeClaimValue`). It is written to the same file as the
resource.

## Values TOSCA cannot type

TOSCA has no union or `any` type. `IntOrString` derives from `string`, so its
fields take quoted numbers (`"8080"`). A schema that accepts any JSON value
derives from `string`. Integer formats (`int32`, `int64`) add no range check.

## A finished profile

The generated files hold the Kubernetes types as the specification defines
them. A profile that deploys resources adds its own types and lifecycle
operations on top, such as the base resource types, requirements between
resources and the scripts that apply them; the published `io.kubernetes`
profiles describe how they are generated and finished in their `GENERATING.md`.
