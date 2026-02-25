# query-missing-indexes

## Why It Matters

Prevent full-table scans on hot paths. Missing indexes often dominate total latency and amplify lock contention under load.

## Incorrect Pattern

- Filter or join large tables on non-indexed columns.
- Add indexes reactively without checking real query plans.

```sql
SELECT id, status, created_at
FROM orders
WHERE customer_id = $1
ORDER BY created_at DESC
LIMIT 50;
```

If `orders(customer_id, created_at DESC)` is missing, this can degrade into costly scans or sort-heavy plans.

## Correct Pattern

1. Inspect the plan with `EXPLAIN (ANALYZE, BUFFERS)`.
2. Add the smallest useful index that matches filter + order shape.
3. Re-check the plan and measure latency before/after.

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_customer_created_at
ON orders (customer_id, created_at DESC);
```

## Validation

- Confirm index scan or bitmap plan replaces sequential scan where expected.
- Compare p95/p99 latency before and after.
- Check write overhead and index bloat impact for high-churn tables.
