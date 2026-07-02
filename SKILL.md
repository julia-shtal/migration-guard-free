---
name: migration-guard
description: Reviews SQL, Flyway, and Liquibase database migrations for production safety before they ship. Catches blocking DDL, data-loss operations, and zero-downtime/rolling-deploy incompatibilities, then rewrites the migration into a safe multi-step version. Apply this skill whenever the user shows, writes, or asks about a database migration — an ALTER TABLE, CREATE INDEX, schema change, Flyway/Liquibase changelog, or asks "is this safe for prod", "will this lock the table", "review my migration", or "why did the migration hang production" — even if they don't explicitly ask for a safety review.
---

# migration-guard

Static safety reviewer for database migrations. It combines a deterministic analyzer
(`scripts/check_migration.py`) with your judgment about the specific schema and deploy setup.

## When this triggers

Any migration content: raw `.sql`, Flyway `V__`/`R__` scripts, Liquibase `.xml`/`.yaml` changelogs,
or a migration the user is *about to write*. Also triggers on questions about a migration that
already caused an incident.

## Workflow

1. **Determine the dialect.** Look for signals in the project: `docker-compose.yml`, `application.yml`
   (`jdbc:postgresql` vs `jdbc:mysql`), the driver in `pom.xml`/`build.gradle`. If nothing indicates
   it, ask the user (default to `postgres`).

2. **Run the analyzer.** From the skill directory:
   ```bash
   python scripts/check_migration.py <path> --dialect postgres --format json   # or mysql / oracle
   ```
   Use `--format text` for a quick human read, `--format sarif` when wiring into CI. Use
   `--fail-on critical` (or `blocker`) as a CI gate (exit code 1 at/above the threshold).

3. **Explain each finding in the user's context.** For every rule id in the output, read the matching
   entry in `references/rules-postgres.md` (or `rules-mysql.md`) and translate it to *their* migration:
   name the table, and if the user mentioned row counts or a rolling deploy, factor that in. Don't just
   echo the rule text — say why it bites *this* migration.

4. **Rewrite the migration safely.** Produce the full corrected migration, not a fragment. For
   CRITICAL rules that need a multi-step fix (expand-contract, batched backfill, concurrent index,
   two-step constraint validation), read `references/zero-downtime-patterns.md` and lay out every
   step as separate `V{n}` migrations with the ordering explained.

5. **Add the semantic layer the analyzer can't do.** The script is pattern-based; you can reason about
   things it can't:
   - Does the migration match the entities/code in the repo? (e.g., dropping a column still referenced
     in a JPA `@Entity` or a model class.)
   - Is there a forgotten rollback / undo script if the project maintains them?
   - Do several migrations in the same PR interact badly (one adds an index the next drops)?
   - Is the *intent* consistent — e.g., a "confirmed-drop" marker on a table that other migrations
     still populate?
   Report these as additional findings alongside the analyzer's.

6. **If there are no findings**, say so briefly and offer at most one or two genuine improvements
   (a `lock_timeout`, a partial index). Do not invent problems to look useful.

## Reading the analyzer output

Each finding has `rule_id`, `severity` (BLOCKER / CRITICAL / WARNING), `file`, `line`, `message`,
and `safe_alternative`. Severities:
- **BLOCKER** — will lock or destroy data in production. Must be fixed before shipping.
- **CRITICAL** — breaks zero-downtime / rolling deploys (old and new app versions running together).
- **WARNING** — hygiene and idempotency; fix when convenient.

`MG-PARSE` means a statement couldn't be parsed and only regex-level rules ran on it — mention that
the review of that statement is partial.

## Suppressing a rule (when the user has a good reason)

A rule can be suppressed on a specific migration with a comment:
```sql
-- migration-guard:ignore MG-002 index built during a scheduled maintenance window
```
Destructive drops require explicit confirmation instead of suppression:
```sql
-- migration-guard:confirmed-drop replaced by orders_v2 in V41
DROP TABLE legacy_orders;
```
Only suggest suppression when the user's justification is real; never add it just to silence output.

## Reference files

- `references/rules-postgres.md` — full Postgres rule catalog (ids, rationale, safe fix). Read the
  entries for whichever ids fired.
- `references/rules-mysql.md` — MySQL catalog (online DDL, no transactional DDL, gh-ost/pt-osc).
- `references/rules-oracle.md` — Oracle catalog (ONLINE index builds, SET UNUSED vs DROP COLUMN,
  ALTER TABLE MOVE, auto-committing DDL). For locking rules suggest `ALTER SESSION SET DDL_LOCK_TIMEOUT`.
- `references/zero-downtime-patterns.md` — expand-contract, batched backfill, concurrent index,
  two-step validation, fail-fast timeouts. Read when a CRITICAL rule needs a multi-step rewrite.
- `references/liquibase-notes.md` — how changeSet tags map to rules and how suppression works there.

## Hard rules

- Never claim a migration is safe without running the analyzer first.
- When rewriting, give the complete migration set in order; a half-fix is worse than a clear warning.
- Table size matters: a rewrite that's instant on 1k rows is an outage on 100M. If size is unknown and
  the operation rewrites the table, assume it could be large and warn accordingly.
