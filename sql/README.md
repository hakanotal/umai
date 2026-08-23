# Ready-made queries

Mounted into pgAdmin at `/sql`, so they can be opened from the Query Tool with
**File → Open** without leaving the browser. Also usable from the terminal:

```bash
just q sql/what-did-i-eat.sql
```

They exist because almost nothing interesting in this schema is one table. What
you ate is `log_entries` joined to `food_items` joined to `foods`, filtered on
the supersede chain — and getting that filter wrong is the difference between
"today" and "today plus every version of today I later corrected".

| File | Answers |
|---|---|
| `what-did-i-eat.sql` | Every meal, newest first, with per-item macros |
| `today.sql` | Today's totals the way the bot computes them |
| `unmatched.sql` | Items with no `foods` row — what the enrichment job is chasing |
| `enrichment.sql` | What the researcher added, and what it refused to |
| `foods.sql` | The food table by trust tier and source |
| `learning.sql` | The personal library and portion priors as they accumulate |
| `corrections.sql` | Every correction, with what it changed |
| `spend.sql` | API cost by day, task and model |
| `health.sql` | What Health Auto Export has delivered |
| `steps.sql` | Steps per local day, with the sample count that reveals a granularity switch |
| `row-counts.sql` | One line per table — the "is anything in here" query |

## The one rule when writing your own

`log_entries.superseded_by IS NULL`. Entries are immutable: a correction writes
a *new* entry and points the old one at it. Every query that sums anything must
exclude superseded rows or it double-counts corrected meals.
