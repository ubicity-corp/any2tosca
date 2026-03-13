# Circular Import Avoidance Algorithm

TOSCA 2.0 forbids circular imports. Because Redfish (and similar) JSON Schema
corpora contain many cross-file `$ref` cycles, `converter.py` applies a
multi-layered algorithm to detect and break those cycles before they reach the
TOSCA output.

---

## Concepts

### Versioned vs. unversioned files

Redfish schemas come in two shapes:

| Shape | Example | Role |
|---|---|---|
| **Unversioned stub** | `NetworkAdapter.json` | Aggregates versioned variants via `anyOf`. Never defines types independently. |
| **Versioned schema** | `NetworkAdapter.v1_14_0.json` | Defines the actual type properties. Used as `derived_from` target. |

### File-root type

A schema file is an *explicit-root* file when its top-level document contains
`"$ref": "#/definitions/TypeName"`. That named definition is the file's
*root type* — the canonical Redfish resource the file represents.

When an explicit-root file is independently converted, the file-root bypass
(see §4) can force an import of it even if the file was tentatively marked
no-import.

### `_no_import_files`

The central data structure of the algorithm. Filenames in this set are treated
as if their types were defined locally in the current output file:

- `ref_to_tosca_type` returns a bare type name (no `namespace:` prefix, no import recorded).
- `schema_to_property` inlines the referenced schema rather than emitting an external type reference.
- `_mark_no_import(fname)` adds `fname` to the set **and** registers the file's root type (if any) in `_node_type_names` so that references to it are recognised as node-type references.

---

## Phase 1 — Pre-scan (`_collect_no_import_files`)

Called once at the very start of each `convert_schema` invocation, before any
type is converted. It scans every `$ref` in the **root schema document** (the
in-memory dict for the file being converted) and marks external files that
would create circular TOSCA imports.

```
for each external filename F referenced anywhere in root_schema:
    if F is versioned (matches BaseName.vX_Y_Z):
        if F directly contains a $ref back to the current file:
            mark F as no-import          ← _refs_back_to_current
    else (F is unversioned):
        if the current file is transitively reachable from F via $refs:
            mark F as no-import          ← _refs_back_to_current_transitive
```

### Helpers used

| Helper | What it checks |
|---|---|
| `_refs_back_to_current(ref)` | Does the target file's **direct** `$ref` set include `_current_filename`? O(1) set lookup after `_ref_fnames_for` caches the direct refs of each file. |
| `_refs_back_to_current_transitive(ref)` | Is `_current_filename` reachable from the target file via an arbitrarily deep BFS over all `$ref` links? Uses `_get_reachable_fnames` (persistent BFS cache) + an O(1) frozenset membership test. |

### Limitation

The pre-scan only sees refs in the **root schema**. Refs that appear exclusively
inside *versioned* schemas (e.g. `EthernetInterface.v1_11_0.json` refs
`Chassis.json`) are not visible here. Those are caught by the on-demand checks
in Phase 2.

---

## Phase 2 — On-demand checks during conversion

These checks fire whenever a `$ref` is resolved during type conversion. They
catch files that were not visible during the pre-scan.

### 2a. `_classify_member` (called from `_merge_allof`)

Every member of an `anyOf`/`oneOf`/`allOf` is classified before deciding how
to handle it:

```
if ref is local (#/…):
    → "local"
if ref points to a versioned file (BaseName.vX_Y_Z):
    if the versioned file directly refs the current file:
        mark it no-import
    → "versioned"   (always; versioned files use the file-root bypass later)
else (unversioned):
    if already in _no_import_files:
        → "circular"
    if _refs_back_to_current_transitive returns True:
        mark it no-import
        → "circular"
    → "unversioned"
```

Classification determines how the member is processed in `_merge_allof`:
- `"versioned"` → candidate for `derived_from` (subject to further guards below).
- `"local"` → candidate for `derived_from` (no versioned refs present) or merged inline.
- `"unversioned"` / `"circular"` → properties merged inline; no import.

### 2b. `ref_to_tosca_type` (called for every property type reference)

The main import-registration function. Before recording an import it runs:

```
if fname is versioned:
    if fname's unversioned base is already no-import:
        mark fname no-import too  (entire family stays local)
        return bare type name
    if fname directly refs the current file (_refs_back_to_current):
        mark fname no-import
        return bare type name
else (fname is unversioned):
    if _would_create_back_import(fname, current) is True:
        mark fname no-import
        return bare type name
    if _refs_back_to_current_transitive(fname) is True:   ← catches deep chains
        mark fname no-import
        return bare type name

→ safe to import: record namespace→tosca_file and return "namespace:TypeName"
```

The dual check (`_would_create_back_import` **or** `_refs_back_to_current_transitive`)
for unversioned files ensures that both shallow 2-hop cycles and arbitrarily
deep transitive chains are caught, even when the ref was discovered inside a
versioned schema rather than the root document.

---

## Phase 3 — Outcome decisions

Once a file has been marked no-import (by either phase), every call site that
encounters a ref to it must decide what to emit. There are five possible
outcomes, applied in order:

### Outcome A — File-root bypass (force import of versioned no-import file)

Used in `_merge_allof` when the best versioned `derived_from` candidate was
marked no-import. The bypass re-enables the import when **all** guards pass:

| Guard | Condition that blocks the bypass |
|---|---|
| Unsafe-bypass guard | The current file is a utility library (no explicit top-level `$ref`) **and** it directly refs `fname`. Importing would be mutual. |
| Back-import guard (`_versioned_refs_our_root_type`) | The versioned file contains a `$ref` pointing to the current file's own root type. Independently converting the versioned YAML would trigger the file-root bypass *there*, re-importing the current file. |
| Indirect-ref guard (`_base_circular`) | The versioned file's unversioned base is in `_no_import_files` **and** the current file did not directly ref the versioned file. The versioned file was discovered through an inlined schema, so there is no safe import path. |

If all guards pass → record the import and emit `"namespace:TypeName"` as
`derived_from`.

If any guard blocks → fall back to merging all versioned refs inline; no
`derived_from`.

### Outcome B — Symmetric (mutual) cycle: lexicographic loser

Detected by `_would_create_back_import`. When two file families mutually depend
on each other (e.g. Sensor ↔ Control), `_would_create_back_import` uses
recursion with a stack sentinel to detect the mutual recursion, then breaks the
tie lexicographically:

```
if (fname, checking_for) appears in the recursion stack:
    result = fname > checking_base   # larger name loses (returns True = blocked)
```

The *winner* (lexicographically smaller family name, e.g. `Control`) is allowed
to import the other. The *loser* (e.g. `Sensor`) has its file-root type
reference emitted as `CommentedOut("namespace:TypeName")` — the type name is
preserved in the YAML as a comment so the intent is visible.

### Outcome C — Step-1 allow (safe one-directional import)

Used in `schema_to_property` and `_resolve_entry_type`. If `fname` is in
`_no_import_files` but `fname` **directly** refs the current file (i.e. the
dependency is one-directional from `fname`'s perspective), and `fname` is not
an explicit-root file (which would trigger the file-root bypass when
independently converted, re-importing the current file), then importing `fname`
is safe:

```
if not unsafe_bypass
   and current file does NOT directly ref fname
   and fname is NOT explicit-root
   and fname directly refs current file (_ref_fnames_for):
    → record import and emit "namespace:TypeName"
```

### Outcome D — CommentedOut (unresolvable file-root reference)

When a file-root node type is encountered via an inlined external schema (not a
direct ref of the current file), and none of the bypass conditions apply, the
type reference is emitted as a YAML comment:

```yaml
# namespace:TypeName
```

This preserves intent in the output without creating an invalid import or an
unresolvable bare type name.

### Outcome E — Inline expansion

For non-file-root types (e.g. sub-definitions inside circular files) and for
cases where all bypasses are blocked, the referenced schema's properties are
resolved and merged directly into the current type definition via
`_inline_type_or_primitive`. If the inlined schema has `properties`, a local
data type is created in the current output file.

---

## Caches and performance

All graph-traversal results are memoized to avoid repeated file I/O and BFS
traversals:

| Cache | Key | Scope | Contents |
|---|---|---|---|
| `_file_cache` | absolute path | persistent (all files) | raw JSON text |
| `_doc_cache` | absolute path | persistent | parsed JSON dict |
| `_ref_fnames_cache` | absolute path | persistent | direct external filenames of a file |
| `_reachable_cache` | start filename | persistent | BFS frozenset of all transitively reachable filenames |
| `_transitive_back_cache` | start filename | per-conversion | whether `_current_filename` is reachable from start |
| `_back_import_cache` | `(fname, checking_for)` | per-conversion | `_would_create_back_import` result |

Persistent caches survive across multiple `convert_schema` calls on the same
`Converter` instance (same base path / search dirs). Per-conversion caches are
reset at the start of each call.

---

## Decision flowchart

```
ref_to_tosca_type(ref)
│
├─ fname == current file?  → return bare type name (same TOSCA file)
│
├─ fname in _no_import_files?  → skip to "Outcome dispatch"
│
├─ fname is versioned?
│   ├─ base already no-import?  → mark fname no-import, return bare type name
│   └─ fname directly refs current?  → mark fname no-import, return bare type name
│
└─ fname is unversioned?
    ├─ _would_create_back_import(fname, current)?  ┐
    └─ _refs_back_to_current_transitive(fname)?    ┘ → mark fname no-import, return bare type name
    (either condition suffices)

    → safe: record import, return "namespace:TypeName"

"Outcome dispatch" (fname is in _no_import_files, at a call site):
│
├─ File-root bypass conditions all pass?  → force import (Outcome A)
├─ Symmetric loser (_back_import)?  → CommentedOut (Outcome B)
├─ Step-1 allow (fname refs current, not explicit-root)?  → safe import (Outcome C)
├─ File-root type found via inlined schema?  → CommentedOut (Outcome D)
└─ Otherwise  → inline schema properties (Outcome E)
```

---

## Known limitations

- The algorithm operates on **filename-level** granularity. Two definitions
  within the same file cannot be independently circular — they are always in
  the same TOSCA output file.

- The `_collect_no_import_files` pre-scan only sees `$ref`s in the root
  schema document. Refs introduced by versioned schemas are caught on-demand
  (Phase 2), which means the `_no_import_files` set can grow during conversion.
  Code that checks `_no_import_files` must therefore be prepared for this
  set to be updated mid-conversion.

- `_would_create_back_import` implements 2-hop look-ahead (fname's versioned
  schemas → their direct refs) for the purpose of symmetric-cycle
  tiebreaking. The full transitive check (`_refs_back_to_current_transitive`)
  is used as a complementary fallback to catch longer chains.
