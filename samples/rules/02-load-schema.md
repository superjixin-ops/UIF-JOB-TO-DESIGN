# Load Schema Requirements

## Declare the schema, always

Every `load` block must declare a complete `schema` list. Leaving it implicit
means the physical table shape is decided at runtime by whatever the dataframe
happens to contain. That is how production tables end up with columns nobody
documented.

A declared schema serves three purposes:

1. It is the contract downstream consumers code against.
2. It makes type drift visible: a column changing from integer to string is a
   breaking change, and it is silent without a schema.
3. It lets validation fail the pipeline before bad data lands.

## The column contract

For each column declare:

| Field | Meaning |
|---|---|
| `columnName` | exact physical name |
| `dataType` | Spark type: IntegerType, StringType, DecimalType, TimestampType, DateType, BooleanType |
| `nullAble` | whether downstream may receive null |
| `dataTypeArgs` | required for DecimalType: precision and scale |

`nullAble: false` is a promise. If the dataframe can produce null, either fix
the transform or mark the column nullable. Do not mark it false because the
table looked fine in testing.

## Schema drift

The most common defect is a mismatch between two things:

- the columns the upstream job actually writes
- the columns the downstream `load.schema` declares

This happens when a `Drop` or `RenameColumns` step is added to one job and the
schema of the next job is not updated. The pipeline still runs; the physical
table is simply wider or narrower than documented.

To detect it, compare, for each pair of adjacent jobs:

```
frame_columns = upstream_schema
                minus columns dropped by downstream transforms
                with renames applied
declared = downstream_load_schema
```

Any column in `frame_columns` that is missing from `declared` is drift.

## Nullability pitfalls

- A `FillNA` step must run before the schema promises `nullAble: false`.
- `Soft delete` flags are often produced by `AddColumns`; make sure the type
  matches the schema (boolean, not string).
- Placeholder keys (`-1`) must be integers; mixing `-1` and `'-1'` breaks joins.

## Review checklist

- [ ] every load has a schema
- [ ] decimal columns declare precision and scale
- [ ] `nullAble: false` columns are guaranteed non-null by a transform
- [ ] adjacent job schemas agree after drop and rename
