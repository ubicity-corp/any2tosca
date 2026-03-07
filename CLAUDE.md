# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode (use the project venv)
source env/bin/activate
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
yang2tosca model.yang -o output_dir/
```

## Architecture

This is a Python package that converts JSON Schema documents (and Redfish, OpenAPI, YANG variants) to TOSCA 2.0 `data_types` and `node_types` (YAML).

**Core modules:**

- `any2tosca/converter.py` — all conversion logic. `Converter` class is the main workhorse; `convert_schema()` and `build_tosca_document()` are module-level convenience wrappers.
- `any2tosca/cli.py` — four entry points (`main_json`, `main_redfish`, `main_oas`, `main_yang`) for the four CLI tools. Each collects input files, runs conversion, and writes one `.yaml` per input file.
- `any2tosca/redfish.py` — `RedfishNormalizer`: strips OData properties, promotes `x-longDescription`, identifies node type names from Redfish resource schemas.
- `any2tosca/openapi.py` — `OpenAPIExtractor`: hoists `components/schemas` (OAS 3.x) or `definitions` (Swagger 2.x) into a root JSON Schema, identifies POST request bodies as node types.
- `any2tosca/yang.py` — `YANGNormalizer`: converts YANG `.yang` files to a JSON-Schema-like dict via `pyang`.
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
- `sanitize_name()` replaces spaces, dashes, and dots with underscores.
- External `$ref` resolution: local `#/...` refs resolved against root schema; relative file refs and HTTP URI refs resolved via a `referencing.Registry` built from all `.json` files in `base_path` and `-I` search dirs, with filename-based fallback.
- When `allOf`/`anyOf`/`oneOf` merges properties from an external file, local `#/definitions/TypeName` refs inside those properties are rewritten to `ExternalFile.json#/definitions/TypeName` so the TOSCA output uses namespaced type references and records the correct import.
- `anyOf`/`oneOf` with versioned refs (`BaseName.vX_Y_Z`): latest versioned ref → `derived_from`; unversioned refs' properties are merged in.
- External `$ref` type names become `namespace:TypeName`; the namespace is derived from the filename (≤8 chars, lowercase, PascalCase split, vowel-contracted).
- Each input file produces its own `.yaml` output (no merging across files).

**`old/` directory** contains a previous implementation kept for reference; it is not imported or tested.
