# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode, in an active virtual environment
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/test_converter.py::test_convert_schema_top_level

# Run the CLIs
any2tosca my_schema.json
any2tosca my_schema.json -o output_dir/
any2tosca schemas/ -o output_dir/ -I /extra/schema/search/dir

redfish2tosca redfish_schema.json -o output_dir/
oas2tosca openapi.yaml -o output_dir/
oas2tosca --kubernetes swagger.json --profile io.kubernetes:1.35 -o io/kubernetes/1.35/
yang2tosca model.yang -o output_dir/
```

## Architecture

This is a Python package that converts JSON Schema documents (and Redfish, OpenAPI, YANG variants) to TOSCA 2.0 `data_types` and `node_types` (YAML).

**Core modules:**

- `any2tosca/converter.py` — all conversion logic. `Converter` class is the main workhorse; `convert_schema()` and `build_tosca_document()` are module-level convenience wrappers.
- `any2tosca/cli.py` — one pipeline, `run(fmt, args)`, for every format: collect input files, load and normalize, by-value pre-pass, split or convert, write. The four entry points (`main_json`, `main_redfish`, `main_oas`, `main_yang`) pick a format; `oas2tosca --kubernetes` picks `KUBERNETES`.
- `any2tosca/formats.py` — `InputFormat` (extensions, loader, chain of normalizers returning `Normalized(root_schema, node_type_names, top_level_name)`, `Conventions`, packaged split rules) and the five formats `JSON_SCHEMA`, `REDFISH`, `OPENAPI`, `KUBERNETES`, `YANG`. A new format is a new `InputFormat`, not a new pipeline. Design: `docs/generalization.md`.
- `any2tosca/redfish.py` — `RedfishNormalizer`: strips OData properties, promotes `x-longDescription`, identifies node type names from Redfish resource schemas.
- `any2tosca/openapi.py` — `OpenAPIExtractor`: hoists `components/schemas` (OAS 3.x) or `definitions` (Swagger 2.x) into a root JSON Schema, identifies POST request bodies as node types when the same path also supports GET (POST-only paths are actions → data types).
- `any2tosca/yang.py` — `YANGNormalizer`: converts YANG `.yang` files to a JSON-Schema-like dict via `pyang`.
- `any2tosca/split.py` — `split_schema()`: divides one normalized root schema into units (one TOSCA file each) by regex split rules, rewrites `$ref`s within/across units, rejects or merges import cycles. Used by the `--split` CLI option; units are converted with `Converter.convert_schema(..., documents=, namespaces=, node_types_by_file=)`. Design: `docs/multi-output.md`.
- `any2tosca/companions.py` — `add_value_companions()`: the by-value pre-pass for the `value` relationship model. Each `$ref` to a node-type schema (except a node type's own `allOf` base) is pointed at a `<Name>Value` copy that converts to a data type. With `--split`, definitions the rules skip are ignored.
- `any2tosca/kubernetes.py` — `KubernetesNormalizer` (runs after `OpenAPIExtractor` under `oas2tosca --kubernetes`): single-entry group-version-kind list → map, drops `apiVersion`/`kind` from node types, marks `status` readOnly. Also `EXTENSION_METADATA` and `RULES_PATH` (packaged split rules `any2tosca/rules/kubernetes.yaml`, shipped as package data).
- `any2tosca/yaml_utils.py` — wraps PyYAML `yaml.dump` with a custom representer for folded-style multi-line strings.

**Conversion flow:**

1. Parse input file(s).
2. Optionally normalize (Redfish / OpenAPI / YANG) → root JSON Schema dict + set of node-type names.
3. `Converter.convert_schema(root_schema, top_level_name, node_type_names, base_path, search_paths)`:
   - Named definitions in `definitions`/`$defs` are processed first.
   - Top-level schema is added if it has `type: object` or `properties`.
   - Returns `ConversionResult(data_types, node_types, imports)`.
4. `build_tosca_document(data_types, node_types, imports)` wraps in `tosca_definitions_version: tosca_2_0`.
5. Output is written as one YAML file per input.

**Key design decisions:**

- TOSCA version: **tosca_2_0**. Imports use `url` + `namespace` keywords.
- Object schemas without an explicit parent: no `derived_from` added. Scalar/enum definitions: `derived_from: <tosca_primitive_type>`.
- JSON `number` → TOSCA `float`; JSON `null` → `string`.
- `sanitize_name()` replaces spaces, dashes, and dots with underscores in **type** names. Property and attribute names are kept as the schema spells them (TOSCA prefers kebab case).
- A definition with no `type`, no properties and no structure (`$ref`, `items`, `additionalProperties`, composition) derives from `string`, as an untyped property does.
- External `$ref` resolution: local `#/...` refs resolved against root schema; relative file refs and HTTP URI refs resolved via a `referencing.Registry` built from all `.json` files in `base_path` and `-I` search dirs, with filename-based fallback.
- When `allOf`/`anyOf`/`oneOf` merges properties from an external file, local `#/definitions/TypeName` refs inside those properties are rewritten to `ExternalFile.json#/definitions/TypeName` so the TOSCA output uses namespaced type references and records the correct import.
- `anyOf`/`oneOf` with versioned refs (`BaseName.vX_Y_Z`, Redfish rules only): latest versioned ref → `derived_from`; unversioned refs' properties are merged in.
- External `$ref` type names become `namespace:TypeName`; the namespace is derived from the filename (≤8 chars, lowercase, PascalCase split, vowel-contracted).
- Conventions (`convert_schema(conventions=Conventions(relationship_model, redfish, extension_metadata))`; each `InputFormat` carries its own). Relationship model, CLI `--relationship-model`: `links` (requirements, Feature/Containable caps, `_caps.yaml`, `Core.yaml`; default for redfish2tosca) or `value` (none of those; by-value companions; default for any2tosca, oas2tosca, yang2tosca). Design: `docs/kubernetes.md`.
- `redfish=True` (the `REDFISH` format only) enables the Redfish corpus rules: versioned file families (every `_VER_RE` use goes through `Converter._versioned`), version-stripped `_caps.yaml` names, the `redfish.dmtf.org` file-root heuristic, and `Links`/`Actions` expansion. Off for every other format; tests that exercise them pass `Conventions(redfish=True)`.
- Extension metadata (`Conventions.extension_metadata`, CLI `--extension-metadata KEY`; normalizers keep `x-` keys): listed keys are copied into TOSCA `metadata` from a definition's own schema (not its `allOf` bases) and from a property's own schema (not from the schema it `$ref`s).
- `readOnly: true` on a node-type property → TOSCA attribute (no `required` key), placed after `properties`. Data types keep readOnly properties as properties.
- Each input file produces its own `.yaml` output (no merging across files), unless `--split` divides one input into several files.

**`old/` directory** contains a previous implementation kept for reference; it is not imported or tested.
