# Postgres migration rules catalog

Each rule: id, severity, detection, why it's dangerous, and the safe alternative.
The `check_migration.py` analyzer implements these ids. Weights per severity are in
`../scripts/check_migration.py` (SEVERITY_ORDER). Rationale/safe text here is what the
SKILL.md workflow reads back to the user.

Severities: **BLOCKER** (will lock or destroy data in prod), **CRITICAL**
(breaks zero-downtime / rolling deploys), **WARNING** (hygiene / style).

---

## Locks (BLOCKER)

### MG-001: ADD COLUMN NOT NULL / with volatile DEFAULT on an existing table
detection: `ALTER TABLE ... ADD COLUMN` that is `NOT NULL` without a constant default, or a
default produced by a volatile function (`now()`, `random()`, `uuid_generate_v4()`, `gen_random_uuid()`).
Why dangerous: on PG < 11, and on any version when the default is volatile, this forces a full
table rewrite under `ACCESS EXCLUSIVE` — the table is unavailable for reads and writes for the
duration of the rewrite (minutes to hours on a large table).
Safe:
1. `ADD COLUMN col TYPE` (nullable, no default) — fast, metadata-only.
2. Backfill in batches (see MG-032 / zero-downtime-patterns.md).
3. `ALTER COLUMN col SET DEFAULT ...` and later `SET NOT NULL` via a validated CHECK (MG-006).

### MG-002: CREATE INDEX without CONCURRENTLY
detection: `CREATE INDEX` (or `CREATE UNIQUE INDEX`) without the `CONCURRENTLY` keyword.
Why dangerous: takes a `SHARE` lock that blocks all writes to the table until the index is built.
Safe: `CREATE INDEX CONCURRENTLY ...`. Note this cannot run inside a transaction (see MG-007).

### MG-003: DROP INDEX without CONCURRENTLY
detection: `DROP INDEX` without `CONCURRENTLY`.
Why dangerous: takes an `ACCESS EXCLUSIVE` lock on the table.
Safe: `DROP INDEX CONCURRENTLY ...`.

### MG-004: ALTER COLUMN TYPE with a rewriting conversion
detection: `ALTER TABLE ... ALTER COLUMN ... TYPE ...` where the conversion is not on the safe list.
Safe (no rewrite): widening `varchar(n)`→`varchar(m>n)` or `varchar`→`text`, and a few binary-compatible
casts. Everything else rewrites the whole table under `ACCESS EXCLUSIVE`.
Why dangerous: full table rewrite + exclusive lock.
Safe: add a new column of the target type, backfill in batches, swap via expand-contract
(zero-downtime-patterns.md).

### MG-005: ADD CONSTRAINT FOREIGN KEY / CHECK without NOT VALID
detection: `ADD CONSTRAINT ... FOREIGN KEY` or `ADD CONSTRAINT ... CHECK` without `NOT VALID`.
Why dangerous: validating the constraint scans and locks the whole table (and the referenced table
for FKs) under a lock that blocks writes.
Safe: `ADD CONSTRAINT ... NOT VALID` in one migration, then `VALIDATE CONSTRAINT ...` in a later one
(validation takes only a `SHARE UPDATE EXCLUSIVE` lock).

### MG-006: SET NOT NULL directly on an existing column
detection: `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`.
Why dangerous: performs a full-table scan to verify no nulls, under `ACCESS EXCLUSIVE`.
Safe (PG12+): add `CHECK (col IS NOT NULL) NOT VALID`, then `VALIDATE CONSTRAINT`, then `SET NOT NULL`
(the last step reuses the validated constraint and skips the scan).

### MG-007: CREATE/DROP INDEX CONCURRENTLY inside a transaction
detection: presence of `CONCURRENTLY` together with an explicit `BEGIN`/`COMMIT`, or a note that the
migration tool wraps statements in a transaction (Flyway does by default for non-`-- flyway:` scripts).
Why dangerous: `CONCURRENTLY` cannot run inside a transaction block — Postgres raises an error, and a
failed concurrent index leaves an `INVALID` index behind.
Safe: run the concurrent statement in its own non-transactional migration (Flyway: put it alone in a
script and disable the transaction, e.g. a dedicated migration; Liquibase: `runInTransaction=false`).

### MG-008: RENAME of a table/column with live readers
detection: `ALTER TABLE ... RENAME TO` or `RENAME COLUMN`.
Why dangerous: the rename itself is fast, but the *old* application version still references the old
name during a rolling deploy → errors. (Also see MG-031.)
Safe: expand-contract — add the new name as a synonym (view/generated column), migrate readers, then drop.

### MG-009: VACUUM FULL / CLUSTER / REINDEX (non-concurrent) in a migration
detection: `VACUUM FULL`, `CLUSTER`, or `REINDEX` without `CONCURRENTLY`.
Why dangerous: rewrites the table/index under `ACCESS EXCLUSIVE`.
Safe: run out-of-band during a maintenance window, or use `REINDEX ... CONCURRENTLY` (PG12+); usually
should not live in a schema migration at all.

### MG-010: explicit LOCK TABLE
detection: `LOCK TABLE` statement.
Why dangerous: explicit heavy locks in a migration are almost always a mistake and serialize traffic.
Safe: rely on the minimal lock each DDL needs; if you truly need a lock, scope it and set
`lock_timeout` (MG-034).

### MG-011: ADD CONSTRAINT PRIMARY KEY / UNIQUE without USING INDEX
detection: `ALTER TABLE ... ADD [CONSTRAINT name] PRIMARY KEY (...)` or `... UNIQUE (...)`
without a `USING INDEX` clause.
Why dangerous: the constraint builds its underlying unique index inline, under an exclusive lock —
same write outage as MG-002, hidden inside a constraint.
Safe: two steps — `CREATE UNIQUE INDEX CONCURRENTLY uq_t_c ON t (c);` then
`ALTER TABLE t ADD CONSTRAINT pk_t PRIMARY KEY USING INDEX uq_t_c;` (the second step is fast).

### MG-015: ADD COLUMN ... GENERATED ALWAYS AS (...) STORED
detection: `ADD COLUMN` with a `GENERATED ALWAYS AS (...) STORED` expression.
Why dangerous: Postgres must compute and store the value for every existing row — a full table
rewrite under `ACCESS EXCLUSIVE`.
Safe: add a plain column and maintain it with a trigger or in application code, backfill in batches;
or accept the rewrite consciously in a maintenance window.

---

## Data loss (BLOCKER)

### MG-020: DROP TABLE / DROP COLUMN without a confirmation marker
detection: `DROP TABLE` or `ALTER TABLE ... DROP COLUMN` without a preceding
`-- migration-guard:confirmed-drop` comment.
Why dangerous: irreversible data loss; also breaks the old app version mid-deploy.
Safe: confirm intent with the marker comment, and prefer expand-contract (stop using the column,
deploy, then drop in a later migration).

### MG-021: TRUNCATE
detection: `TRUNCATE` statement.
Why dangerous: irreversible removal of all rows; not MVCC-friendly, takes `ACCESS EXCLUSIVE`.
Safe: if intentional, gate behind the confirmation marker and a maintenance window.

### MG-022: UPDATE / DELETE without WHERE
detection: `UPDATE` or `DELETE` with no `WHERE` clause.
Why dangerous: touches every row — data loss for `DELETE`, table-wide rewrite + long locks for `UPDATE`.
Safe: add a `WHERE`, and for large tables batch the operation (MG-032).

### MG-023: narrowing type change
detection: `ALTER COLUMN ... TYPE` to a narrower type (`bigint`→`int`, `text`→`varchar(n)`,
`numeric(p,s)`→smaller precision/scale, `timestamptz`→`date`).
Why dangerous: silent truncation / overflow → data loss, on top of a table rewrite.
Safe: keep the wider type, or migrate values explicitly with validation before narrowing.

---

## Zero-downtime / compatibility (CRITICAL)

### MG-030: NOT NULL column added while the old version still writes without it
detection: `ADD COLUMN ... NOT NULL DEFAULT ...` (constant default, so no rewrite on PG11+, but a
compatibility hazard during rolling deploys).
Why dangerous: safe lock-wise, but the *old* app version doesn't set the column; relying on the default
can mask bugs, and if there's no default the old version's inserts fail.
Safe: expand-contract — add nullable with a default, deploy the writer, backfill, then `SET NOT NULL`.

### MG-031: RENAME column/table during a rolling deploy
detection: same as MG-008 (`RENAME`), reported additionally under the compatibility lens.
Why dangerous: old and new app versions run simultaneously; one of them sees the wrong name.
Safe: expand-contract with a transitional period where both names resolve.

### MG-032: full-table DML backfill in a single statement
detection: an `UPDATE` whose target has no selective `WHERE` (or a `WHERE` that still matches the whole
table), used to populate a new column.
Why dangerous: one giant transaction holds row locks and bloats WAL; can stall replication.
Safe: batch by primary-key ranges with small commits (see zero-downtime-patterns.md).

### MG-033: DDL and heavy DML mixed in one transaction
detection: a schema-changing statement and a large `UPDATE`/`INSERT ... SELECT` in the same migration
without a transaction break.
Why dangerous: the DDL's exclusive lock is held for the entire duration of the DML.
Safe: split into separate migrations — schema change first, data movement second (batched).

### MG-034: dangerous migration without lock_timeout / statement_timeout
detection: a migration that contains any BLOCKER/CRITICAL lock rule but sets neither `lock_timeout`
nor `statement_timeout`.
Why dangerous: if the DDL can't get its lock immediately it queues behind long transactions and
blocks everything behind it (lock-queue pileup).
Safe: `SET lock_timeout = '5s';` (and often `SET statement_timeout`) at the top so the migration fails
fast instead of freezing the table.

### MG-036: ALTER TYPE ... ADD VALUE inside a transaction
detection: an enum `ALTER TYPE ... ADD VALUE` in a file that also contains explicit `BEGIN`/`COMMIT`
(or a transactional migration tool default).
Why dangerous: on PG < 12 this statement cannot run inside a transaction block — the migration fails
at deploy time; even on PG 12+ the new value can't be used in the same transaction.
Safe: put the `ADD VALUE` in its own non-transactional migration (Flyway: dedicated script with the
transaction disabled; Liquibase: `runInTransaction:false`).

---

## Hygiene (WARNING)

### MG-040: Flyway naming/numbering convention mismatch
detection: a `.sql` filename that doesn't match `V<version>__<description>.sql` (or `U`/`R` prefixes).
Why: Flyway won't pick up or order the migration as expected.
Safe: rename to the convention.

### MG-041: missing IF EXISTS / IF NOT EXISTS where appropriate
detection: `DROP ...` without `IF EXISTS`, or `CREATE TABLE/INDEX` without `IF NOT EXISTS` in contexts
where reruns are possible.
Why: makes migrations non-idempotent and reruns brittle.
Safe: add the guard clause.

### MG-042: index on an obviously low-selectivity column
detection: `CREATE INDEX` on a single `boolean` column.
Why: informational — such an index is rarely worth its write cost.
Safe: consider a partial index (`WHERE flag`) instead, or drop it.

### MG-043: CASCADE in DROP
detection: `DROP ... CASCADE`.
Why: silently drops dependent objects (views, FKs, columns) the author may not intend.
Safe: enumerate dependents and drop explicitly, or confirm the cascade is intended.
