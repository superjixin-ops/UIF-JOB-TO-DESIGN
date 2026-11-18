---
name: multi-source-dimension
description: Load a dimension that is assembled from several source systems without one source overwriting another
---

# Multi-Source Dimension

## When to use this skill

Use this skill when a dimension is fed by more than one source system, for
example a customer dimension where one system owns the master record and
another contributes attributes.

## The problem

If each source writes the whole dimension with `mode: overwrite`, the last run
wins and the other sources' rows disappear. This is the most common defect in
multi-source dimensions and it is invisible until someone reports missing
customers.

## The pattern

1. **Partition by source.** Add a `SOURCE_SYSTEM` column during extraction.
2. **Append, do not overwrite.** The EID load uses `mode: append`.
3. **Deduplicate downstream.** The datamart job uses `FilterType2` on the
   natural key plus source, keeping the newest revision per source.
4. **Resolve precedence explicitly.** If two sources describe the same entity,
   declare which one wins. Do not let row order decide.

## Anti-pattern

```yaml
# WRONG - each source overwrites the whole dimension
load:
  mode: overwrite
  query: dimension/CUSTOMER/transform.parquet
```

With two sources this job produces a dimension that alternates between them on
every run.

## Review checklist

- [ ] `SOURCE_SYSTEM` present in every row
- [ ] EID load uses `append`
- [ ] natural key includes the source identifier, or precedence is declared
- [ ] dedupe step runs on the merged set, not per source
- [ ] a row-count check exists per source
