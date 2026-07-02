# migration-guard

Static safety reviewer for database migrations. It catches the schema changes that lock or
break production — blocking DDL, data-loss operations, and zero-downtime incompatibilities —
**before** they ship, and rewrites them into safe multi-step migrations.

Works as a [SKILL.md](./SKILL.md) skill for Claude Code, Codex CLI, Cursor, Gemini CLI, and other
compatible agents, and as a standalone CLI you can drop into CI.

## Why

A one-line `ALTER TABLE ... ADD COLUMN ... NOT NULL` is fine on a laptop and a multi-minute outage
on a table with 100M rows. `CREATE INDEX` without `CONCURRENTLY` freezes writes. A `RENAME` breaks
the old app version mid rolling-deploy. These are well-known traps, but they're easy to miss in
review — migration-guard encodes them so the agent (and CI) catch them every time.

## Free vs Full

| Feature | Free | Full |
|---|---|---|
| **BLOCKER rules** — locking DDL (non-concurrent indexes, rewriting `ALTER TYPE`, `SET NOT NULL`, unvalidated constraints, `VACUUM FULL`, inline PK/UNIQUE builds, stored generated columns) | ✅ | ✅ |
| **BLOCKER rules** — data loss (`DROP`, `TRUNCATE`, `UPDATE`/`DELETE` without `WHERE`, narrowing type changes) | ✅ | ✅ |
| **Oracle support** — `ONLINE` index builds, `SET UNUSED` vs `DROP COLUMN`, `ALTER TABLE MOVE`, auto-committing DDL | ✅ | ✅ |
| **CRITICAL rules** — zero-downtime / rolling-deploy hazards (`RENAME`, adding `NOT NULL` while old version writes, single-statement backfills, DDL+DML in one transaction, missing `lock_timeout`) | ❌ | ✅ |
| **WARNING rules** — hygiene (Flyway naming, missing `IF [NOT] EXISTS`, `DROP CASCADE`, low-selectivity indexes) | ❌ | ✅ |
| **Zero-downtime rewrites** — the agent produces a complete safe multi-step migration (expand-contract, batched backfill, two-step constraint validation) | ❌ | ✅ |
| **Semantic layer** — catches what regex can't: dropped column still mapped in a JPA `@Entity`, forgotten rollback script, interacting migrations in the same PR | ❌ | ✅ |
| **Output formats** | text only | text + JSON + SARIF |
| **CI gate** (`--fail-on blocker\|critical\|warning` + exit code) | blocker only | all severities |
| **MySQL support** — online DDL nuances, gh-ost/pt-osc guidance | ❌ | ✅ |
| **Liquibase support** — XML/YAML changeSet tag mapping | ❌ | ✅ |

**Get the full edition →** *(marketplace link coming soon)*

## Install (as a skill)

```bash
curl -sSL https://github.com/julia-shtal/migration-guard-free/releases/latest/download/migration-guard-free.zip -o mg.zip
unzip mg.zip -d ~/.claude/skills/
```

Then just ask your agent to review a migration — the skill triggers automatically on `ALTER TABLE`,
`CREATE INDEX`, Flyway/Liquibase files, or "is this safe for prod".

## Use (as a CLI)

```bash
python scripts/check_migration.py path/to/migration.sql --dialect postgres
python scripts/check_migration.py db/migration/ --format sarif        # whole directory
python scripts/check_migration.py V7__add_index.sql --fail-on critical # CI gate
```

- `--dialect postgres|mysql|oracle`
- `--format text|json|sarif`
- `--fail-on blocker|critical|warning` — exit code `1` at/above the threshold, else `0`

## Example: before → report → after

**Before** (`V1__add_status.sql`):

```sql
ALTER TABLE orders ADD COLUMN status text NOT NULL;
```

**Report:**

```
=== BLOCKER ===
[MG-001] V1__add_status.sql:1
    ADD COLUMN NOT NULL or with a volatile DEFAULT forces a full table rewrite under ACCESS EXCLUSIVE.
    fix: Add the column nullable (no volatile default), backfill in batches, then enforce NOT NULL via a validated CHECK (MG-006).

=== CRITICAL ===
[MG-034] V1__add_status.sql:1
    A locking migration without lock_timeout can pile up behind a long transaction and freeze the table.
    fix: Set lock_timeout (and often statement_timeout) at the top so the migration fails fast.

Summary: 1 blocker, 1 critical, 0 warning.
```

**After** (safe, split into ordered migrations):

```sql
-- V1__add_status_nullable.sql
SET lock_timeout = '5s';
ALTER TABLE orders ADD COLUMN status text;
ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'new';

-- V2__backfill_status.sql   (run in batches by id range, committing each batch)

-- V3__enforce_status_not_null.sql
SET lock_timeout = '5s';
ALTER TABLE orders ADD CONSTRAINT status_nn CHECK (status IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT status_nn;
ALTER TABLE orders ALTER COLUMN status SET NOT NULL;
ALTER TABLE orders DROP CONSTRAINT status_nn;
```

## CI integration (GitHub Actions)

```yaml
- name: Check DB migrations
  run: |
    pip install sqlglot
    python scripts/check_migration.py db/migration/ --dialect postgres --fail-on critical
```

Add `--format sarif` and upload the output to GitHub code scanning to see findings inline on PRs.

## Suppressing a rule

When you have a real reason (e.g. an index built during a maintenance window):

```sql
-- migration-guard:ignore MG-002 built during Sunday 02:00 maintenance
CREATE INDEX idx_users_email ON users (email);
```

Destructive drops need explicit confirmation instead:

```sql
-- migration-guard:confirmed-drop replaced by orders_v2 in V41
DROP TABLE legacy_orders;
```

## What it checks

Postgres (~28 rules), MySQL (~12 rules), and Oracle (~8 rules: ONLINE index builds,
SET UNUSED vs DROP COLUMN, ALTER TABLE MOVE, auto-committing DDL), grouped as:

- **BLOCKER** — locking DDL (non-concurrent index, rewriting `ALTER TYPE`, `SET NOT NULL`,
  unvalidated constraints, inline PRIMARY KEY/UNIQUE builds, stored generated columns, `VACUUM FULL`) and data loss (`DROP`, `TRUNCATE`, `UPDATE`/`DELETE`
  without `WHERE`, narrowing type changes).
- **CRITICAL** — zero-downtime / rolling-deploy hazards (`RENAME`, adding `NOT NULL` while the old
  version writes, single-statement backfills, DDL+DML in one transaction, missing `lock_timeout`).
- **WARNING** — hygiene (Flyway naming, missing `IF [NOT] EXISTS`, `DROP CASCADE`, low-selectivity
  index).

Full catalog with rationale and fixes: [`references/rules-postgres.md`](./references/rules-postgres.md).

## Editions

- **free** — BLOCKER-group lock + data-loss rules, text output. Catches the worst outages.
- **full** — every rule, zero-downtime rewrites, JSON/SARIF output, MySQL support.

Build them: `python build.py --edition free` / `--edition full` (outputs to `dist/`).

## Limitations

migration-guard is a static analyzer: it reads SQL, it does not connect to your database or know
your table sizes. It reduces risk; it does not replace a human review for high-stakes changes.
When an operation rewrites a table and the size is unknown, it errs toward warning.
