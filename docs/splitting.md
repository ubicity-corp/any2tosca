# Splitting one input into several files

`--split RULES` converts one input file into several TOSCA files that import
each other. Split rules assign every schema of the input to a file, called a
**unit**, by the schema's name. It is available on every command, and it is how
`oas2tosca --kubernetes` writes one file per Kubernetes API group
([kubernetes.md](kubernetes.md)).

```bash
oas2tosca api.json --split rules.yaml --profile io.example:1.0 -o profile/
```

## Split rules

```yaml
rules:
  - match: '.*List'                             # first matching rule wins
    skip: true
  - match: 'io\.k8s\.apimachinery\.pkg\.apis\.meta\.v1\.(?P<name>[^.]+)'
    unit: meta                                  # written to meta.yaml
  - match: 'io\.k8s\.api\.(?P<unit>[a-z]+)\.v1\.(?P<name>[^.]+)'
  - match: '.*'
    skip: true
default_unit: other                             # optional
```

- Rules are tried in order against the **schema name** (the key under
  `definitions`, or `components.schemas` in OpenAPI 3.x). The first rule whose
  `match`, a Python regular expression, matches the whole name decides.
- The **unit** comes from the rule's `unit` key, or else from the pattern's
  `unit` named group. A rule gives one or the other.
- The **type name** comes from the pattern's `name` named group, or else is the
  full schema name with spaces, `-` and `.` replaced by `_`.
- `namespace` sets the namespace under which other units import the unit; the
  default is the unit name. Rules that name the same unit must agree on it.
- `skip: true` leaves the schemas the rule matches out of the output.
- A schema that matches no rule goes to the unit named by `default_unit`. Without
  `default_unit`, such a schema is an error.

Several rules may name the same unit, which gathers several name prefixes into
one file. Because rules can skip, they also select which schemas are converted
at all: writing `v1` into the patterns converts only version `v1`.

## What the output looks like

Each unit is written to `<unit>.yaml`. A schema that refers to a schema in the
same unit refers to its type by name; one that refers to a schema in another
unit uses `<namespace>:<type name>`, and the unit imports the other one:

```yaml
imports:
- url: meta.yaml
  namespace: meta
```

With `--profile NAME:VERSION`, the run also writes `profile.yaml`, which
declares the profile and imports every unit, each after the units it imports,
and `TOSCA.meta`, which names `profile.yaml` as the entry definitions. The unit
files declare no profile of their own.

Under the `links` relationship model, each unit's node types also get `feature`
and `containable` capabilities, written to `<unit>_caps.yaml`, and the unit
imports `Core.yaml` under the namespace `core`; no unit may then take the
namespace `core`. Under the `value` model, a unit holds its types only (see the
README's *Relationship models*).

## Errors

The run stops before converting anything when:

- two schemas get the same type name in one unit (for example a `v1` and a
  `v1beta1` `Deployment` both sent to `apps`); the message names both schemas;
- a unit name or namespace is not a valid TOSCA name;
- a unit name looks like a versioned schema file name (`Name.vX_Y_Z`);
- a converted schema refers to a skipped one. `--allow-skipped-refs` turns this
  into a warning, and the referring property falls back to the JSON type of the
  skipped schema (`map` for an object);
- units would import each other in a cycle, which TOSCA does not allow. The
  message names the units and, for each import in the cycle, a schema that
  causes it. `--merge-cycles` merges the units of each cycle into one, named
  after the first of them in rule order, instead.

`--split` takes exactly one input file.

## Options

| Option | |
|---|---|
| `--split RULES` | split the input using the rules in the YAML file `RULES` |
| `--merge-cycles` | merge units that would import each other in a cycle, instead of failing |
| `--allow-skipped-refs` | warn, instead of failing, when a converted schema refers to a skipped one |
| `--profile NAME:VERSION` | also write `profile.yaml` and `TOSCA.meta` |
