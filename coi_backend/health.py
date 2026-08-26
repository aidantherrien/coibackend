"""Deployment checks that fail closed for automation."""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from .migrations import pending_migrations

EXPECTED_TABLES = {
    "document_artifacts",
    "document_sources",
    "documents",
    "invoice_line_items",
    "invoice_summary",
    "oa_line_items",
    "oa_summary",
    "parse_attempts",
    "schema_migrations",
}

EXPECTED_VIEWS = {
    "document_review_queue",
    "po_document_overview",
    "po_product_reconciliation",
}

REQUIRED_COLUMNS = {
    "documents": {
        "document_id",
        "sha256",
        "document_type",
        "vendor",
        "artifact_backend",
        "storage_account_name",
        "storage_container",
        "blob_name",
        "blob_version_id",
        "status",
        "review_status",
    },
    "document_artifacts": {
        "document_artifact_id",
        "document_id",
        "artifact_kind",
        "artifact_backend",
        "storage_account_name",
        "storage_container",
        "blob_name",
        "blob_version_id",
        "sha256",
        "content_length_bytes",
    },
    "document_sources": {
        "document_id",
        "source_kind",
        "source_reference",
        "review_status",
        "error_code",
        "metadata",
    },
    "parse_attempts": {
        "parse_attempt_id",
        "document_id",
        "provider_job_id",
        "result_artifact_backend",
        "result_storage_account_name",
        "result_container",
        "result_blob_name",
        "result_blob_version_id",
        "status",
        "metadata",
    },
    "invoice_summary": {"invoice_id", "document_id", "vendor", "invoice_no", "po"},
    "invoice_line_items": {"invoice_id", "line_position", "source_line_no"},
    "oa_summary": {"oa_id", "document_id", "vendor", "order_no", "po"},
    "oa_line_items": {"oa_id", "line_position", "source_line_no"},
    "schema_migrations": {"version", "checksum", "applied_at"},
}


def check_database(connection: Connection[dict[str, Any]]) -> tuple[str, ...]:
    pending = pending_migrations(connection)
    if pending:
        versions = ", ".join(migration.version for migration in pending)
        raise RuntimeError(f"pending database migrations: {versions}")

    rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'coi'
        """
    ).fetchall()
    found = {str(row["table_name"]) for row in rows}
    missing = sorted(EXPECTED_TABLES - found)
    if missing:
        raise RuntimeError("missing coi tables: " + ", ".join(missing))

    view_rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.views
        WHERE table_schema = 'coi'
        """
    ).fetchall()
    found_views = {str(row["table_name"]) for row in view_rows}
    missing_views = sorted(EXPECTED_VIEWS - found_views)
    if missing_views:
        raise RuntimeError("missing coi views: " + ", ".join(missing_views))

    column_rows = connection.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'coi'
          AND table_name = ANY(%s)
        """,
        (list(REQUIRED_COLUMNS),),
    ).fetchall()
    found_columns: dict[str, set[str]] = {table: set() for table in REQUIRED_COLUMNS}
    for row in column_rows:
        found_columns[str(row["table_name"])].add(str(row["column_name"]))
    missing_columns = [
        f"{table}.{column}"
        for table, required in REQUIRED_COLUMNS.items()
        for column in sorted(required - found_columns[table])
    ]
    if missing_columns:
        raise RuntimeError("missing required coi columns: " + ", ".join(missing_columns))
    return tuple(sorted(found))
