# Liquibase notes: changeSet → SQL semantics

`check_migration.py` handles Liquibase `.xml` and `.yaml`/`.yml` changelogs by:

1. Extracting raw SQL from `<sql>` / `sql:` blocks and running the normal SQL rules on it.
2. Mapping common structured changeSet tags to the same rule ids (below), since the tag implies
   a known SQL operation.

## Tag → rule mapping

| Liquibase construct | Implied operation | Rule |
|---|---|---|
| `createIndex` (no `runInTransaction:false`) | `CREATE INDEX` (transactional) | MG-002 / MG-007 |
| `dropIndex` | `DROP INDEX` | MG-003 |
| `addColumn` with `constraints nullable="false"` and no `defaultValue` | `ADD COLUMN NOT NULL` | MG-001 / MG-030 |
| `modifyDataType` | `ALTER COLUMN TYPE` | MG-004 / MG-023 |
| `addNotNullConstraint` | `SET NOT NULL` | MG-006 |
| `addForeignKeyConstraint` / `addCheckConstraint` (no `validate:false`) | validated constraint | MG-005 |
| `renameColumn` / `renameTable` | `RENAME` | MG-008 / MG-031 |
| `dropTable` / `dropColumn` | destructive DDL | MG-020 |
| `update` / `delete` without `where` | table-wide DML | MG-022 |

## Suppression

The SQL-comment suppression (`-- migration-guard:ignore MG-XXX reason`) also works inside `<sql>`
blocks. For structured tags, a Liquibase `<comment>` / `comment:` field containing
`migration-guard:ignore MG-XXX` suppresses that rule for the changeSet.

## Limitations (documented, not silent)

- `runInTransaction` and `validate` attributes are read when present; if absent, the analyzer assumes
  the Liquibase default (transactional true, validate true) and reports accordingly.
- Preconditions and rollback blocks are not analyzed in v1.0 — noted in the report footer.
