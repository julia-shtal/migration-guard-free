# Oracle migration rules catalog

Oracle-specific hazards (`--dialect oracle`). Shared rules (data loss MG-020..MG-023, rename
compatibility MG-031, backfill MG-032) apply exactly as in rules-postgres.md; this file covers
what is different in Oracle.

Key Oracle facts driving these rules:
- **DDL auto-commits.** There is no transactional DDL: every DDL implicitly commits, and a failed
  multi-statement migration leaves the schema half-applied with nothing to roll back.
- **Locks are heavy by default.** Plain `CREATE INDEX` and `DROP COLUMN` block DML for their full
  duration; the online variants (`ONLINE`, `SET UNUSED`) exist precisely to avoid that.

---

## Locks / rewrites (BLOCKER)

### MG-060: CREATE INDEX without ONLINE
detection: `CREATE [UNIQUE] INDEX` without the `ONLINE` keyword.
Why dangerous: the default index build takes a table lock that blocks all DML until the build
finishes — on a large table that's a write outage.
Safe: `CREATE INDEX ... ONLINE` (Enterprise Edition). On Standard Edition, schedule a maintenance
window or build on a quiet replica strategy.

### MG-061: ALTER TABLE ... MOVE
detection: `ALTER TABLE ... MOVE [TABLESPACE ...]`.
Why dangerous: physically rewrites the table AND marks every index on it `UNUSABLE` — queries start
failing or full-scanning until you `REBUILD` each index.
Safe: run in a maintenance window with an explicit index `REBUILD ... ONLINE` plan afterwards, or use
`DBMS_REDEFINITION` for an online reorganization. This does not belong in a routine migration.

### MG-062: DROP COLUMN instead of SET UNUSED
detection: `ALTER TABLE ... DROP COLUMN` (without a preceding `SET UNUSED` strategy).
Why dangerous: physically rewrites every row to remove the column, holding a long exclusive lock.
Safe: `ALTER TABLE t SET UNUSED COLUMN c;` — instant, metadata-only; the column disappears logically.
Physically reclaim later with `ALTER TABLE t DROP UNUSED COLUMNS CHECKPOINT 5000;` in a maintenance
window. The confirmation-marker requirement (MG-020) still applies: dropping data needs
`-- migration-guard:confirmed-drop`.

### MG-001 (Oracle variant): ADD COLUMN ... NOT NULL without DEFAULT
Same id as Postgres; on Oracle, adding NOT NULL without a default requires validating every row.
With a constant DEFAULT on 11g+ it's a fast metadata-only operation — that's the safe form.

---

## Process / compatibility (CRITICAL)

### MG-063: multiple DDL statements in one migration
detection: more than one DDL statement in a single migration file.
Why dangerous: each DDL auto-commits. If statement 3 of 5 fails, statements 1–2 are permanently
applied, the migration is marked failed, and a re-run will crash on the already-applied statements.
Safe: one DDL per migration file, or make every statement independently idempotent (guard with
checks against `user_tab_columns` / `user_indexes` in PL/SQL blocks).

### MG-031: RENAME during rolling deploy — same as Postgres (old app version breaks).

---

## Notes for the SKILL.md workflow

- Oracle has no `lock_timeout` equivalent in-script (MG-034 is skipped); the closest control is
  `DDL_LOCK_TIMEOUT` at session level — suggest `ALTER SESSION SET DDL_LOCK_TIMEOUT = 5;` when a
  locking rule fires.
- For large-table restructuring, the online answer is `DBMS_REDEFINITION`; mention it whenever the
  user hits MG-061/MG-004-class changes on big tables.
