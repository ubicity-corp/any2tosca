# One Pipeline, Format Rules Where Needed

**Status:** Review and proposal (2026-11-25). Not implemented.

`any2tosca` converts JSON Schema to TOSCA, and treats OpenAPI, Redfish, YANG and
the Kubernetes specification as JSON Schema with extra rules. This document
reviews where the code is general and where format rules are mixed into general
code, and proposes changes so that every format is the general pipeline plus a
small, explicit set of rules.

---

## How it works today

Every command runs the same stages. What differs per format:

| Stage | `any2tosca` | `redfish2tosca` | `oas2tosca` | `oas2tosca --kubernetes` | `yang2tosca` |
|---|---|---|---|---|---|
| Load | JSON | JSON | JSON or YAML | JSON or YAML | pyang, from the path |
| Normalize | none | `RedfishNormalizer.normalize(doc)` | `OpenAPIExtractor.extract(doc)` | the same, then `KubernetesNormalizer.normalize(root, names)` | `YANGNormalizer.normalize(path)` |
| Node types | none | the resource the file's top-level `$ref` names | `POST` bodies on paths with `GET` | the same | top-level `container` and `list` |
| Top-level name | `title`, else file stem | first node type, else stem | `info.title`, else stem | the same | module name, else stem |
| Relationship model | `links` | `links` | `value` | `value` | `links` |
| `x-` extensions | kept | all dropped by the normalizer | kept | four copied to metadata | none in the input |
| Split | `--split` | `--split` | `--split` | packaged rules | `--split` |

General already: the converter's JSON Schema → TOSCA mapping, the splitter, the
by-value pre-pass, extension metadata, `readOnly` → attribute, and profile
packaging.

---

## Findings

1. **The pipeline is written four times.** `main_json`, `main_redfish`,
   `main_oas` and `main_yang` (`cli.py`) each load, report errors, normalize,
   apply the by-value pre-pass, split, convert, write and report, in about 70
   lines that differ only in the stage table above. A new format is a fifth copy,
   and a new option is threaded through every copy: `--extension-metadata` needed
   five call sites.

2. **Normalizers have four interfaces.** `extract(doc)`, `normalize(doc)`,
   `normalize(path)` plus a `module_name` attribute, and `normalize(root, names)`.
   The top-level name is computed in the CLI, per format, from different sources.

3. **Converter options grow as keyword arguments.** `convert_schema` takes
   `relationship_model`, `extension_metadata`, `documents`, `namespaces` and
   `node_types_by_file` beside its positional arguments. The first two describe
   the format's conventions; the last three describe the split. Every caller
   passes the conventions one by one.

4. **Redfish rules apply to every format.** Outside the relationship model, the
   converter applies Redfish corpus rules to any input whose files look like
   Redfish:
   - versioned file families (`Base.vX_Y_Z`) in the circular-import avoidance
     (`_collect_no_import_files`, `_ref_to_tosca_type`);
   - the latest versioned `anyOf`/`oneOf` member as `derived_from`
     (`_pick_latest_versioned_ref`);
   - the file-root type heuristic, partly keyed on `redfish.dmtf.org` in `$id`
     (`_is_file_root_type`);
   - version-stripped capability file names (`_caps_file_for`).

   And `links` combines a general rule (a reference to a node type is a
   relationship: requirements, capabilities, `Core.yaml`) with two expansions
   that only Redfish has (the `Links` and `Actions` properties). A JSON Schema
   corpus that links resources cannot have the first without the second, or
   without the version-family rules if its file names happen to match.

5. **The Redfish normalizer drops every `x-` keyword**, so
   `--extension-metadata` has no effect on Redfish input. The converter already
   ignores keys it is not told to copy, so the dropping is not needed for
   correctness. (`x-longDescription` is promoted to `description` first.)

6. **Only a split can be packaged as a profile.** `--profile` requires
   `--split`. A multi-file run, such as a Redfish schema directory or a directory
   of JSON Schemas whose files import each other, writes the files but no
   `profile.yaml`.

7. **Plain JSON Schema and YANG default to `links`.** Every node type then gets
   `Feature` and `Containable` capabilities, a `_caps.yaml` file and a `Core.yaml`
   import, which are Redfish conventions. In JSON Schema a `$ref` embeds a value,
   and YANG containers do not reference each other with `$ref`.

---

## Proposal

### A. An input format is an object; one driver runs every format

```python
@dataclass(frozen=True)
class Normalized:
    root_schema: dict[str, Any]
    node_type_names: set[str]
    top_level_name: str

@dataclass(frozen=True)
class InputFormat:
    name: str                                  # "jsonschema", "redfish", "openapi", ...
    extensions: tuple[str, ...]                # input files a directory contributes
    load: Callable[[Path], Any]
    normalizers: tuple[Normalizer, ...]        # applied in order
    conventions: Conventions                   # see B
    split_rules: Path | None = None            # packaged rules, used without --split
```

Every normalizer implements one method, `normalize(doc, path) -> Normalized`
(the first in a chain receives the loaded document, the next ones the previous
`Normalized`). Kubernetes is the OpenAPI normalizer followed by the Kubernetes
one. One `run(fmt, args)` holds the pipeline; the four entry points become
wrappers that pick a format, and `oas2tosca --kubernetes` picks the Kubernetes
format. Options are parsed once and apply to every format.

### B. Conventions are one value, and Redfish rules have their own switch

```python
@dataclass(frozen=True)
class Conventions:
    relationship_model: str = "value"          # "links" | "value"
    redfish: bool = False                      # Links/Actions expansion, versioned
                                               # file families, file-root heuristic,
                                               # version-stripped capability files
    extension_metadata: tuple[str, ...] = ()
```

`convert_schema(..., conventions=...)` replaces the two convention keyword
arguments. `links` keeps the general relationship rules; the four Redfish rules
of finding 4 and the `Links`/`Actions` expansions run only when `redfish` is set.
Presets: Redfish `links` + `redfish`; OpenAPI `value`; Kubernetes `value` plus
its four extension keys and packaged split rules; JSON Schema and YANG see E.

### C. Normalizers keep extension keywords

The Redfish normalizer keeps `x-` keys (still promoting `x-longDescription`), so
`--extension-metadata` works for every format. Output is unchanged unless the
option is given.

### D. `--profile` for any multi-file run

`--profile NAME:VERSION` writes `profile.yaml` and `TOSCA.meta` for whatever the
run writes: the units of a split, or the files of a multi-file run, imported in
dependency order (from each file's imports). It no longer requires `--split`.

### E. Plain JSON Schema and YANG default to `value` (decision)

Their node types then carry no Redfish capabilities, `_caps.yaml` or `Core.yaml`
import; `--relationship-model links` restores the current output.

---

## Order and effect on output

| Change | Output change | Notes |
|---|---|---|
| A. Formats and one driver | none | the existing CLI tests cover every format |
| B. Conventions, `redfish` switch | none with the presets | Redfish keeps every rule; the others stop applying rules they never relied on |
| C. Extensions kept | none unless `--extension-metadata` | |
| D. `--profile` without `--split` | new output, only with the option | |
| E. `value` default for JSON Schema and YANG | yes: no capabilities, `_caps.yaml` or `Core.yaml` | needs a decision |

A, B and C first, as three commits with the test suite unchanged; then D; E on a
decision. Once B gates the Redfish rules, they can move out of `converter.py`
(2,200 lines) into their own module, which makes the general converter readable
on its own; that is optional and follows B.
