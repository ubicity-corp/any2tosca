# Redfish → TOSCA Generator

Converts Redfish JSON Schemas into TOSCA node types.

## Install

pip install -e .

## CLI

redfish-tosca <schema_dir> <output_dir>

Optional:
--meta-schema <file>

If omitted, bundled meta_schema.json is used.