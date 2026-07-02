# MySQL migration rules catalog

Shorter than Postgres for v1.0. Same rule ids where the hazard is identical; MySQL-specific
ids use the same MG numbers but the detection/safe text accounts for MySQL semantics
(online DDL, no transactional DDL).

Severities: BLOCKER / CRITICAL / WARNING (see rules-postgres.md).

---

## Locks / rewrites (BLOCKER)

### MG-001: ADD COLUMN that forces a table copy
detection: `ALTER TABLE ... ADD COLUMN` in a position that isn't `INSTANT`-eligible, or with a
default requiring evaluation on existing rows.
Why dangerous: pre-8.0.12 always copies the table; even on 8.0+ some adds fall back to `COPY`
algorithm, locking writes for the duration.
Safe: on MySQL 8.0.12+ use `ALGORITHM=INSTANT` where eligible; otherwise use `gh-ost`/`pt-osc`
for large tables (MG-050).

### MG-002: CREATE INDEX that copies the table
detection: `CREATE INDEX` / `ADD INDEX` where `ALGORITHM=INPLACE` isn't available.
Why dangerous: a `COPY`-algorithm index build locks the table for writes.
Safe: `ALTER TABLE ... ADD INDEX ..., ALGORITHM=INPLACE, LOCK=NONE`; for big tables, gh-ost/pt-osc.

### MG-004: ALTER COLUMN TYPE with a copy
detection: type change not supported as `INPLACE`/`INSTANT`.
Why dangerous: full table copy under lock.
Safe: expand-contract with a new column, or an online-DDL tool.

### MG-009: OPTIMIZE TABLE / ALTER ... FORCE
detection: `OPTIMIZE TABLE` or `ALTER TABLE ... FORCE`.
Why dangerous: rebuilds the table; on many engines locks writes.
Safe: run out-of-band or via an online-DDL tool.

---

## Data loss (BLOCKER)

### MG-020: DROP TABLE / DROP COLUMN without the confirmation marker
Same as Postgres MG-020. MySQL has no transactional DDL, so a failed multi-statement migration can
leave the schema half-changed — dropping is doubly risky.

### MG-021: TRUNCATE — same as Postgres MG-021.

### MG-022: UPDATE/DELETE without WHERE — same as Postgres MG-022.

### MG-023: narrowing type change — same as Postgres MG-023 (silent truncation).

---

## Compatibility / process (CRITICAL)

### MG-031: RENAME column/table during rolling deploy
Same hazard as Postgres MG-031. `CHANGE COLUMN`/`RENAME COLUMN` break the old app version.
Safe: expand-contract.

### MG-032: full-table backfill in one statement — same as Postgres MG-032; batch it.

### MG-033: multiple DDL statements in one migration file (no transactional rollback)
detection: more than one DDL statement in a single migration.
Why dangerous: MySQL auto-commits each DDL; a failure midway leaves earlier statements applied with
no automatic rollback.
Safe: one DDL per migration, or make each step independently idempotent.

### MG-050: large-table ALTER without an online-DDL tool
detection: any rewriting `ALTER` on a table the author flags as large, without gh-ost / pt-osc.
Why dangerous: even `INPLACE` can hold metadata locks and stall replicas.
Safe: use `gh-ost` or `pt-online-schema-change` for large tables.

---

## Hygiene (WARNING)

### MG-040: Flyway naming/numbering convention mismatch — same as Postgres.
### MG-041: missing IF EXISTS / IF NOT EXISTS — same as Postgres.
### MG-043: foreign-key CASCADE on DELETE/UPDATE added implicitly — confirm intent.
