"""
cli.py
------
Command-line entry points: any2tosca, redfish2tosca, oas2tosca and yang2tosca.

Every command runs one pipeline (``run``) for its input format
(``formats.py``): collect the input files, load and normalize each to a root
JSON Schema, apply the by-value pre-pass under the ``value`` relationship
model, then either split it into units or convert it whole, and write the
TOSCA files.
"""

from __future__ import annotations

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

import argparse
import sys
from pathlib import Path
from typing import Any

from .companions import CompanionError, add_value_companions
from .converter import ConversionResult, Conventions, Converter, build_caps_document, build_tosca_document
from .formats import JSON_SCHEMA, KUBERNETES, OPENAPI, REDFISH, YANG, InputFormat, Normalized
from .split import SplitError, load_rules, skipped_definitions, split_schema
from .yaml_utils import FoldedStr, dump_tosca_yaml

__all__ = ["main_json", "main_redfish", "main_oas", "main_yang", "run"]


# ── Options ───────────────────────────────────────────────────────────────────

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
    split = parser.add_argument_group("multi-output (several TOSCA files, packaged as a profile)")
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
        help="Also write profile.yaml, importing every file written, and TOSCA.meta, "
             "packaging the output directory as profile NAME:VERSION.",
    )
    return parser


def _apply_format_defaults(fmt: InputFormat, args: argparse.Namespace) -> str | None:
    """Merge the format's defaults into *args*; return an error message, or None."""
    if fmt.required_relationship_model and args.relationship_model != fmt.required_relationship_model:
        return (f"the {fmt.name} format requires the "
                f"{fmt.required_relationship_model} relationship model.")
    if fmt.split_rules and not args.split:
        args.split = str(fmt.split_rules)
    if fmt.conventions.extension_metadata:
        args.extension_metadata = list(dict.fromkeys(
            [*fmt.conventions.extension_metadata, *args.extension_metadata]))
    return None


def _conventions(fmt: InputFormat, args: argparse.Namespace) -> Conventions:
    """The format's conventions with the relationship model and extension keys of *args*."""
    return Conventions(
        relationship_model=args.relationship_model,
        redfish=fmt.conventions.redfish,
        extension_metadata=tuple(args.extension_metadata),
    )


def _split_args_error(args: argparse.Namespace, input_files: list[Path]) -> str | None:
    """Return an error message if the multi-output options are used incorrectly."""
    if args.profile and ":" not in args.profile:
        return f"--profile takes NAME:VERSION, not '{args.profile}'."
    if (args.merge_cycles or args.allow_skipped_refs) and not args.split:
        return "--merge-cycles and --allow-skipped-refs require --split."
    if args.split and len(input_files) != 1:
        return f"--split takes exactly one input file, not {len(input_files)}."
    return None


# ── Pipeline steps ────────────────────────────────────────────────────────────

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


def _load_and_normalize(fmt: InputFormat, path: Path) -> Normalized | None:
    """Load and normalize one input file; None (after reporting) on error."""
    try:
        return fmt.normalize(fmt.load(path), path)
    except OSError as exc:
        print(f"  ✗ File read error: {exc}", file=sys.stderr)
    except fmt.parse_errors as exc:
        print(f"  ✗ Parse error in {path.name}: {exc}", file=sys.stderr)
    return None


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
        caps_path = out_dir / (result.caps_file or f"{stem}_caps.yaml")
        caps_doc = build_caps_document(result.capability_types)
        caps_path.write_text(dump_tosca_yaml(caps_doc), encoding="utf-8")

    return out_path


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


def _import_order(written: dict[str, list[str]]) -> list[str]:
    """The written files, each after the written files it imports.

    *written* maps each written file to the files it imports, in the order the
    files were written; that order breaks ties.  Imports of files the run did
    not write do not constrain the order.
    """
    deps = {f: {i for i in imports if i in written and i != f} for f, imports in written.items()}
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(written):
        ready = [f for f in written if f not in done and deps[f] <= done]
        if not ready:
            # A cycle (the converter avoids them): keep the written order for the rest.
            ready = [f for f in written if f not in done]
        ordered.extend(ready)
        done.update(ready)
    return ordered


def _missing_imports(written: dict[str, list[str]], out_dir: Path) -> list[str]:
    """Files the written files import that this run did not write and are not in *out_dir*."""
    produced = set(written)
    return sorted({i for imports in written.values() for i in imports
                   if i not in produced and not (out_dir / i).exists()})


def _run_split(
    normalized: Normalized,
    conventions: Conventions,
    args: argparse.Namespace,
    input_path: Path,
    search_paths: list[Path],
    out_dir: Path,
) -> int:
    """Split one normalized input into units, convert each, and write one file per unit."""
    node_type_names = normalized.node_type_names
    try:
        rules = load_rules(Path(args.split))
        root_schema = _by_value(
            normalized.root_schema, node_type_names, args,
            skipped_definitions(normalized.root_schema, rules),
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
            conventions=conventions,
        )
        _report(result, args.verbose, Path(unit.tosca_file))
        out_path = _write_file(result, unit.name, out_dir)
        print(f"  → {out_path}")
    if args.profile:
        _write_profile([u.tosca_file for u in split.units], args.profile, out_dir)
        print(f"  → {out_dir / 'profile.yaml'}")
    print(f"\n✅  {len(split.units)} file(s) written to: {out_dir}/")
    return 0


# ── The pipeline ──────────────────────────────────────────────────────────────

def run(fmt: InputFormat, args: argparse.Namespace) -> int:
    """Convert the inputs named in *args* as format *fmt*; return the exit code."""
    error = _apply_format_defaults(fmt, args)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    input_files = _collect_input_files(args.inputs, fmt.extensions)
    if not input_files:
        print(f"Error: no {fmt.label} files found.", file=sys.stderr)
        return 1

    error = _split_args_error(args, input_files)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else Path("tosca")
    out_dir.mkdir(parents=True, exist_ok=True)
    conventions = _conventions(fmt, args)
    converter = Converter()
    search_paths = [Path(d) for d in args.include_dir]
    written = errors = 0
    profile_files: dict[str, list[str]] = {}   # written file → files it imports

    for path in input_files:
        normalized = _load_and_normalize(fmt, path)
        if normalized is None:
            errors += 1
            continue
        if args.split:
            return _run_split(normalized, conventions, args, path, search_paths, out_dir)
        root_schema = _by_value(normalized.root_schema, normalized.node_type_names, args)
        if root_schema is None:
            errors += 1
            continue
        result = converter.convert_schema(root_schema, normalized.top_level_name,
                                          normalized.node_type_names,
                                          base_path=path.parent,
                                          search_paths=search_paths,
                                          source_path=path,
                                          conventions=conventions)
        _report(result, args.verbose, path)
        if not result.data_types and not result.node_types:
            continue
        out_path = _write_file(result, path.stem, out_dir)
        print(f"  → {out_path}")
        written += 1
        profile_files[out_path.name] = list(result.imports.values())

    if written == 0:
        print("No TOSCA files were written.", file=sys.stderr)
        return 1
    if args.profile:
        _write_profile(_import_order(profile_files), args.profile, out_dir)
        print(f"  → {out_dir / 'profile.yaml'}")
        missing = _missing_imports(profile_files, out_dir)
        if missing:
            print("Warning: the profile imports files this run did not write; "
                  f"provide them beside profile.yaml: {', '.join(missing)}", file=sys.stderr)
    print(f"\n✅  {written} file(s) written to: {out_dir}/")
    if errors:
        print(f"⚠   {errors} file(s) had errors and were skipped.", file=sys.stderr)
        return 2
    return 0


# ── Commands ──────────────────────────────────────────────────────────────────

def main_json(argv: list[str] | None = None) -> int:
    """Convert plain JSON Schema files to TOSCA."""
    parser = _make_parser(
        "any2tosca",
        "Convert JSON Schema files to TOSCA data types (YAML).",
        "Examples:\n"
        "  any2tosca my_schema.json\n"
        "  any2tosca schemas/ -o output/\n"
        "  any2tosca a.json b.json -o output/",
        default_model=JSON_SCHEMA.conventions.relationship_model,
    )
    return run(JSON_SCHEMA, parser.parse_args(argv))


def main_redfish(argv: list[str] | None = None) -> int:
    """Convert Redfish JSON Schema files to TOSCA."""
    parser = _make_parser(
        "redfish2tosca",
        "Convert Redfish JSON Schema files to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  redfish2tosca Drive.json\n"
        "  redfish2tosca schemas/ -o tosca/",
        default_model=REDFISH.conventions.relationship_model,
    )
    return run(REDFISH, parser.parse_args(argv))


def main_oas(argv: list[str] | None = None) -> int:
    """Convert OpenAPI documents, or the Kubernetes specification, to TOSCA."""
    parser = _make_parser(
        "oas2tosca",
        "Convert OpenAPI 2.0/3.x documents to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  oas2tosca api.yaml\n"
        "  oas2tosca api.json -o output/\n"
        "  oas2tosca specs/ -o output/",
        default_model=OPENAPI.conventions.relationship_model,
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
    return run(KUBERNETES if args.kubernetes else OPENAPI, args)


def main_yang(argv: list[str] | None = None) -> int:
    """Convert YANG module files to TOSCA."""
    parser = _make_parser(
        "yang2tosca",
        "Convert YANG module files to TOSCA node and data types (YAML).",
        "Examples:\n"
        "  yang2tosca server.yang\n"
        "  yang2tosca models/ -o tosca/",
        default_model=YANG.conventions.relationship_model,
    )
    return run(YANG, parser.parse_args(argv))


# ── Entry point wrappers ──────────────────────────────────────────────────────

def cli_entry() -> None:
    sys.exit(main_json())

def redfish_entry() -> None:
    sys.exit(main_redfish())

def oas_entry() -> None:
    sys.exit(main_oas())

def yang_entry() -> None:
    sys.exit(main_yang())
