"""Small transactional SQL migration runner with checksum drift detection."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg import sql as psycopg_sql

from .config import PROJECT_ROOT

SOURCE_MIGRATIONS_DIR = PROJECT_ROOT / "sql" / "migrations"
INSTALLED_MIGRATIONS_DIR = Path(sys.prefix) / "share" / "coi-backend" / "sql" / "migrations"
MIGRATIONS_DIR = (
    SOURCE_MIGRATIONS_DIR if SOURCE_MIGRATIONS_DIR.is_dir() else INSTALLED_MIGRATIONS_DIR
)
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """Raised for migration ordering, checksum, or application failures."""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str
    sql: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    if not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"migration filename must match NNNN_name.sql: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=match.group(1),
                path=path,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    versions = [migration.version for migration in migrations]
    if not migrations:
        raise MigrationError("no SQL migrations were found")
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration versions were found")
    return tuple(migrations)


def _ensure_ledger(connection: Connection[dict[str, Any]]) -> None:
    legacy = connection.execute(
        "SELECT to_regclass('public.invoice_summary') AS legacy_table"
    ).fetchone()
    ledger = connection.execute("SELECT to_regclass('coi.schema_migrations') AS ledger").fetchone()
    if legacy and legacy["legacy_table"] and not (ledger and ledger["ledger"]):
        raise MigrationError(
            "legacy public.invoice_summary exists without a migration ledger; "
            "back it up and follow docs/database-migration.md instead of baselining silently"
        )

    connection.execute("CREATE SCHEMA IF NOT EXISTS coi")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS coi.schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(connection: Connection[dict[str, Any]]) -> dict[str, str]:
    ledger = connection.execute("SELECT to_regclass('coi.schema_migrations') AS ledger").fetchone()
    if not ledger or not ledger["ledger"]:
        raise MigrationError("database has no migration ledger; run the migrate command")
    rows = connection.execute(
        "SELECT version, checksum FROM coi.schema_migrations ORDER BY version"
    ).fetchall()
    return {str(row["version"]): str(row["checksum"]) for row in rows}


def pending_migrations(
    connection: Connection[dict[str, Any]],
    directory: Path = MIGRATIONS_DIR,
) -> tuple[Migration, ...]:
    applied = applied_migrations(connection)
    migrations = discover_migrations(directory)
    for migration in migrations:
        previous_checksum = applied.get(migration.version)
        if previous_checksum and previous_checksum != migration.checksum:
            raise MigrationError(
                f"applied migration {migration.version} has been modified; "
                "create a new migration instead"
            )
    unknown = sorted(set(applied) - {migration.version for migration in migrations})
    if unknown:
        raise MigrationError(
            "database contains migration versions absent from the repository: " + ", ".join(unknown)
        )
    return tuple(m for m in migrations if m.version not in applied)


def apply_migrations(
    connection: Connection[dict[str, Any]],
    directory: Path = MIGRATIONS_DIR,
) -> tuple[str, ...]:
    migration_role = os.getenv("DATABASE_MIGRATION_ROLE", "").strip()
    if migration_role:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", migration_role):
            raise MigrationError("DATABASE_MIGRATION_ROLE is not a valid SQL role name")
        connection.execute(
            psycopg_sql.SQL("SET ROLE {}").format(psycopg_sql.Identifier(migration_role))
        )
    connection.execute("SELECT pg_advisory_lock(hashtext('coi-schema-migrations'))")
    applied_now: list[str] = []
    try:
        _ensure_ledger(connection)
        for migration in pending_migrations(connection, directory):
            with connection.transaction():
                connection.execute(migration.sql)
                connection.execute(
                    """
                    INSERT INTO coi.schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (migration.version, migration.checksum),
                )
            applied_now.append(migration.version)
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtext('coi-schema-migrations'))")
        if migration_role:
            connection.execute("RESET ROLE")
    return tuple(applied_now)
