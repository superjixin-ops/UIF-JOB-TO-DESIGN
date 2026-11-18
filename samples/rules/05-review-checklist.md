# Design Review Checklist

## How to review a design document

A design document is not a summary of the YAML. It is a set of claims that a
reviewer should be able to verify. If a claim cannot be verified, it should be
marked as an open question rather than stated as fact.

## Structural checks

- [ ] `metadata.name` matches the file name
- [ ] every job declares `setups.size`
- [ ] exactly one `main_df: true` per job
- [ ] job order matches the standard: eid → datamart-parquet → publish

## Column-level checks

- [ ] every load declares a schema
- [ ] adjacent job schemas agree after drop and rename
- [ ] decimal columns declare precision and scale
- [ ] columns promised non-null are guaranteed by a transform
- [ ] no column is renamed to a name that already exists

## Lineage checks

- [ ] every target column traces to a source column or a documented expression
- [ ] derived columns have a stated formula
- [ ] dropped columns are listed with a reason

## Operational checks

- [ ] load mode (`overwrite` / `append`) matches the grain
- [ ] partition columns are low-cardinality and stable
- [ ] placeholder keys use `-1`, not null, and the type is integer
- [ ] credentials match the connector type

## How to write findings

Each finding should carry:

1. **severity** — `high` blocks release, `check` needs confirmation, `info` is
   informational
2. **evidence** — the file and the specific column names or numbers
3. **what is uncertain** — if the reviewer cannot decide whether it is a bug or
   a deliberate choice, say so

A finding without evidence is an opinion. Reviewers ignore opinions.
