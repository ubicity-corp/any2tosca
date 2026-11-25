"""
cli.py
------
Command-line entry points for any2tosca, redfish2tosca, and oas2tosca.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from .converter import ConversionResult, Converter, build_caps_document, build_tosca_document, convert_schema
from .openapi import OpenAPIExtractor
from .redfish import RedfishNormalizer
from .yang import YANGNormalizer
from .yaml_utils import dump_tosca_yaml

__all__ = ["main_json", "main_redfish", "main_oas", "main_yang"]


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
        metavar="OUTPUT_DIR",
        help="Output directory for TOSCA files (default: tosca/).",
    )
    parser.add_argument(
        "-I", "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Additional directory to search for referenced schema files "
             "(may be given multiple times).",
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


def _write_file(result: ConversionResult, stem: str, out_dir: Path) -> Path:
    """Write a single ConversionResult to <out_dir>/<stem>.yaml (and companion _caps.yaml)."""
    tosca_doc = build_tosca_document(
        result.data_types,
        result.node_types,
        result.relationship_types or None,
        result.interface_types or None,
        result.imports or None,
    )
    out_path = out_dir / f"{stem}.yaml"
    out_path.write_text(dump_tosca_yaml(tosca_doc), encoding="utf-8")

    if result.capability_types:
        # Strip version suffix (e.g. .v1_14_0) to get the base name
        base = re.sub(r"[._]v\d+(?:[._]\d+)*$", "", stem) or stem
        caps_path = out_dir / f"{base}_caps.yaml"
        caps_doc = build_caps_document(result.capability_types)
        caps_path.write_text(dump_tosca_yaml(caps_doc), encoding="utf-8")

    return out_path


# ── any2tosca ────────────────────────────────────────────────────────────────

def main_json(argv: list[str] | None = None) -> int:
    """Convert plain JSON Schema files to TOSCA."""
    parser = _make_parser(
        "any2tosca",
        "Convert JSON Schema files to TOSCA data types (YAML).",
        "Examples:\n"
        "  any2tosca my_schema.json\n"
        "  any2tosca schemas/ -o output/\n"
        "  any2tosca a.json b.json -o output/",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json",))

    if not input_files:
        print("Error: no JSON Schema files found.", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else Path("tosca")
    out_dir.mkdir(parents=True, exist_ok=True)
    search_paths = [Path(d) for d in args.include_dir]
    written = errors = 0

    for schema_path in input_files:
        try:
            root_schema = _load_file(schema_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error in {schema_path.name}: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        top_name = root_schema.get("title") or schema_path.stem
        result = convert_schema(root_schema, top_name, base_path=schema_path.parent,
                                search_paths=search_paths, source_path=schema_path)
        _report(result, args.verbose, schema_path)
        if not result.data_types and not result.node_types:
            continue
        out_path = _write_file(result, schema_path.stem, out_dir)
        print(f"  → {out_path}")
        written += 1

    if written == 0:
        print("No TOSCA files were written.", file=sys.stderr)
        return 1
    print(f"\n✅  {written} file(s) written to: {out_dir}/")
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
        "  redfish2tosca schemas/ -o tosca/",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json",))

    if not input_files:
        print("Error: no Redfish JSON Schema files found.", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else Path("tosca")
    out_dir.mkdir(parents=True, exist_ok=True)
    normalizer = RedfishNormalizer()
    converter = Converter()
    search_paths = [Path(d) for d in args.include_dir]
    written = errors = 0

    for schema_path in input_files:
        try:
            redfish_schema = _load_file(schema_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error in {schema_path.name}: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        root_schema, node_type_names = normalizer.normalize(redfish_schema)
        top_name = next(iter(node_type_names), None) or schema_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=schema_path.parent,
                                          search_paths=search_paths,
                                          source_path=schema_path)
        _report(result, args.verbose, schema_path)
        if not result.data_types and not result.node_types:
            continue
        out_path = _write_file(result, schema_path.stem, out_dir)
        print(f"  → {out_path}")
        written += 1

    if written == 0:
        print("No TOSCA files were written.", file=sys.stderr)
        return 1
    print(f"\n✅  {written} file(s) written to: {out_dir}/")
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
        "  oas2tosca api.json -o output/\n"
        "  oas2tosca specs/ -o output/",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".json", ".yaml", ".yml"))

    if not input_files:
        print("Error: no OpenAPI files found.", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else Path("tosca")
    out_dir.mkdir(parents=True, exist_ok=True)
    extractor = OpenAPIExtractor()
    converter = Converter()
    search_paths = [Path(d) for d in args.include_dir]
    written = errors = 0

    for api_path in input_files:
        try:
            openapi_doc = _load_file(api_path)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            print(f"  ✗ Parse error in {api_path.name}: {exc}", file=sys.stderr)
            errors += 1
            continue
        except OSError as exc:
            print(f"  ✗ File read error: {exc}", file=sys.stderr)
            errors += 1
            continue

        root_schema, node_type_names = extractor.extract(openapi_doc)
        top_name = openapi_doc.get("info", {}).get("title") or api_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=api_path.parent,
                                          search_paths=search_paths,
                                          source_path=api_path)
        _report(result, args.verbose, api_path)
        if not result.data_types and not result.node_types:
            continue
        out_path = _write_file(result, api_path.stem, out_dir)
        print(f"  → {out_path}")
        written += 1

    if written == 0:
        print("No TOSCA files were written.", file=sys.stderr)
        return 1
    print(f"\n✅  {written} file(s) written to: {out_dir}/")
    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── yang2tosca ────────────────────────────────────────────────────────────────

def main_yang(argv: list[str] | None = None) -> int:
    """Convert YANG module files to TOSCA."""
    parser = _make_parser(
        "yang2tosca",
        "Convert YANG module files to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  yang2tosca server.yang\n"
        "  yang2tosca models/ -o tosca/",
    )
    args = parser.parse_args(argv)
    input_files = _collect_input_files(args.inputs, (".yang",))

    if not input_files:
        print("Error: no YANG files found.", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else Path("tosca")
    out_dir.mkdir(parents=True, exist_ok=True)
    normalizer = YANGNormalizer()
    converter = Converter()
    search_paths = [Path(d) for d in args.include_dir]
    written = errors = 0

    for yang_path in input_files:
        try:
            root_schema, node_type_names = normalizer.normalize(yang_path)
        except Exception as exc:
            print(f"  ✗ Parse error in {yang_path.name}: {exc}", file=sys.stderr)
            errors += 1
            continue

        top_name = normalizer.module_name or yang_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=yang_path.parent,
                                          search_paths=search_paths,
                                          source_path=yang_path)
        _report(result, args.verbose, yang_path)
        if not result.data_types and not result.node_types:
            continue
        out_path = _write_file(result, yang_path.stem, out_dir)
        print(f"  → {out_path}")
        written += 1

    if written == 0:
        print("No TOSCA files were written.", file=sys.stderr)
        return 1
    print(f"\n✅  {written} file(s) written to: {out_dir}/")
    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── Entry point wrappers ──────────────────────────────────────────────────────

def cli_entry() -> None:
    sys.exit(main_json())

def redfish_entry() -> None:
    sys.exit(main_redfish())

def oas_entry() -> None:
    sys.exit(main_oas())

def yang_entry() -> None:
    sys.exit(main_yang())
