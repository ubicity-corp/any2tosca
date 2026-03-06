"""
cli.py
------
Command-line entry points for json2tosca, redfish2tosca, and oas2tosca.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .converter import ConversionResult, Converter, build_tosca_document, convert_schema
from .openapi import OpenAPIExtractor
from .redfish import RedfishNormalizer
from .yaml_utils import dump_tosca_yaml

__all__ = ["main", "main_redfish", "main_oas"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_parser(prog: str, description: str, epilog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="INPUT",
        help="Input file(s) or a directory.",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="OUTPUT",
        help=(
            "Output YAML file. "
            "Defaults to <input_stem>.tosca.yaml for a single file, "
            "or tosca_types.yaml for multiple files."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print extra detail while processing.",
    )
    return parser


def _collect_input_files(inputs: list[str], extensions: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            found = sorted(f for ext in extensions for f in p.glob(f"*{ext}"))
            if not found:
                exts = ", ".join(extensions)
                print(f"Warning: no {exts} files found in '{inp}'", file=sys.stderr)
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            print(f"Warning: '{inp}' is not a file or directory — skipping.", file=sys.stderr)
    return files


def _load_file(path: Path) -> Any:
    """Load a JSON or YAML file into a Python object."""
    with open(path, "r", encoding="utf-8") as fh:
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(fh)
        return json.load(fh)


def _report(result: ConversionResult, verbose: bool, path: Path) -> None:
    label = str(path) if verbose else path.name
    print(f"Processing: {label}")
    total = len(result.data_types) + len(result.node_types)
    if not total:
        print("  ⚠  No convertible types found.")
    else:
        names = list(result.data_types) + list(result.node_types)
        print(f"  ✓  {total} type(s): {', '.join(names)}")


def _write_output(
    all_data_types: dict,
    all_node_types: dict,
    input_files: list[Path],
    args: argparse.Namespace,
) -> None:
    tosca_doc = build_tosca_document(all_data_types, all_node_types)
    yaml_output = dump_tosca_yaml(tosca_doc)

    if args.output:
        out_path = Path(args.output)
    elif len(input_files) == 1:
        out_path = input_files[0].with_suffix(".tosca.yaml")
    else:
        out_path = Path("tosca_types.yaml")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_output, encoding="utf-8")
    print(f"\n✅  TOSCA output written to: {out_path}")


# ── json2tosca ────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Convert plain JSON Schema files to TOSCA."""
    parser = _make_parser(
        "json2tosca",
        "Convert JSON Schema files to TOSCA data types (YAML).",
        "Examples:\n"
        "  json2tosca my_schema.json\n"
        "  json2tosca schemas/ -o tosca_types.yaml\n"
        "  json2tosca a.json b.json -o combined.yaml",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json",))

    if not input_files:
        print("Error: no JSON Schema files found.", file=sys.stderr)
        return 1

    all_data_types: dict = {}
    all_node_types: dict = {}
    errors = 0

    for schema_path in input_files:
        try:
            root_schema = _load_file(schema_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        top_name = root_schema.get("title") or schema_path.stem
        result = convert_schema(root_schema, top_name)
        _report(result, args.verbose, schema_path)
        all_data_types.update(result.data_types)
        all_node_types.update(result.node_types)

    if not all_data_types and not all_node_types:
        print("No types were generated.", file=sys.stderr)
        return 1

    _write_output(all_data_types, all_node_types, input_files, args)

    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── redfish2tosca ─────────────────────────────────────────────────────────────

def main_redfish(argv: list[str] | None = None) -> int:
    """Convert Redfish JSON Schema files to TOSCA."""
    parser = _make_parser(
        "redfish2tosca",
        "Convert Redfish JSON Schema files to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  redfish2tosca Drive.json\n"
        "  redfish2tosca schemas/ -o redfish.tosca.yaml",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json",))

    if not input_files:
        print("Error: no Redfish JSON Schema files found.", file=sys.stderr)
        return 1

    normalizer = RedfishNormalizer()
    converter = Converter()
    all_data_types: dict = {}
    all_node_types: dict = {}
    errors = 0

    for schema_path in input_files:
        try:
            redfish_schema = _load_file(schema_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        root_schema, node_type_names = normalizer.normalize(redfish_schema)
        top_name = redfish_schema.get("title") or schema_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names)
        _report(result, args.verbose, schema_path)
        all_data_types.update(result.data_types)
        all_node_types.update(result.node_types)

    if not all_data_types and not all_node_types:
        print("No types were generated.", file=sys.stderr)
        return 1

    _write_output(all_data_types, all_node_types, input_files, args)

    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── oas2tosca ─────────────────────────────────────────────────────────────────

def main_oas(argv: list[str] | None = None) -> int:
    """Convert OpenAPI documents to TOSCA."""
    parser = _make_parser(
        "oas2tosca",
        "Convert OpenAPI 2.0/3.x documents to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  oas2tosca api.yaml\n"
        "  oas2tosca api.json -o api.tosca.yaml\n"
        "  oas2tosca specs/ -o combined.tosca.yaml",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json", ".yaml", ".yml"))

    if not input_files:
        print("Error: no OpenAPI files found.", file=sys.stderr)
        return 1

    extractor = OpenAPIExtractor()
    converter = Converter()
    all_data_types: dict = {}
    all_node_types: dict = {}
    errors = 0

    for api_path in input_files:
        try:
            openapi_doc = _load_file(api_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        root_schema, node_type_names = extractor.extract(openapi_doc)
        top_name = (
            openapi_doc.get("info", {}).get("title")
            or api_path.stem
        )
        result = converter.convert_schema(root_schema, top_name, node_type_names)
        _report(result, args.verbose, api_path)
        all_data_types.update(result.data_types)
        all_node_types.update(result.node_types)

    if not all_data_types and not all_node_types:
        print("No types were generated.", file=sys.stderr)
        return 1

    _write_output(all_data_types, all_node_types, input_files, args)

    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── Entry point wrappers ──────────────────────────────────────────────────────

def cli_entry() -> None:
    sys.exit(main())

def redfish_entry() -> None:
    sys.exit(main_redfish())

def oas_entry() -> None:
    sys.exit(main_oas())
