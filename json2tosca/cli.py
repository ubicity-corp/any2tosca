"""
cli.py
------
Command-line interface for json2tosca.

Usage
-----
    json2tosca <input.json> [<input2.json> ...] [-o output.yaml]
    json2tosca schema_dir/ [-o output.yaml]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .converter import build_tosca_document, convert_schema
from .yaml_utils import dump_tosca_yaml

__all__ = ["main"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="json2tosca",
        description="Convert JSON Schema files to TOSCA data types (YAML).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  json2tosca my_schema.json
  json2tosca schemas/ -o tosca_types.yaml
  json2tosca a.json b.json -o combined.yaml
""",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="INPUT",
        help="JSON Schema file(s) or a directory containing .json files.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="OUTPUT",
        help=(
            "Output YAML file path. "
            "Defaults to <input_stem>.tosca.yaml for a single file, "
            "or tosca_types.yaml when multiple files are provided."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print extra detail while processing.",
    )
    return parser.parse_args(argv)


def _collect_input_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            found = sorted(p.glob("*.json"))
            if not found:
                print(f"Warning: no .json files found in '{inp}'", file=sys.stderr)
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            print(f"Warning: '{inp}' is not a file or directory — skipping.", file=sys.stderr)
    return files


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns an exit code (0 = success)."""
    args = _parse_args(argv)
    input_files = _collect_input_files(args.inputs)

    if not input_files:
        print("Error: no JSON Schema files found.", file=sys.stderr)
        return 1

    all_data_types: dict = {}
    errors = 0

    for schema_path in input_files:
        if args.verbose:
            print(f"Processing: {schema_path}")
        else:
            print(f"Processing: {schema_path.name}")

        try:
            with open(schema_path, "r", encoding="utf-8") as fh:
                root_schema = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"  ✗ JSON parse error: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        top_name = root_schema.get("title") or schema_path.stem
        data_types = convert_schema(root_schema, top_name)

        if not data_types:
            print(f"  ⚠  No convertible types found.")
        else:
            print(f"  ✓  {len(data_types)} type(s): {', '.join(data_types)}")
            all_data_types.update(data_types)

    if not all_data_types:
        print("No data types were generated.", file=sys.stderr)
        return 1

    tosca_doc = build_tosca_document(all_data_types)
    yaml_output = dump_tosca_yaml(tosca_doc)

    # Determine output path
    if args.output:
        out_path = Path(args.output)
    elif len(input_files) == 1:
        out_path = input_files[0].with_suffix(".tosca.yaml")
    else:
        out_path = Path("tosca_types.yaml")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_output, encoding="utf-8")
    print(f"\n✅  TOSCA output written to: {out_path}")

    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2

    return 0


def cli_entry() -> None:
    """Thin wrapper so setuptools console_scripts gets a no-arg callable."""
    sys.exit(main())
