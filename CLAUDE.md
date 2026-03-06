# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/test_converter.py::test_convert_schema_top_level

# Run the CLI
any2tosca my_schema.json
any2tosca my_schema.json -o output.yaml
any2tosca schemas/ -o combined.yaml
```

## Architecture

This is a Python package that converts [JSON Schema](https://json-schema.org/) documents to [TOSCA](https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/) `data_types` (YAML).

**Core modules:**

- `any2tosca/converter.py` — all conversion logic: `convert_schema()` is the main entry point, `build_tosca_document()` wraps output in a TOSCA service-template skeleton. Handles `$ref` resolution (local only: `#/definitions/<name>` and `#/$defs/<name>`), constraint mapping, and type coercion.
- `any2tosca/cli.py` — `main()` drives the CLI: collects input files (individual JSON files or directories), merges all `data_types` dicts, writes YAML output. Entry point is `any2tosca.__main__:main` per `setup.cfg`.
- `any2tosca/yaml_utils.py` — wraps PyYAML's `yaml.dump` with a custom representer for literal block style on multi-line strings.

**Conversion flow:**

1. Parse JSON Schema file(s) with `json.load`
2. `convert_schema(root_schema, top_level_name)` returns a flat `dict[str, ToscaDataType]`:
   - Named definitions in `definitions`/`$defs` are processed first
   - Top-level schema is added if it has `type: object` or `properties`
3. `build_tosca_document(data_types)` wraps in `tosca_definitions_version: tosca_simple_yaml_1_3`
4. `dump_tosca_yaml()` serializes to YAML

**Key design decisions:**

- All `object` types become `derived_from: tosca.datatypes.Root`; scalar/enum definitions become `derived_from: <tosca_primitive_type>`
- JSON `number` maps to TOSCA `float`; JSON `null` falls back to `string`
- `sanitize_name()` replaces spaces, dashes, and dots with underscores in all type and property names
- External `$ref` URIs are silently ignored (return `{}`)
- Multiple input schemas are merged into a single output file; name collisions are resolved by last-write-wins

**`old/` directory** contains a previous implementation kept for reference; it is not imported or tested.
