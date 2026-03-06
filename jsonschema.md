# JSON Schema Reference

## Core Keyword Definitions

| Keyword          | Category                 | Purpose / Description                                                            |
| ---------------- | ------------------------ | -------------------------------------------------------------------------------- |
| `$schema`        | Identification           | URI of the JSON Schema dialect (meta-schema) used.                               |
| `$id`            | Identification           | Unique identifier for this schema (URI).                                         |
| `$anchor`        | Identification           | Creates a local anchor within the schema for `$ref`.                             |
| `$dynamicAnchor` | Identification           | Defines a dynamic anchor for runtime `$dynamicRef` resolution.                   |
| `$ref`           | Reference                | Reference another schema (local or remote).                                      |
| `$dynamicRef`    | Reference                | Reference a dynamic anchor in a scope chain.                                     |
| `$defs`          | Definitions              | Container for reusable subschemas within the schema.                             |
| `$comment`       | Metadata                 | Free-form comment; ignored by validators.                                        |
| `title`          | Metadata                 | Short descriptive title for the schema.                                          |
| `description`    | Metadata                 | Long descriptive text for the schema.                                            |
| `default`        | Metadata                 | Default value for instances when none is provided.                               |
| `examples`       | Metadata                 | Example instances (informational).                                               |
| `type`           | Validation / Structural  | Declares expected data type(s) of instance (`string`, `number`, `object`, etc.). |
| `enum`           | Validation / Structural  | Array of literal values the instance is allowed to equal.                        |
| `const`          | Validation / Structural  | Single literal value the instance must equal.                                    |
| `allOf`          | Applicator / Structural  | Instance must validate against all schemas in this array.                        |
| `anyOf`          | Applicator / Structural  | Instance must validate against at least one schema in this array.                |
| `oneOf`          | Applicator / Structural  | Instance must validate against exactly one schema in this array.                 |
| `not`            | Applicator / Structural  | Instance must **not** validate against this schema.                              |
| `if`             | Conditional / Structural | Conditional schema; evaluated to decide `then` or `else`.                        |
| `then`           | Conditional / Structural | Applied if `if` evaluates to true.                                               |
| `else`           | Conditional / Structural | Applied if `if` evaluates to false.                                              |

## Validation Keyword Definitions
| Instance Type                 | Keyword                | Description                                                                                      |
| ----------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------ |
| **String**                    | `maxLength`            | Maximum number of characters in the string.                                                      |
|                               | `minLength`            | Minimum number of characters in the string.                                                      |
|                               | `pattern`              | Regex pattern the string must match.                                                             |
|                               | `format`               | Semantic format (e.g., `"email"`, `"date-time"`) – informational unless enforced by a validator. |
| **Number / Integer**          | `maximum`              | Maximum numeric value (inclusive).                                                               |
|                               | `exclusiveMaximum`     | Maximum numeric value (exclusive).                                                               |
|                               | `minimum`              | Minimum numeric value (inclusive).                                                               |
|                               | `exclusiveMinimum`     | Minimum numeric value (exclusive).                                                               |
|                               | `multipleOf`           | Instance must be a multiple of this number.                                                      |
| **Object**                    | `maxProperties`        | Maximum number of properties.                                                                    |
|                               | `minProperties`        | Minimum number of properties.                                                                    |
|                               | `required`             | Array of property names that must exist.                                                         |
|                               | `properties`           | Map of property names to schemas.                                                                |
|                               | `patternProperties`    | Map of regex patterns to schemas for property names.                                             |
|                               | `additionalProperties` | Schema or boolean controlling properties not listed in `properties`.                             |
|                               | `propertyNames`        | Schema applied to all property names.                                                            |
|                               | `dependentRequired`    | Properties that are required if another property exists.                                         |
|                               | `dependentSchemas`     | Schemas applied if a property exists.                                                            |
| **Array**                     | `maxItems`             | Maximum number of items in the array.                                                            |
|                               | `minItems`             | Minimum number of items in the array.                                                            |
|                               | `uniqueItems`          | Boolean: all items must be unique.                                                               |
|                               | `items`                | Schema for each item, or tuple of schemas for position-based validation.                         |
|                               | `additionalItems`      | Schema or boolean for extra items in a tuple array.                                              |
|                               | `contains`             | Schema that at least one item must satisfy.                                                      |
|                               | `minContains`          | Minimum number of items that must satisfy `contains`.                                            |
|                               | `maxContains`          | Maximum number of items that may satisfy `contains`.                                             |
| **Combinators / Applicators** | `allOf`                | Array of schemas, all must validate.                                                             |
|                               | `anyOf`                | Array of schemas, at least one must validate.                                                    |
|                               | `oneOf`                | Array of schemas, exactly one must validate.                                                     |
|                               | `not`                  | Instance must **not** validate against this schema.                                              |
| **Conditional**               | `if`                   | Schema evaluated to determine `then` / `else`.                                                   |
|                               | `then`                 | Applied if `if` evaluates true.                                                                  |
|                               | `else`                 | Applied if `if` evaluates false.                                                                 |
| **Constant / Enumeration**    | `const`                | Instance must equal this single value.                                                           |
|                               | `enum`                 | Instance must match one of the listed literal values.                                            |

## Hierarchy of Core keywords vs. Validation keywords

```
JSON Schema Draft 2020-12
├─ CORE VOCABULARY
│   ├─ Identification / References
│   │   ├─ $schema
│   │   ├─ $id
│   │   ├─ $anchor
│   │   ├─ $dynamicAnchor
│   │   ├─ $ref
│   │   └─ $dynamicRef
│   │
│   ├─ Definitions / Reuse
│   │   └─ $defs
│   │
│   ├─ Metadata / Documentation
│   │   ├─ $comment
│   │   ├─ title
│   │   ├─ description
│   │   ├─ default
│   │   └─ examples
│   │
│   └─ Structural / Applicator
│       ├─ type
│       ├─ enum
│       ├─ const
│       ├─ allOf
│       ├─ anyOf
│       ├─ oneOf
│       ├─ not
│       ├─ if
│       ├─ then
│       └─ else
│
├─ VALIDATION VOCABULARIES
│
│   ├─ String Keywords
│   │   ├─ minLength
│   │   ├─ maxLength
│   │   └─ pattern
│   │
│   ├─ Number / Integer Keywords
│   │   ├─ minimum
│   │   ├─ exclusiveMinimum
│   │   ├─ maximum
│   │   ├─ exclusiveMaximum
│   │   └─ multipleOf
│   │
│   ├─ Object Keywords
│   │   ├─ properties
│   │   ├─ required
│   │   ├─ patternProperties
│   │   ├─ additionalProperties
│   │   ├─ propertyNames
│   │   ├─ minProperties
│   │   └─ maxProperties
│   │
│   ├─ Array Keywords
│   │   ├─ items
│   │   ├─ additionalItems
│   │   ├─ minItems
│   │   ├─ maxItems
│   │   └─ uniqueItems
│   │
│   └─ Format / Conditional Keywords
│       ├─ contains
│       ├─ minContains
│       ├─ maxContains
│       ├─ dependentSchemas
│       ├─ dependentRequired
│       ├─ propertyNames
│       └─ format  (informational, not enforced by core)
```