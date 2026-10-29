# queryfix-sandbox

A deliberately flawed Spring Boot application, used as a fixture for QueryFix.

**This is not a reference implementation.** It contains four planted performance
bugs and one destructive query. Do not copy patterns out of it, and do not point
it at any database you care about.

## Layout

```
src/main/java/com/queryfix/sandbox/
├── SandboxApplication.java
├── domain/          Customer, Order, OrderItem, OrderStatus
├── repository/      CustomerRepository, OrderRepository, OrderItemRepository
├── service/         ReportingService, MaintenanceService
└── seed/            DataSeeder (JavaFaker), FixtureExerciser
src/main/resources/
├── application.properties
├── spy.properties               p6spy statement-log format
└── db/migration/V1__create_tables.sql
```

## Schema

`customers` 1—N `orders` 1—N `order_items`, with `ON DELETE CASCADE` down the
chain.

Index coverage is deliberate. `orders.customer_id`, `orders.placed_at`,
`orders.status`, `order_items.order_id`, and `customers.signed_up_at` are
indexed; `customers.email` is uniquely indexed. **`customers.country` is
intentionally left unindexed** — it is the target of planted bug 2. Do not add an
index for it.

## Planted bugs

Each is marked in source with a `PLANTED BUG:` comment.

| # | Bug | Location | Caught by |
| - | --- | -------- | --------- |
| 1 | Native query, `SELECT *`, no `WHERE` | `OrderRepository.exportAllOrders` | Static rules + `EXPLAIN ANALYZE` (seq scan) |
| 2 | JPQL filtering an unindexed column (`country`) | `CustomerRepository.findByCountryCode` | `EXPLAIN ANALYZE` + HypoPG index suggestion |
| 3 | Derived method called inside a `for` loop (N+1) | `ReportingService.buildCustomerOrderSummary` | LLM cross-query stage + p6spy statement log |
| 4 | `UPDATE` with no `WHERE` | `CustomerRepository.promoteAllToTier` | Static rules (critical) |

Each bug sits next to a healthy counterpart so the rules can be tested for false
positives as well as true ones — `exportRecentOrders`, `findBySignedUpAtAfter`,
`buildCustomerOrderSummaryBatched`, and `promoteHighValueCustomers` respectively.
QueryFix should flag the first column and stay silent on the second.

### Bug 4 is destructive and guarded

`promoteAllToTier` rewrites `loyalty_tier` for every row in `customers` and holds
a row lock on the whole table for the transaction. Its only call site,
`MaintenanceService.promoteLoyaltyTier`, throws unless
`sandbox.allow-destructive-fixtures=true`, which defaults to `false`. QueryFix
analyzes source, so the fixture works without ever executing. Enable the flag
only against a database you are willing to lose.

## Seed data

`DataSeeder` runs on startup (`sandbox.seed.seed-on-startup`, default `true`) and
skips if `customers` is already populated. On the fixed random seed (`20260805`)
it produces exactly 5,000 customers, 17,736 orders, and 53,896 order items, so
plan fixtures are reproducible.

Distributions are skewed rather than uniform, because uniform data hides the
problems QueryFix looks for — with even spread the planner's estimates are
always right and a missing index costs almost nothing:

- **Orders per customer** — heavy tail: ~28% never order, most place 1–4, a small
  VIP cohort places up to ~40. Row estimates for a customer join are therefore
  wrong for the tail.
- **Country** — weighted, US-dominant (40% US, 4% JP). A `country` predicate is
  highly selective for `JP` and barely selective for `US`, so the missing index
  shows very different costs per bind value.
- **Order status** — weighted toward `DELIVERED` (62%).
- **`placed_at`** — recency-weighted across 18 months; orders never predate signup.
- **Prices** — log-normal, with a thin tail of expensive items.

## Running it

Requires a JDK and a throwaway Postgres. The Maven wrapper is checked in, so no
Maven install is needed.

Use **JDK 21** (LTS). `pom.xml` targets 21, and Spring Boot 3.5 supports 17–24 —
so a newer JDK is outside the supported range even where it happens to work.
Builds and runs were verified on Temurin 21.0.12.

```bash
# Throwaway database
docker run --rm -d --name queryfix-sandbox-db \
  -e POSTGRES_DB=queryfix_sandbox \
  -e POSTGRES_USER=queryfix \
  -e POSTGRES_PASSWORD=queryfix \
  -p 5432:5432 postgres:16

./mvnw spring-boot:run          # mvnw.cmd on Windows
```

Flyway applies `V1__create_tables.sql` on boot, then the seeder fills the tables.
The app has no web starter: it runs its startup work and exits.

The build pins the JVM to `-Duser.timezone=UTC` (see `pom.xml`). That is not
cosmetic — pgjdbc sends the JVM's default zone as the `TimeZone` startup
parameter, and on a host whose zone resolves to a legacy alias such as
`Asia/Calcutta`, Postgres 16 rejects the connection outright with
`FATAL: invalid value for parameter "TimeZone"` and Flyway never runs.

## Capturing a statement log

Every statement goes through p6spy into `target/p6spy-statements.log`, formatted
as `epoch_millis|elapsed_ms|category|sql` — the contract that
`queryfix/integrations/p6spy.py` parses. Change one and change the other.

Seeding alone does not exercise the read-path fixtures, so the log holds no N+1
evidence by default. Turn the exerciser on:

```bash
./mvnw spring-boot:run -Dspring-boot.run.arguments=--sandbox.exercise-fixtures=true
```

That runs `buildCustomerOrderSummary` (the N+1) and its batched counterpart back
to back. On the default seed the contrast is stark — same 5,000 rows out:

| | Statements | Wall clock |
| --- | --- | --- |
| `buildCustomerOrderSummary` (N+1) | 5,001 | 34–48× the batched form |
| `buildCustomerOrderSummaryBatched` | 2 | baseline |

Compare the two runs against each other, not against a recorded number. Absolute
timings here moved by 5× across otherwise identical runs on one machine, purely
with background load — the statement *counts* and their ratio are what reproduce.
The exerciser logs both so the comparison is always in-run.

The exerciser is read-path only and never touches the destructive fixture.

### Why the N+1 is invisible per query

The derived method compiles to a join filtered on the parent's primary key, not
the bare foreign-key predicate you might expect:

```sql
select o1_0.id, ... from orders o1_0
  join customers c1_0 on c1_0.id = o1_0.customer_id
 where c1_0.id = ?
```

That plan is an index-only scan on `customers_pkey` feeding an index scan on
`idx_orders_customer_id`, and it finishes in about 0.09 ms. Each execution is
genuinely healthy; only the 5,000-fold repetition is the defect. This is exactly
why the fixture cannot be caught by single-query analysis — and why the statement
log matters, where the shape appears 5,000 times with 5,000 distinct bind values.

## Known dependency wrinkle

JavaFaker 1.0.2 (last released 2020) calls `new SafeConstructor()`, a no-arg
constructor SnakeYAML 2.x removed. Spring Boot 3.x manages SnakeYAML to 2.x, so
`pom.xml` pins `snakeyaml.version` back to `1.33`; config lives in
`application.properties` rather than `.yml` so Boot itself never needs SnakeYAML.
[Datafaker](https://www.datafaker.net/) is the maintained fork with the same API
if you would rather drop the pin — JavaFaker is used here because it was asked
for by name.

<!-- Touched by the QueryFix diff fixture PR: Markdown is a language no
extractor claims, so ingest must skip this file rather than degrade it. -->
