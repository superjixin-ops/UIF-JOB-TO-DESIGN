# Pipeline Structure Requirements

## Why structure matters

A pipeline is a contract: it declares where data comes from, how it changes,
and where it lands. When the structure is inconsistent, every downstream
consumer has to guess.

## Required job sequence

A fact pipeline normally has three jobs, in this order:

1. `eid` — extract and transform, writes the EID landing dataset
2. `datamart-parquet` — reshapes for reporting, writes partitioned parquet
3. `datamart-self-serve` — publishes to the queryable database

A dimension pipeline usually stops after `datamart-parquet`, because the
dimension is small enough to serve directly.

## Extract rules

- Exactly one extract must be marked `main_df: true` per job. More than one
  makes the join order ambiguous and produces unpredictable row counts.
- Every extract must declare `connector` and `credentials`.
- A `skapi` extract should also declare `keyName` and `naturalKey`; without
  them incremental loading cannot identify new rows.

## Naming

- `metadata.name` must match the file name without the extension.
- `alias` values are upper snake case, and must be unique within a job.
- The load `metadata.name` must equal the `loadObject` of the connection.

## What to check before opening a review

- [ ] one and only one `main_df`
- [ ] every job has `setups.size`
- [ ] `metadata.name` matches the file name
- [ ] load `metadata.name` matches `loadObject`

## Common failure conditions

| Symptom | Likely cause |
|---|---|
| job produces zero rows | filter predicate too strict, or join key type mismatch |
| duplicate rows after load | missing dedupe before write, or wrong `main_df` |
| downstream cannot find a column | column dropped in a later job without updating the schema |
