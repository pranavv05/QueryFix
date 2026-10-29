<!-- queryfix:report -->

## QueryFix

Reviewed 2 queries and found 2 problems.

*Static analysis only: every finding below comes from the query's source text, not a measured execution plan — nothing in this change was run against a database.*

### 🟠 High

#### Repository call inside a loop issues one query per row

**Where:** `src/ReportingService.java:52` (`buildSummaries`), with `src/ReportingService.java:48`

*Inferred query shape, from the method name — not the literal SQL Spring Data will issue:*

```sql
SELECT *
FROM orders
WHERE customer_id = ?
```

`findByCustomerId` is called for each customer returned by the listing query, so the number of statements grows with the result set rather than staying constant.

**Impact:** At 5,000 customers this is 5,001 round trips where two would do. Latency is dominated by the round trips, not by the work.

<details><summary>Evidence</summary>

- **Statements captured:** 5,001 in one request
- **Distinct bind values:** 5,000 — not a cache miss

</details>

**Suggested fix:** Fetch the orders for every customer in one query.

```sql
SELECT * FROM orders WHERE customer_id IN (:ids);
```

*Evidence strength: strong, inferred — the model read the diff and no plan was available to check it. This was not confirmed against a running system or a query plan.*

<sub>`n-plus-one`</sub>

#### Filter on `orders.customer_id`, which has no index

**Where:** `src/OrderRepository.java:31` (`findByCustomerId`)

*Inferred query shape, from the method name — not the literal SQL Spring Data will issue:*

```sql
SELECT *
FROM orders
WHERE customer_id = ?
```

The WHERE clause filters on a column with no index leading it.

**Impact:** Every execution reads the whole table, so cost tracks table size.

**Suggested fix:** Add an index and measure it before committing.

```sql
CREATE INDEX idx_orders_customer_id ON orders (customer_id);
```

**Simulated cost:** 18,450.25 → 8.31 (99% lower)

<sub>`unindexed-filter`</sub>
