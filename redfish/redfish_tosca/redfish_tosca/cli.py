import argparse
from pathlib import Path
from importlib.resources import files

from .generator import generate

def default_meta_schema() -> Path:
    return files("redfish_tosca").joinpath("meta_schema.json")

def main():
    parser = argparse.ArgumentParser(
        description="Convert Redfish JSON Schemas to TOSCA Node Types"
    )
    parser.add_argument("schema_dir")
    parser.add_argument("output_dir")
    parser.add_argument("--meta-schema", dest="meta_schema", required=False)

    args = parser.parse_args()

    schema_dir = Path(args.schema_dir)
    output_dir = Path(args.output_dir)
    meta_schema = Path(args.meta_schema) if args.meta_schema else default_meta_schema()

    generate(schema_dir, meta_schema, output_dir)