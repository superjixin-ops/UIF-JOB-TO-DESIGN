# Pipeline Sizing

## Always declare a size

Every job must declare `setups.size`. Without it, the platform falls back to a
default that may be far too small (job times out) or far too large (wastes
cluster capacity for hours).

## The tiers

| Size | Typical use | Order of magnitude |
|---|---|---|
| `T` | reference data, small dimensions | thousands of rows |
| `S` | medium dimensions, light datamart work | hundreds of thousands |
| `M` | daily incremental facts | millions per day |
| `L` | large facts, multi-join enrichment | tens of millions |
| `XL` | very wide joins, heavy shuffles | hundreds of millions |

## Start small, then measure

Start one tier below your guess. Run it. If it completes comfortably, keep it.
Scaling up is cheap; discovering after a month that every run over-allocates is
also cheap to fix but wastes cluster budget in the meantime.

Use these signals to adjust:

- job finishes in under 20% of the expected window → try one tier down
- job fails on executor memory → one tier up, and check for skew
- job spends most time in shuffle → the problem is the join, not the size

## What size does not fix

- **Skew.** One partition with 10x the rows will fail at every tier.
- **A missing filter.** Loading a whole table to use 1% of it is a query
  problem.
- **An accidental cross join.** That is a logic bug; more memory only delays
  the failure.

## Sizing and cost

Cost scales roughly with executor-hours. Doubling the tier for a job that does
not need it doubles the cost of that job forever. Review sizes when a pipeline
is touched for other reasons.
