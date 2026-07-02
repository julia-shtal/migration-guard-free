#!/usr/bin/env python3
"""migration-guard: static safety reviewer for SQL / Flyway / Liquibase migrations.

Detects patterns that are dangerous in production (blocking DDL, data loss, zero-downtime
incompatibilities) and reports severity + a safe alternative for each finding.

Dependency policy: sqlglot is the only third-party dependency. If a statement fails to parse,
a regex fallback still applies the most important rules, and the parse failure itself is reported
as a WARNING.

Usage:
    python check_migration.py <file|dir> [--dialect postgres|mysql]
                              [--format text|json|sarif] [--fail-on blocker|critical|warning]
Exit codes: 0 clean (or below threshold), 1 findings at/above --fail-on, 2 internal error.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from typing import Callable, Iterable

try:
    import logging as _logging

    import sqlglot
    from sqlglot import exp
    _logging.getLogger("sqlglot").setLevel(_logging.ERROR)
    _HAS_SQLGLOT = True
except Exception:  # pragma: no cover - environment without sqlglot
    _HAS_SQLGLOT = False


# --------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------

FREE_EDITION_RULES = {
    "MG-001",
    "MG-002",
    "MG-003",
    "MG-004",
    "MG-005",
    "MG-006",
    "MG-007",
    "MG-008",
    "MG-009",
    "MG-010",
    "MG-011",
    "MG-015",
    "MG-020",
    "MG-021",
    "MG-022",
    "MG-023",
    "MG-060",
    "MG-061",
    "MG-062",
}

def _edition_allows(rule_id):
    return rule_id in FREE_EDITION_RULES or rule_id.startswith('MG-PARSE') or rule_id == 'MG-IO'

SEVERITY_ORDER = {"WARNING": 1, "CRITICAL": 2, "BLOCKER": 3}


@dataclasses.dataclass
class Finding:
    file: str
    line: int
    rule_id: str
    severity: str
    message: str
    safe_alternative: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# Safe-alternative text keyed by rule id (kept in sync with references/rules-*.md).
SAFE: dict[str, str] = {
    "MG-001": "Add the column nullable (no volatile default), backfill in batches, then enforce NOT NULL via a validated CHECK (MG-006).",
    "MG-002": "Use CREATE INDEX CONCURRENTLY (in its own non-transactional migration).",
    "MG-003": "Use DROP INDEX CONCURRENTLY.",
    "MG-004": "Add a new column of the target type, backfill, and swap via expand-contract.",
    "MG-005": "Add the constraint with NOT VALID, then VALIDATE CONSTRAINT in a later migration.",
    "MG-006": "Add CHECK (col IS NOT NULL) NOT VALID, VALIDATE it, then SET NOT NULL (PG12+).",
    "MG-007": "Run CONCURRENTLY statements in their own migration with the transaction disabled.",
    "MG-008": "Use expand-contract: add the new name, migrate readers, then drop the old one.",
    "MG-009": "Run out-of-band in a maintenance window, or use REINDEX ... CONCURRENTLY (PG12+).",
    "MG-010": "Avoid explicit LOCK TABLE; rely on minimal DDL locks and set lock_timeout.",
    "MG-020": "Confirm intent with `-- migration-guard:confirmed-drop`, and prefer expand-contract.",
    "MG-021": "Gate TRUNCATE behind the confirmation marker and a maintenance window.",
    "MG-022": "Add a WHERE clause; for large tables, batch the operation.",
    "MG-023": "Keep the wider type, or migrate values explicitly with validation before narrowing.",
    "MG-030": "Expand-contract: add nullable with a default, deploy the writer, backfill, then SET NOT NULL.",
    "MG-031": "Use expand-contract with a transitional period where both names resolve.",
    "MG-032": "Batch the backfill by primary-key ranges with small commits.",
    "MG-033": "Split into separate migrations: schema change first, batched data movement second.",
    "MG-034": "Set lock_timeout (and often statement_timeout) at the top so the migration fails fast.",
    "MG-040": "Rename the file to the Flyway convention: V<version>__<description>.sql.",
    "MG-041": "Add IF EXISTS / IF NOT EXISTS to make the migration idempotent.",
    "MG-042": "Consider a partial index (WHERE flag) instead of indexing a boolean column.",
    "MG-043": "Enumerate dependents and drop explicitly, or confirm the cascade is intended.",
    "MG-050": "Use gh-ost or pt-online-schema-change for large-table ALTERs.",
    "MG-011": "Create a UNIQUE INDEX CONCURRENTLY first, then ADD CONSTRAINT ... PRIMARY KEY/UNIQUE USING INDEX.",
    "MG-015": "Add a plain column and maintain it via trigger/application code, or accept the rewrite in a maintenance window.",
    "MG-036": "Run ALTER TYPE ... ADD VALUE in its own non-transactional migration (required on PG < 12).",
    "MG-060": "Use CREATE INDEX ... ONLINE so DML can continue during the build (Oracle EE).",
    "MG-061": "Avoid ALTER TABLE ... MOVE in a migration; it rewrites the table and invalidates indexes. Run in a maintenance window and REBUILD indexes.",
    "MG-062": "Use ALTER TABLE ... SET UNUSED (instant), then DROP UNUSED COLUMNS later in a maintenance window.",
    "MG-063": "Oracle DDL auto-commits: keep one DDL per migration so a failure can't leave earlier statements applied without rollback.",
}

SEV: dict[str, str] = {
    "MG-001": "BLOCKER", "MG-002": "BLOCKER", "MG-003": "BLOCKER", "MG-004": "BLOCKER",
    "MG-005": "BLOCKER", "MG-006": "BLOCKER", "MG-007": "BLOCKER", "MG-008": "BLOCKER",
    "MG-009": "BLOCKER", "MG-010": "BLOCKER",
    "MG-020": "BLOCKER", "MG-021": "BLOCKER", "MG-022": "BLOCKER", "MG-023": "BLOCKER",
    "MG-030": "CRITICAL", "MG-031": "CRITICAL", "MG-032": "CRITICAL", "MG-033": "CRITICAL",
    "MG-034": "CRITICAL",
    "MG-040": "WARNING", "MG-041": "WARNING", "MG-042": "WARNING", "MG-043": "WARNING",
    "MG-050": "BLOCKER",
    "MG-011": "BLOCKER", "MG-015": "BLOCKER", "MG-036": "CRITICAL",
    "MG-060": "BLOCKER", "MG-061": "BLOCKER", "MG-062": "BLOCKER", "MG-063": "CRITICAL",
}

MSG: dict[str, str] = {
    "MG-001": "ADD COLUMN NOT NULL or with a volatile DEFAULT forces a full table rewrite under ACCESS EXCLUSIVE.",
    "MG-002": "CREATE INDEX without CONCURRENTLY blocks all writes to the table until the index is built.",
    "MG-003": "DROP INDEX without CONCURRENTLY takes an ACCESS EXCLUSIVE lock on the table.",
    "MG-004": "ALTER COLUMN TYPE with a rewriting conversion rewrites the whole table under ACCESS EXCLUSIVE.",
    "MG-005": "Adding a FOREIGN KEY/CHECK without NOT VALID scans and locks the table during validation.",
    "MG-006": "SET NOT NULL performs a full-table scan under ACCESS EXCLUSIVE.",
    "MG-007": "CONCURRENTLY cannot run inside a transaction; a failed build leaves an INVALID index.",
    "MG-008": "RENAME breaks the old application version during a rolling deploy.",
    "MG-009": "VACUUM FULL / CLUSTER / REINDEX (non-concurrent) rewrites under ACCESS EXCLUSIVE.",
    "MG-010": "Explicit LOCK TABLE serializes traffic and is almost always a mistake in a migration.",
    "MG-020": "DROP TABLE/COLUMN is irreversible data loss and breaks the old app version.",
    "MG-021": "TRUNCATE irreversibly removes all rows and takes ACCESS EXCLUSIVE.",
    "MG-022": "UPDATE/DELETE without WHERE touches every row (data loss / table-wide rewrite).",
    "MG-023": "Narrowing the column type can silently truncate or overflow values (data loss).",
    "MG-030": "Adding NOT NULL while the old app version still writes without it is a rolling-deploy hazard.",
    "MG-031": "Renaming during a rolling deploy makes one running version reference the wrong name.",
    "MG-032": "A single-statement full-table backfill holds locks, bloats WAL, and can stall replication.",
    "MG-033": "Mixing DDL with heavy DML holds the DDL's exclusive lock for the entire data movement.",
    "MG-034": "A locking migration without lock_timeout can pile up behind a long transaction and freeze the table.",
    "MG-040": "Filename does not match the Flyway convention V<version>__<description>.sql.",
    "MG-041": "Missing IF EXISTS / IF NOT EXISTS makes the migration non-idempotent.",
    "MG-042": "Indexing a boolean column is rarely worth its write cost.",
    "MG-043": "DROP ... CASCADE silently drops dependent objects.",
    "MG-050": "A rewriting ALTER on a large table without an online-DDL tool locks writes.",
    "MG-011": "ADD CONSTRAINT PRIMARY KEY/UNIQUE builds its index under an exclusive lock, blocking writes.",
    "MG-015": "Adding a GENERATED ... STORED column rewrites the whole table under ACCESS EXCLUSIVE.",
    "MG-036": "ALTER TYPE ... ADD VALUE cannot run inside a transaction on PG < 12 and the migration will fail.",
    "MG-060": "CREATE INDEX without ONLINE blocks DML on the table for the duration of the build (Oracle).",
    "MG-061": "ALTER TABLE ... MOVE rewrites the table and marks its indexes UNUSABLE (Oracle).",
    "MG-062": "DROP COLUMN physically rewrites every row and holds a long exclusive lock (Oracle).",
    "MG-063": "Multiple DDL statements in one Oracle migration: DDL auto-commits, so a mid-file failure leaves a half-applied schema.",
}

# Conversions that do NOT rewrite the table in Postgres.
_SAFE_TYPE_CHANGE = re.compile(
    r"\bTYPE\s+(text|varchar\s*\(\s*\d+\s*\)|character\s+varying)", re.I
)
_NARROWING = re.compile(
    r"\bTYPE\s+(int(eger)?|smallint|varchar\s*\(\s*\d+\s*\)|numeric\s*\(\s*\d+\s*,)", re.I
)
_VOLATILE_DEFAULT = re.compile(
    r"DEFAULT\s+(now\s*\(|current_timestamp|random\s*\(|uuid_generate|gen_random_uuid|clock_timestamp)",
    re.I,
)
_FLYWAY_NAME = re.compile(r"^[VUR]\d+([._]\d+)*__.+\.sql$", re.I)


# --------------------------------------------------------------------------------------
# Statement splitting + line tracking
# --------------------------------------------------------------------------------------

def _split_statements(sql: str) -> list[tuple[str, int]]:
    """Split on semicolons that aren't inside strings/comments. Returns (statement, line_no)."""
    statements: list[tuple[str, int]] = []
    buf: list[str] = []
    line = 1
    start_line = 1
    i = 0
    n = len(sql)
    in_line_comment = in_block_comment = in_squote = in_dquote = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append((stmt, start_line))
            buf = []
            start_line = line
            i += 1
            continue
        if not buf or "".join(buf).strip() == "":
            start_line = line
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append((tail, start_line))
    return statements


def _parse_suppressions(sql: str) -> set[str]:
    """Collect rule ids suppressed via `-- migration-guard:ignore MG-XXX reason`."""
    out: set[str] = set()
    for m in re.finditer(r"migration-guard:ignore\s+(MG-\d+)", sql, re.I):
        out.add(m.group(1).upper())
    return out


def _has_confirmed_drop(sql: str) -> bool:
    return bool(re.search(r"migration-guard:confirmed-drop", sql, re.I))


# --------------------------------------------------------------------------------------
# Rule checks (per statement) — sqlglot-aware where possible, regex fallback otherwise
# --------------------------------------------------------------------------------------

def _norm(sql: str) -> str:
    # strip comments for keyword scanning, collapse whitespace
    no_line = re.sub(r"--[^\n]*", " ", sql)
    no_block = re.sub(r"/\*.*?\*/", " ", no_line, flags=re.S)
    return re.sub(r"\s+", " ", no_block).strip()


def check_statement(stmt: str, dialect: str, whole_file: str) -> list[str]:
    """Return a list of rule ids fired for this statement."""
    s = _norm(stmt)
    up = s.upper()
    fired: list[str] = []

    is_alter = up.startswith("ALTER TABLE")
    is_create_index = bool(re.match(r"CREATE\s+(UNIQUE\s+)?INDEX", up))
    is_drop_index = up.startswith("DROP INDEX")
    concurrently = "CONCURRENTLY" in up

    # ---- Locks / rewrites ----
    if is_alter and re.search(r"ADD\s+COLUMN", up):
        not_null = "NOT NULL" in up
        has_default = "DEFAULT" in up
        volatile = bool(_VOLATILE_DEFAULT.search(s))
        if (not_null and not has_default) or volatile:
            fired.append("MG-001")
        elif not_null and has_default:
            fired.append("MG-030")

    if is_create_index and not concurrently:
        fired.append("MG-002")
    if is_drop_index and not concurrently:
        fired.append("MG-003")

    if is_alter and re.search(r"ALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE", up):
        if _NARROWING.search(s) and not _SAFE_TYPE_CHANGE.search(s):
            fired.append("MG-023")
        elif not _SAFE_TYPE_CHANGE.search(s):
            fired.append("MG-004")

    if is_alter and re.search(r"ADD\s+CONSTRAINT", up) and (
        "FOREIGN KEY" in up or ("CHECK" in up and "PRIMARY KEY" not in up and "UNIQUE" not in up)
    ) and "NOT VALID" not in up:
        fired.append("MG-005")

    # MG-011: PK/UNIQUE constraint that builds its index under lock
    if is_alter and re.search(r"ADD\s+(CONSTRAINT\s+\w+\s+)?(PRIMARY\s+KEY|UNIQUE)", up) \
            and "USING INDEX" not in up:
        fired.append("MG-011")

    # MG-015: stored generated column forces a rewrite
    if is_alter and re.search(r"ADD\s+COLUMN", up) and re.search(
            r"GENERATED\s+ALWAYS\s+AS\s*\(.*\)\s*STORED", up):
        fired.append("MG-015")

    # MG-036: enum ADD VALUE inside a transaction (explicit BEGIN, or noted transactional tool)
    if re.match(r"ALTER\s+TYPE\s+\w+.*ADD\s+VALUE", up) and re.search(
            r"\bBEGIN\b|\bCOMMIT\b", whole_file.upper()):
        fired.append("MG-036")

    if is_alter and re.search(r"ALTER\s+COLUMN\s+\w+\s+SET\s+NOT\s+NULL", up):
        fired.append("MG-006")

    if concurrently and re.search(r"\bBEGIN\b|\bCOMMIT\b", whole_file.upper()):
        fired.append("MG-007")

    if is_alter and re.search(r"RENAME\s+(COLUMN\s+\w+\s+)?TO|RENAME\s+TO", up):
        fired.append("MG-008")
        fired.append("MG-031")

    if re.match(r"(VACUUM\s+FULL|CLUSTER|REINDEX)", up) and not concurrently:
        fired.append("MG-009")
    if up.startswith("LOCK TABLE") or re.match(r"LOCK\s+\w", up):
        fired.append("MG-010")

    # ---- Data loss ----
    if up.startswith("DROP TABLE") or (is_alter and "DROP COLUMN" in up):
        if not _has_confirmed_drop(whole_file):
            fired.append("MG-020")
    if up.startswith("TRUNCATE"):
        fired.append("MG-021")
    if re.match(r"(UPDATE|DELETE)\b", up) and " WHERE " not in f" {up} ":
        fired.append("MG-022")

    # ---- Compatibility ----
    # MG-032: UPDATE that fills a column across the table (has WHERE col IS NULL only)
    if up.startswith("UPDATE") and " WHERE " in f" {up} ":
        where_part = up.split(" WHERE ", 1)[1]
        if re.match(r"\s*\w+\s+IS\s+NULL\s*$", where_part) or where_part.strip() == "":
            fired.append("MG-032")

    # ---- MySQL-specific ----
    if dialect == "mysql":
        if is_alter and "ADD INDEX" in up and "ALGORITHM=INPLACE" not in up.replace(" ", ""):
            fired.append("MG-002")

    # ---- Oracle-specific ----
    if dialect == "oracle":
        # Postgres' CONCURRENTLY wording never applies to Oracle
        while "MG-002" in fired:
            fired.remove("MG-002")
        while "MG-003" in fired:
            fired.remove("MG-003")
        if is_create_index and "ONLINE" not in up:
            fired.append("MG-060")
        if is_alter and re.search(r"\bMOVE\b", up):
            fired.append("MG-061")
        if is_alter and "DROP COLUMN" in up and "SET UNUSED" not in up:
            fired.append("MG-062")

    # ---- Hygiene ----
    if up.startswith("DROP ") and "IF EXISTS" not in up:
        fired.append("MG-041")
    if re.match(r"CREATE\s+TABLE", up) and "IF NOT EXISTS" not in up:
        fired.append("MG-041")
    if "CASCADE" in up and up.startswith("DROP"):
        fired.append("MG-043")
    if is_create_index and re.search(r"\(\s*\w+\s*\)", s):
        # naive boolean-column heuristic: column literally named like a flag
        if re.search(r"\(\s*(is_\w+|has_\w+|\w*_flag|enabled|active|deleted)\s*\)", s, re.I):
            fired.append("MG-042")

    return fired


# --------------------------------------------------------------------------------------
# File-level checks
# --------------------------------------------------------------------------------------

def _file_level_checks(path: str, sql: str, per_stmt_fired: list[str],
                       dialect: str = "postgres") -> list[str]:
    fired: list[str] = []
    base = os.path.basename(path)
    if base.lower().endswith(".sql") and not _FLYWAY_NAME.match(base):
        # only flag Flyway-style dirs; skip obvious non-Flyway names like schema.sql seeds
        if re.match(r"^[VUR]", base) or "migration" in path.lower():
            fired.append("MG-040")

    up = sql.upper()
    # MG-033: DDL + heavy DML in the same file
    has_ddl = bool(re.search(r"\b(ALTER TABLE|CREATE TABLE|CREATE INDEX|DROP)\b", up))
    has_heavy_dml = bool(re.search(r"\b(UPDATE|INSERT\s+INTO\s+\w+\s+SELECT|DELETE)\b", up))
    if has_ddl and has_heavy_dml:
        fired.append("MG-033")

    # MG-063 (Oracle): more than one DDL statement in a single migration (DDL auto-commits)
    if dialect == "oracle":
        ddl_count = len(re.findall(
            r"\b(ALTER\s+TABLE|CREATE\s+TABLE|CREATE\s+(UNIQUE\s+)?INDEX|DROP\s+TABLE|DROP\s+INDEX|TRUNCATE)\b",
            up))
        if ddl_count > 1:
            fired.append("MG-063")

    # MG-034: locking rule present but no lock_timeout (Postgres concept; skip for oracle)
    locking = {"MG-001", "MG-002", "MG-003", "MG-004", "MG-005", "MG-006",
               "MG-008", "MG-009", "MG-011", "MG-015", "MG-023"}
    if dialect != "oracle" and set(per_stmt_fired) & locking and "LOCK_TIMEOUT" not in up:
        fired.append("MG-034")
    return fired


# --------------------------------------------------------------------------------------
# Liquibase support
# --------------------------------------------------------------------------------------

def _extract_liquibase_sql(text: str) -> str:
    """Pull raw SQL out of Liquibase <sql> / sql: blocks and synthesize SQL for common tags."""
    fragments: list[str] = []
    for m in re.finditer(r"<sql>(.*?)</sql>", text, re.S | re.I):
        fragments.append(m.group(1))
    for m in re.finditer(r"^\s*sql:\s*(.+)$", text, re.M | re.I):
        fragments.append(m.group(1))
    # map common structured tags to synthetic SQL so the SQL rules fire
    if re.search(r"<createIndex|createIndex:", text, re.I):
        fragments.append("CREATE INDEX idx_x ON t (c);")
    if re.search(r"<dropIndex|dropIndex:", text, re.I):
        fragments.append("DROP INDEX idx_x;")
    if re.search(r'nullable="false"', text, re.I) and re.search(r"<addColumn|addColumn:", text, re.I):
        fragments.append("ALTER TABLE t ADD COLUMN c text NOT NULL;")
    if re.search(r"<renameColumn|renameColumn:|<renameTable|renameTable:", text, re.I):
        fragments.append("ALTER TABLE t RENAME COLUMN a TO b;")
    if re.search(r"<dropTable|dropTable:|<dropColumn|dropColumn:", text, re.I):
        fragments.append("DROP TABLE t;")
    return "\n".join(fragments)


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def analyze_text(path: str, text: str, dialect: str) -> list[Finding]:
    suppressed = _parse_suppressions(text)
    is_liquibase = path.lower().endswith((".xml", ".yaml", ".yml"))
    sql = _extract_liquibase_sql(text) if is_liquibase else text

    findings: list[Finding] = []
    all_fired: list[str] = []

    # parse-failure warning (only for plain SQL; sqlglot present)
    statements = _split_statements(sql)
    for stmt, line in statements:
        if _HAS_SQLGLOT and not is_liquibase:
            try:
                sqlglot.parse_one(stmt, read=dialect if dialect != "postgres" else "postgres")
            except Exception:
                findings.append(Finding(
                    path, line, "MG-PARSE", "WARNING",
                    "Statement could not be parsed; only regex-level rules were applied.",
                    "Check the SQL syntax; broken statements can hide other issues.",
                ))
        fired = check_statement(stmt, dialect, sql)
        for rid in fired:
            if rid in suppressed:
                continue
            if not _edition_allows(rid):
                continue
            all_fired.append(rid)
            findings.append(Finding(
                path, line, rid, SEV[rid], MSG[rid], SAFE[rid],
            ))

    for rid in _file_level_checks(path, sql, all_fired, dialect):
        if rid in suppressed:
            continue
        findings.append(Finding(
            path, 1, rid, SEV[rid], MSG[rid], SAFE[rid],
        ))

    # de-dup identical (line, rule) pairs, keep order
    seen: set[tuple[int, str]] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (f.line, f.rule_id)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _iter_files(target: str) -> Iterable[str]:
    if os.path.isfile(target):
        yield target
        return
    for root, _dirs, files in os.walk(target):
        for name in sorted(files):
            if name.lower().endswith((".sql", ".xml", ".yaml", ".yml")):
                yield os.path.join(root, name)


def analyze_path(target: str, dialect: str) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files(target):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:  # pragma: no cover
            findings.append(Finding(path, 1, "MG-IO", "WARNING",
                                    f"Could not read file: {exc}", "Check file permissions."))
            continue
        findings.extend(analyze_text(path, text, dialect))
    return findings


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def render_text(findings: list[Finding]) -> str:
    if not findings:
        return "migration-guard: no issues found.\n"
    order = {"BLOCKER": 0, "CRITICAL": 1, "WARNING": 2}
    findings = sorted(findings, key=lambda f: (order.get(f.severity, 3), f.file, f.line))
    lines: list[str] = []
    current_sev = None
    for f in findings:
        if f.severity != current_sev:
            current_sev = f.severity
            lines.append(f"\n=== {current_sev} ===")
        loc = f"{os.path.basename(f.file)}:{f.line}"
        lines.append(f"[{f.rule_id}] {loc}")
        lines.append(f"    {f.message}")
        lines.append(f"    fix: {f.safe_alternative}")
    counts = _counts(findings)
    lines.append(
        f"\nSummary: {counts['BLOCKER']} blocker, "
        f"{counts['CRITICAL']} critical, {counts['WARNING']} warning."
    )
    return "\n".join(lines) + "\n"


def render_json(findings: list[Finding]) -> str:
    return json.dumps(
        {"findings": [f.as_dict() for f in findings], "summary": _counts(findings)},
        indent=2,
    )


def render_sarif(findings: list[Finding]) -> str:
    rules = {}
    results = []
    sarif_level = {"BLOCKER": "error", "CRITICAL": "error", "WARNING": "warning"}
    for f in findings:
        rules.setdefault(f.rule_id, {
            "id": f.rule_id,
            "shortDescription": {"text": f.message},
            "helpUri": "https://github.com/julia-shtal/migration-guard-free/blob/main/references/rules-postgres.md#" + f.rule_id.lower(),
        })
        results.append({
            "ruleId": f.rule_id,
            "level": sarif_level.get(f.severity, "note"),
            "message": {"text": f"{f.message} Fix: {f.safe_alternative}"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file},
                    "region": {"startLine": max(f.line, 1)},
                }
            }],
        })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "migration-guard",
                "informationUri": "https://github.com/julia-shtal/migration-guard-free/blob/main/references/rules-postgres.md",
                "rules": list(rules.values()),
            }},
            "results": results,
        }],
    }
    return json.dumps(doc, indent=2)


def _counts(findings: list[Finding]) -> dict:
    c = {"BLOCKER": 0, "CRITICAL": 0, "WARNING": 0}
    for f in findings:
        c[f.severity] = c.get(f.severity, 0) + 1
    return c


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def _max_severity(findings: list[Finding]) -> int:
    return max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static safety reviewer for DB migrations.")
    parser.add_argument("target", help="migration file or directory")
    parser.add_argument("--dialect", choices=["postgres", "mysql", "oracle"], default="postgres")
    parser.add_argument("--format", choices=["text"], default="text")
    parser.add_argument("--fail-on", choices=["blocker", "critical", "warning"], default="blocker")
    args = parser.parse_args(argv)

    if not os.path.exists(args.target):
        print(f"error: path not found: {args.target}", file=sys.stderr)
        return 2

    try:
        findings = analyze_path(args.target, args.dialect)
    except Exception as exc:  # pragma: no cover
        print(f"internal error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(render_json(findings))
    elif args.format == "sarif":
        print(render_sarif(findings))
    else:
        print(render_text(findings), end="")

    threshold = SEVERITY_ORDER[args.fail_on.upper()]
    return 1 if _max_severity(findings) >= threshold else 0


if __name__ == "__main__":
    raise SystemExit(main())
