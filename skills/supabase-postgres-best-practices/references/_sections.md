# Supabase Postgres Rule Sections

Use this index to choose which rule category to apply first.

| Priority | Category | Prefix | Impact | Typical Trigger |
| --- | --- | --- | --- | --- |
| 1 | Query Performance | `query-` | Critical | Slow queries, seq scans, expensive joins, bad plans |
| 2 | Connection Management | `conn-` | Critical | Timeouts, connection spikes, pool exhaustion |
| 3 | Security and RLS | `security-` | Critical | RLS policy review, auth-bound queries, tenant isolation |
| 4 | Schema Design | `schema-` | High | New tables, migrations, index strategy, partitioning |
| 5 | Concurrency and Locking | `lock-` | Medium-High | Deadlocks, blocking writes, long transactions |
| 6 | Data Access Patterns | `data-` | Medium | N+1 access, pagination, bulk reads/writes |
| 7 | Monitoring and Diagnostics | `monitor-` | Low-Medium | Regression checks, observability, tuning loops |
| 8 | Advanced Features | `advanced-` | Low | Extensions, specialized data types, advanced planner features |

## Selection Procedure

1. Detect the primary failure mode from the request.
2. Start at the highest-priority category that matches the failure mode.
3. Add lower-priority categories only when they materially affect the same outcome.
4. Keep recommendations scoped to a measurable performance or reliability objective.
