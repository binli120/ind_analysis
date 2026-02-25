# schema-partial-indexes

## Why It Matters

Reduce index size and maintenance cost by indexing only rows needed by frequent predicates.

## Incorrect Pattern

- Create wide global indexes on tables where most rows are irrelevant to the primary query path.

```sql
CREATE INDEX idx_tasks_org_status_created_at
ON tasks (organization_id, status, created_at DESC);
```

If most reads target active rows only, this index can become larger and slower than necessary.

## Correct Pattern

1. Confirm frequent predicate shape (for example, `deleted_at IS NULL` or `status = 'active'`).
2. Create a partial index that matches the dominant filter path.
3. Validate planner usage and write overhead.

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_org_created_active
ON tasks (organization_id, created_at DESC)
WHERE deleted_at IS NULL AND status = 'active';
```

## Validation

- Verify matching queries use the partial index.
- Ensure predicate in query text matches partial index condition.
- Measure insert/update overhead after adding the index.
