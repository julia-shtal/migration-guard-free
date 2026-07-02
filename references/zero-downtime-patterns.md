# Zero-downtime migration patterns

Reference read by the SKILL.md workflow when a CRITICAL rule fires and the user needs a rewritten,
safe, multi-step migration. Each pattern is expressed as an ordered set of Flyway-style migrations.

## Expand-contract (the core pattern)

Never change a column in place while old and new app versions run together. Instead:

1. **Expand** — add the new shape alongside the old one (new nullable column, new table, new index).
   Both app versions keep working.
2. **Migrate** — deploy the app version that writes to (and reads from) the new shape.
   Backfill historical data in batches.
3. **Contract** — once no code references the old shape, drop it in a later migration.

Each step is a separate migration and a separate deploy. This is the answer to MG-004, MG-008,
MG-023, MG-030, MG-031.

## Add a NOT NULL column safely (answer to MG-001 / MG-006 / MG-030)

```
-- V10__add_status_nullable.sql
ALTER TABLE orders ADD COLUMN status text;               -- fast, metadata only
ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'new';

-- V11__backfill_status.sql   (batched, see below)

-- V12__enforce_status_not_null.sql   (PG12+)
SET lock_timeout = '5s';
ALTER TABLE orders ADD CONSTRAINT status_not_null CHECK (status IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT status_not_null;  -- SHARE UPDATE EXCLUSIVE, no full lock
ALTER TABLE orders ALTER COLUMN status SET NOT NULL;     -- reuses the validated constraint
ALTER TABLE orders DROP CONSTRAINT status_not_null;
```

## Batched backfill (answer to MG-022 / MG-032)

Do not `UPDATE` a whole large table in one statement. Loop over primary-key ranges with small commits:

```sql
-- pseudo-batches; run outside a single wrapping transaction
UPDATE orders SET status = 'new'
WHERE status IS NULL AND id BETWEEN :lo AND :lo + 5000;
-- repeat, advancing :lo, until no rows remain
```

For Flyway, a batched backfill is usually a callback or an application-side job rather than one SQL
file, because each batch should commit independently.

## Concurrent index (answer to MG-002 / MG-007)

```
-- V20__add_email_index.sql
-- must NOT run inside a transaction
CREATE INDEX CONCURRENTLY idx_users_email ON users (email);
```

Flyway: mark the script so it isn't wrapped in a transaction. Liquibase: `runInTransaction:false`.
If a concurrent build fails it leaves an `INVALID` index — drop it (`DROP INDEX CONCURRENTLY`) and retry.

## Validate a constraint in two steps (answer to MG-005)

```
-- V30__fk_not_valid.sql
ALTER TABLE orders ADD CONSTRAINT fk_customer
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

-- V31__fk_validate.sql
ALTER TABLE orders VALIDATE CONSTRAINT fk_customer;   -- no full write lock
```

## Always fail fast (answer to MG-034)

Put this at the top of any migration that takes a table-level lock, so it queues briefly and aborts
rather than freezing the table behind a long transaction:

```sql
SET lock_timeout = '5s';
SET statement_timeout = '60s';
```
