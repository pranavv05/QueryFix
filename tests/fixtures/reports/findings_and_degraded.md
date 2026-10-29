<!-- queryfix:report -->

## QueryFix

Reviewed 2 queries and found 2 problems. **Part of this change could not be reviewed — see below.**

*Static analysis only: every finding below comes from the query's source text, not a measured execution plan — nothing in this change was run against a database.*

### ⚠️ Not fully analyzed

These stages failed and were skipped, so this report covers less than the change does. Everything below is still accurate — it is just not everything.

- **extract** — `migrations/002_broken.sql`
- **static_rules**

### ⚠️ Queries that could not be parsed

These were found in the change but could not be read, so no rule ran against them.

- `migrations/002_broken.sql:1` — Invalid expression / Unexpected token. Line 1, Col: 10. SELECT FROM WHERE

### 🔴 Critical

#### `UPDATE` rewrites every row: no `WHERE` clause

**Where:** `migrations/003_customers.sql:4`

```sql
UPDATE customers
SET loyalty_tier = 'gold'
```

This UPDATE has no WHERE clause, so it matches every row in `customers` rather than a subset.

**Impact:** Every row in the table is rewritten, and the previous values are gone. On a table this size that is both a long write and an unrecoverable one without a restore.

**Suggested fix:** Scope the write to the rows it is meant to touch. If it really is every row, say so explicitly in the migration's comment so the next reader does not have to guess.

```sql
UPDATE customers SET loyalty_tier = 'gold' WHERE lifetime_value >= 1000;
```

<sub>`missing-where`</sub>

### 🟡 Medium

#### Query selects every column with `SELECT *`

**Where:** `migrations/001_orders.sql:12`

```sql
SELECT * FROM orders
```

The projection is `*`, so the query returns every column of every row it matches, including ones the caller never reads.

**Impact:** Every added column silently widens this query's result set, so a schema change elsewhere makes it slower with no change to this file.

**Suggested fix:** Name the columns the caller actually uses.

<sub>`select-star`</sub>
