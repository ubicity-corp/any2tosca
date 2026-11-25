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

from .companions import CompanionError, add_value_companions
from .kubernetes import EXTENSION_METADATA, RULES_PATH, KubernetesNormalizer
from .converter import ConversionResult, Converter, build_caps_document, build_tosca_document, convert_schema
from .openapi import OpenAPIExtractor
from .redfish import RedfishNormalizer
from .split import SplitError, load_rules, skipped_definitions, split_schema
from .yang import YANGNormalizer
from .yaml_utils import FoldedStr, dump_tosca_yaml

__all__ = ["main_json", "main_redfish", "main_oas", "main_yang"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_parser(
    prog: str, description: str, epilog: str, default_model: str = "links"
) -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--relationship-model",
        choices=Converter.RELATIONSHIP_MODELS,
        default=default_model,
        help="How references to node types are converted: 'links' makes them "
             "requirements and adds the Core.yaml capabilities; 'value' treats them "
             f"as embedded values (default: {default_model}).",
    )
    parser.add_argument(
        "--extension-metadata",
        action="append",
        default=[],
        metavar="KEY",
        help="Copy schema key KEY (typically a vendor extension, x-...) into the TOSCA "
             "metadata of the type or property it appears on; repeatable.",
    )
    split = parser.add_argument_group("multi-output (one input, several TOSCA files)")
    split.add_argument(
        "--split",
        metavar="RULES",
        help="Split the input into several TOSCA files using the split rules in RULES "
             "(a YAML file). Takes exactly one input file.",
    )
    split.add_argument(
        "--merge-cycles",
        action="store_true",
        help="Merge files that would import each other in a cycle instead of failing.",
    )
    split.add_argument(
        "--allow-skipped-refs",
        action="store_true",
        help="Warn instead of failing when a converted type references a skipped one.",
    )
    split.add_argument(
        "--profile",
        metavar="NAME:VERSION",
        help="Also write profile.yaml and TOSCA.meta, packaging the files as profile "
             "NAME:VERSION. Requires --split.",
    )
    return parser


def _by_value(
    root_schema: dict[str, Any],
    node_type_names: Any,
    args: argparse.Namespace,
    ignore: Any = (),
) -> dict[str, Any] | None:
    """Apply the by-value pre-pass under the 'value' model; None (after reporting) on error."""
    if args.relationship_model != "value":
        return root_schema
    try:
        return add_value_companions(root_schema, node_type_names, ignore)
    except CompanionError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return None


def _split_args_error(args: argparse.Namespace, input_files: list[Path]) -> str | None:
    """Return an error message if the multi-output options are used incorrectly."""
    if args.profile and not args.split:
        return "--profile requires --split."
    if args.profile and ":" not in args.profile:
        return f"--profile takes NAME:VERSION, not '{args.profile}'."
    if (args.merge_cycles or args.allow_skipped_refs) and not args.split:
        return "--merge-cycles and --allow-skipped-refs require --split."
    if args.split and len(input_files) != 1:
        return f"--split takes exactly one input file, not {len(input_files)}."
    return None


def _write_profile(names: list[str], profile: str, out_dir: Path) -> None:
    """Write profile.yaml importing *names* (in order) and a TOSCA.meta pointing at it."""
    doc = {
        "tosca_definitions_version": "tosca_2_0",
        "profile": profile,
        "description": FoldedStr("Profile generated by any2tosca"),
        "imports": names,
    }
    (out_dir / "profile.yaml").write_text(dump_tosca_yaml(doc), encoding="utf-8")
    (out_dir / "TOSCA.meta").write_text(
        "CSAR-Version: 2.0\nCreated-By: any2tosca\nEntry-Definitions: profile.yaml\n",
        encoding="utf-8",
    )


def _run_split(
    root_schema: dict[str, Any],
    node_type_names: Any,
    args: argparse.Namespace,
    input_path: Path,
    search_paths: list[Path],
    out_dir: Path,
) -> int:
    """Split *root_schema* into units, convert each, and write one file per unit."""
    try:
        rules = load_rules(Path(args.split))
        root_schema = _by_value(
            root_schema, node_type_names, args, skipped_definitions(root_schema, rules)
        )
        if root_schema is None:
            return 1
        split = split_schema(
            root_schema, rules, node_type_names,
            merge_cycles=args.merge_cycles,
            allow_skipped_refs=args.allow_skipped_refs,
            # Under the Redfish conventions the converter imports Core.yaml as 'core'.
            reserved_namespaces={"core"} if args.relationship_model == "links" else (),
        )
    except SplitError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for warning in split.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    converter = Converter()
    for unit in split.units:
        result = converter.convert_schema(
            unit.document, unit.name, unit.node_type_names,
            base_path=input_path.parent,
            search_paths=search_paths,
            source_path=Path(unit.filename),
            documents=split.documents,
            namespaces=split.namespaces,
            node_types_by_file=split.node_types_by_file,
            relationship_model=args.relationship_model,
            extension_metadata=args.extension_metadata,
        )
        _report(result, args.verbose, Path(unit.tosca_file))
        out_path = _write_file(result, unit.name, out_dir)
        print(f"  → {out_path}")
    if args.profile:
        _write_profile([u.tosca_file for u in split.units], args.profile, out_dir)
        print(f"  → {out_dir / 'profile.yaml'}")
    print(f"\n✅  {len(split.units)} file(s) written to: {out_dir}/")
    return 0


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

    error = _split_args_error(args, input_files)
    if error:
        print(f"Error: {error}", file=sys.stderr)
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

        if args.split:
            return _run_split(root_schema, frozenset(), args, schema_path, search_paths, out_dir)
        top_name = root_schema.get("title") or schema_path.stem
        result = convert_schema(root_schema, top_name, base_path=schema_path.parent,
                                search_paths=search_paths, source_path=schema_path,
                                relationship_model=args.relationship_model,
                                extension_metadata=args.extension_metadata)
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

    error = _split_args_error(args, input_files)
    if error:
        print(f"Error: {error}", file=sys.stderr)
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
        if args.split:
            return _run_split(root_schema, node_type_names, args, schema_path, search_paths, out_dir)
        root_schema = _by_value(root_schema, node_type_names, args)
        if root_schema is None:
            errors += 1
            continue
        top_name = next(iter(node_type_names), None) or schema_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=schema_path.parent,
                                          search_paths=search_paths,
                                          source_path=schema_path,
                                          relationship_model=args.relationship_model,
                                          extension_metadata=args.extension_metadata)
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
        default_model="value",
    )
    parser.add_argument(
        "--kubernetes",
        action="store_true",
        help="Convert the Kubernetes OpenAPI specification: one file per GA API group "
             "(packaged split rules, unless --split is given), the x-kubernetes-* "
             "extensions as metadata, and resource types without apiVersion/kind and "
             "with status as an attribute. Takes exactly one input file.",
    )
    args = parser.parse_args(argv)
    if args.kubernetes:
        if args.relationship_model != "value":
            print("Error: --kubernetes requires the value relationship model.", file=sys.stderr)
            return 1
        args.split = args.split or str(RULES_PATH)
        args.extension_metadata = list(dict.fromkeys([*EXTENSION_METADATA, *args.extension_metadata]))
    input_files = _collect_input_files(args.inputs, (".json", ".yaml", ".yml"))

    if not input_files:
        print("Error: no OpenAPI files found.", file=sys.stderr)
        return 1

    error = _split_args_error(args, input_files)
    if error:
        print(f"Error: {error}", file=sys.stderr)
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
        if args.kubernetes:
            root_schema, node_type_names = KubernetesNormalizer().normalize(
                root_schema, node_type_names
            )
        if args.split:
            return _run_split(root_schema, node_type_names, args, api_path, search_paths, out_dir)
        root_schema = _by_value(root_schema, node_type_names, args)
        if root_schema is None:
            errors += 1
            continue
        top_name = openapi_doc.get("info", {}).get("title") or api_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=api_path.parent,
                                          search_paths=search_paths,
                                          source_path=api_path,
                                          relationship_model=args.relationship_model,
                                          extension_metadata=args.extension_metadata)
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

    error = _split_args_error(args, input_files)
    if error:
        print(f"Error: {error}", file=sys.stderr)
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

        if args.split:
            return _run_split(root_schema, node_type_names, args, yang_path, search_paths, out_dir)
        root_schema = _by_value(root_schema, node_type_names, args)
        if root_schema is None:
            errors += 1
            continue
        top_name = normalizer.module_name or yang_path.stem
        result = converter.convert_schema(root_schema, top_name, node_type_names,
                                          base_path=yang_path.parent,
                                          search_paths=search_paths,
                                          source_path=yang_path,
                                          relationship_model=args.relationship_model,
                                          extension_metadata=args.extension_metadata)
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
