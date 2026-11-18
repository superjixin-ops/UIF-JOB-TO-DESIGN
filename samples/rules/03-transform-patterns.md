# Transform Patterns

## Prefer individual transforms over SparkSql

A raw SQL block is opaque to the platform: it cannot be inspected for lineage,
it cannot be validated for column types, and it cannot be reused.

Use the dedicated transform when one exists:

| Instead of | Use |
|---|---|
| `SELECT a, b FROM t` | `Select` |
| `WHERE x = 1` | `FilterData` |
| `a AS b` | `RenameColumns` |
| `CASE WHEN ... END` as a new column | `AddColumns` with an expression |
| `LEFT JOIN` | `JoinByExpression` with `joinType: left` |
| `DROP COLUMN` | `Drop` |

Use `SparkSql` only when the logic genuinely cannot be expressed by a
combination of the above — for example window functions over a partition that
no window transform covers.

## One responsibility per step

A transform should do one thing. A `SparkSql` block that filters, joins,
aggregates and renames at once is impossible to review. Break it up.

This also makes failures easier to localise: when step 7 fails, you know it was
the join, not "the big SQL".

## Renaming

- Rename early, once. Renaming the same column twice in a job means the second
  rename is fixing a mistake made by the first.
- Never rename a column to the same name as another column.
- Keep the original name when the target name is identical; a no-op rename
  makes the diff noisy.

## Joins

- Always state the join type. An implicit inner join silently drops rows when
  a dimension is missing.
- Join keys must be the same type on both sides. A string-to-integer join can
  work in testing and fail at scale.
- When a dimension can have duplicate keys, deduplicate it with `SelectDistinct`
  before joining, or the fact will fan out.

## Filters

- Filter before heavy joins, not after. Moving a `FilterData` above a join can
  cut the intermediate row count by orders of magnitude.
- A filter on a nullable column must decide explicitly what happens to nulls.
  `x <> 'A'` excludes nulls; `x IS NULL OR x <> 'A'` does not.

## Aggregation

- Be explicit about the grain. If the output is one row per order line, write
  that down in the load documentation.
- An aggregate without a `GROUP BY` key that matches the intended grain will
  collapse rows silently.
