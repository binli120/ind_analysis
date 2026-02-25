---
name: supabase-postgres-best-practices
description: Optimize Postgres performance and reliability using Supabase best practices. Use when writing, reviewing, or debugging SQL queries, indexes, schema migrations, connection pooling, RLS policies, lock/concurrency behavior, data access patterns, or database monitoring in Postgres projects.
---

# Supabase Postgres Best Practices

## Scope

- Apply Supabase Postgres guidance as prioritized guardrails instead of a generic checklist.
- Prioritize query, connection, and security risks before lower-impact improvements.

## Load Needed References

- Read [references/_sections.md](references/_sections.md) first to map the task to category priority.
- Read [references/rules/query-missing-indexes.md](references/rules/query-missing-indexes.md) when slow plans or table scans are present.
- Read [references/rules/schema-partial-indexes.md](references/rules/schema-partial-indexes.md) when predicates are selective (status, soft-delete, tenant, time window).
- Load only the minimum number of rule files required for the current task.

## Execution Workflow

1. Classify the request by category and impact using `_sections.md`.
2. Audit SQL/schema/RLS changes against matching highest-priority rules.
3. Propose exact SQL changes (query rewrite, index, schema adjustment, policy/index pairing, connection guidance).
4. State trade-offs explicitly (write amplification, lock risk, memory pressure, complexity).
5. Define validation with `EXPLAIN (ANALYZE, BUFFERS)`, query latency, and cardinality checks.
6. Return prioritized findings in severity order.

## Output Contract

- Include:
  - `priority`
  - `problem`
  - `recommended_change`
  - `sql_patch` (if applicable)
  - `validation_plan`
  - `risk_if_unfixed`
- Prefer concise bullets and executable SQL over long narrative.

## Non-Negotiable Rules

- Do not recommend index creation without checking predicates and selectivity.
- Do not recommend raising connection counts before checking pooling and long transactions.
- Do not weaken RLS for speed; optimize policy predicates and supporting indexes first.
- Do not claim performance improvement without a measurable validation plan.
