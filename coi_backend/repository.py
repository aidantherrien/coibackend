"""PostgreSQL persistence for ingestion state and typed business records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .mapping import InvoiceRecord, OaRecord
from .storage import ArtifactLocation

StoreOutcome = Literal["stored", "duplicate", "business_key_conflict"]


@dataclass(frozen=True)
class DocumentRecord:
    document_id: int
    sha256: str
    document_type: str
    vendor: str
    status: str
    review_status: str
    created: bool
    content_length_bytes: int | None = None
    artifact_backend: str | None = None
    storage_account_name: str | None = None
    storage_container: str | None = None
    blob_name: str | None = None
    blob_version_id: str | None = None


@dataclass(frozen=True)
class StoreResult:
    outcome: StoreOutcome
    parent_id: int | None = None
    conflicting_document_id: int | None = None
    line_count: int = 0


@dataclass(frozen=True)
class ReusableParse:
    parse_attempt_id: int
    provider_job_id: str | None
    result_location: ArtifactLocation | None


def connect(database_url: str, *, timeout_seconds: int) -> Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url,
        connect_timeout=timeout_seconds,
        autocommit=True,
        row_factory=dict_row,
        application_name="coi-document-ingestion",
    )


class DocumentRepository:
    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def acquire_run_lock(self, *, document_type: str, vendor: str) -> bool:
        lock_name = f"coi-ingest:{vendor}:{document_type}"
        row = self.connection.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (lock_name,)
        ).fetchone()
        return bool(row and row["acquired"])

    def release_run_lock(self, *, document_type: str, vendor: str) -> None:
        lock_name = f"coi-ingest:{vendor}:{document_type}"
        self.connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_name,))

    def register_document(
        self,
        *,
        sha256: str,
        filename: str,
        content_length_bytes: int,
        document_type: str,
        vendor: str,
    ) -> DocumentRecord:
        row = self.connection.execute(
            """
            INSERT INTO coi.documents
                (sha256, original_filename, content_length_bytes, document_type,
                 vendor, status, review_status)
            VALUES (%s, %s, %s, %s, %s, 'discovered', 'pending')
            ON CONFLICT (sha256) DO NOTHING
            RETURNING document_id, sha256, document_type, vendor, status, review_status,
                      content_length_bytes, artifact_backend, storage_account_name,
                      storage_container, blob_name, blob_version_id
            """,
            (
                sha256,
                Path(filename).name,
                content_length_bytes,
                document_type,
                vendor,
            ),
        ).fetchone()
        if row:
            return DocumentRecord(**row, created=True)

        existing = self.connection.execute(
            """
            SELECT document_id, sha256, document_type, vendor, status, review_status,
                   content_length_bytes, artifact_backend, storage_account_name,
                   storage_container, blob_name, blob_version_id
            FROM coi.documents
            WHERE sha256 = %s
            """,
            (sha256,),
        ).fetchone()
        if not existing:  # pragma: no cover - defensive against external deletion
            raise RuntimeError("document disappeared after a hash conflict")
        return DocumentRecord(**existing, created=False)

    def add_local_source(
        self,
        *,
        document_id: int,
        source_reference: str,
        source_filename: str,
        observed_document_type: str,
        observed_vendor: str,
    ) -> int:
        row = self.connection.execute(
            """
            INSERT INTO coi.document_sources
                (document_id, source_kind, source_reference, source_filename, metadata)
            VALUES (%s, 'local_file', %s, %s, %s)
            ON CONFLICT (source_kind, source_reference) DO NOTHING
            RETURNING document_source_id
            """,
            (
                document_id,
                source_reference,
                Path(source_filename).name,
                Jsonb(
                    {
                        "observed_document_type": observed_document_type,
                        "observed_vendor": observed_vendor,
                    }
                ),
            ),
        ).fetchone()
        if row:
            return int(row["document_source_id"])
        existing = self.connection.execute(
            """
            SELECT document_source_id, document_id
            FROM coi.document_sources
            WHERE source_kind = 'local_file' AND source_reference = %s
            """,
            (source_reference,),
        ).fetchone()
        if not existing or int(existing["document_id"]) != document_id:
            raise RuntimeError("source occurrence conflict could not be resolved safely")
        return int(existing["document_source_id"])

    def mark_source_needs_review(
        self,
        *,
        document_source_id: int,
        error_code: str,
        error_message: str,
    ) -> None:
        row = self.connection.execute(
            """
            UPDATE coi.document_sources
            SET review_status = 'needs_review',
                error_code = %s,
                error_message = %s
            WHERE document_source_id = %s
            RETURNING document_source_id
            """,
            (error_code, error_message.replace("\n", " ")[:2000], document_source_id),
        ).fetchone()
        if not row:
            raise RuntimeError("source occurrence disappeared while recording review evidence")

    def try_claim(
        self,
        *,
        document_id: int,
        force_retry: bool,
        stale_after_seconds: int,
    ) -> bool:
        row = self.connection.execute(
            """
            WITH candidate AS (
                SELECT document_id, status
                FROM coi.documents
                WHERE document_id = %s
                  AND (
                      status = 'discovered'
                      OR (%s AND status IN ('failed', 'needs_review'))
                      OR (
                          %s
                          AND status = 'processing'
                          AND updated_at < now() - make_interval(secs => %s)
                      )
                  )
                FOR UPDATE
            ), stale_attempts AS (
                UPDATE coi.parse_attempts AS attempt
                SET status = 'failed',
                    error_code = 'stale_attempt_retried',
                    error_message = 'A forced retry reclaimed a stale processing document.',
                    completed_at = now()
                FROM candidate
                WHERE attempt.document_id = candidate.document_id
                  AND candidate.status = 'processing'
                  AND attempt.status IN ('started', 'processing')
            )
            UPDATE coi.documents
            SET status = 'processing',
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            FROM candidate
            WHERE coi.documents.document_id = candidate.document_id
            RETURNING coi.documents.document_id
            """,
            (document_id, force_retry, force_retry, stale_after_seconds),
        ).fetchone()
        return row is not None

    def set_raw_location(self, *, document_id: int, location: ArtifactLocation) -> None:
        self._record_raw_location(
            document_id=document_id,
            location=location,
            repair=False,
        )

    def repair_raw_location(self, *, document_id: int, location: ArtifactLocation) -> None:
        """Record an integrity repair without discarding the prior raw pointer."""

        self._record_raw_location(
            document_id=document_id,
            location=location,
            repair=True,
        )

    def _record_raw_location(
        self,
        *,
        document_id: int,
        location: ArtifactLocation,
        repair: bool,
    ) -> None:
        with self.connection.transaction():
            document = self.connection.execute(
                """
                SELECT sha256, content_length_bytes,
                       artifact_backend, storage_account_name,
                       storage_container, blob_name, blob_version_id
                FROM coi.documents
                WHERE document_id = %s
                FOR UPDATE
                """,
                (document_id,),
            ).fetchone()
            if not document:
                raise RuntimeError("document is missing while recording raw-artifact evidence")
            current = (
                document["artifact_backend"],
                document["storage_account_name"],
                document["storage_container"],
                document["blob_name"],
                document["blob_version_id"],
            )
            requested = (
                location.backend,
                location.storage_account_name,
                location.container,
                location.blob_name,
                location.version_id,
            )
            if not repair and current == requested:
                return
            if not repair and document["blob_name"] is not None:
                raise RuntimeError("document raw-artifact evidence is already set")
            content_length_bytes = document["content_length_bytes"]
            if content_length_bytes is None:
                raise RuntimeError("document has no canonical content length")

            self.connection.execute(
                """
                INSERT INTO coi.document_artifacts
                    (document_id, artifact_kind, artifact_backend,
                     storage_account_name, storage_container, blob_name,
                     blob_version_id, sha256, content_length_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    "source_pdf_repaired" if repair else "source_pdf_retained",
                    location.backend,
                    location.storage_account_name,
                    location.container,
                    location.blob_name,
                    location.version_id,
                    document["sha256"],
                    content_length_bytes,
                ),
            )
            self.connection.execute(
                """
                UPDATE coi.documents
                SET artifact_backend = %s,
                    storage_account_name = %s,
                    storage_container = %s,
                    blob_name = %s,
                    blob_version_id = %s,
                    updated_at = now()
                WHERE document_id = %s
                """,
                (
                    location.backend,
                    location.storage_account_name,
                    location.container,
                    location.blob_name,
                    location.version_id,
                    document_id,
                ),
            )

    def create_parse_attempt(self, *, document_id: int) -> int:
        row = self.connection.execute(
            """
            INSERT INTO coi.parse_attempts
                (document_id, provider, parser_version, status)
            VALUES (%s, 'pdf.co', 'ai-invoice-parser', 'started')
            RETURNING parse_attempt_id
            """,
            (document_id,),
        ).fetchone()
        if not row:  # pragma: no cover - INSERT RETURNING always returns
            raise RuntimeError("parse attempt insert returned no ID")
        return int(row["parse_attempt_id"])

    def find_reusable_parse(self, *, document_id: int) -> ReusableParse | None:
        row = self.connection.execute(
            """
            SELECT
                parse_attempt_id,
                provider_job_id,
                CASE WHEN artifact_usable THEN result_artifact_backend END
                    AS result_artifact_backend,
                CASE WHEN artifact_usable THEN result_storage_account_name END
                    AS result_storage_account_name,
                CASE WHEN artifact_usable THEN result_container END AS result_container,
                CASE WHEN artifact_usable THEN result_blob_name END AS result_blob_name,
                CASE WHEN artifact_usable THEN result_blob_version_id END
                    AS result_blob_version_id
            FROM (
                SELECT
                    attempt.*,
                    (
                        attempt.result_blob_name IS NOT NULL
                        AND attempt.error_code
                            IS DISTINCT FROM 'retained_artifact_unusable'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM coi.parse_attempts AS invalid_replay
                            WHERE invalid_replay.error_code
                                  = 'retained_artifact_unusable'
                              AND invalid_replay.result_blob_name
                                  = attempt.result_blob_name
                              AND invalid_replay.result_artifact_backend
                                  IS NOT DISTINCT FROM attempt.result_artifact_backend
                              AND invalid_replay.result_storage_account_name
                                  IS NOT DISTINCT FROM attempt.result_storage_account_name
                              AND invalid_replay.result_container
                                  IS NOT DISTINCT FROM attempt.result_container
                              AND invalid_replay.result_blob_version_id
                                  IS NOT DISTINCT FROM attempt.result_blob_version_id
                        )
                    ) AS artifact_usable,
                    (
                        attempt.provider = 'pdf.co'
                        AND attempt.provider_job_id IS NOT NULL
                        AND attempt.error_code
                            IS DISTINCT FROM 'pdfco_terminal_error'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM coi.parse_attempts AS recovery
                            WHERE recovery.provider = 'pdf.co-resume'
                              AND recovery.error_code = 'pdfco_terminal_error'
                              AND recovery.metadata
                                  ->> 'source_parse_attempt_id'
                                  = attempt.parse_attempt_id::TEXT
                        )
                    ) AS job_resumable
                FROM coi.parse_attempts AS attempt
                WHERE attempt.document_id = %s
            ) AS candidate
            WHERE artifact_usable OR job_resumable
            ORDER BY artifact_usable DESC, started_at DESC, parse_attempt_id DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if not row:
            return None
        location = None
        if row["result_blob_name"] is not None:
            location = ArtifactLocation(
                backend=str(row["result_artifact_backend"]),
                storage_account_name=row["result_storage_account_name"],
                container=row["result_container"],
                blob_name=str(row["result_blob_name"]),
                version_id=row["result_blob_version_id"],
            )
        return ReusableParse(
            parse_attempt_id=int(row["parse_attempt_id"]),
            provider_job_id=row["provider_job_id"],
            result_location=location,
        )

    def create_replay_attempt(
        self,
        *,
        document_id: int,
        source_attempt_id: int,
        location: ArtifactLocation,
    ) -> int:
        row = self.connection.execute(
            """
            INSERT INTO coi.parse_attempts
                (document_id, provider, parser_version, status,
                 result_artifact_backend, result_storage_account_name,
                 result_container, result_blob_name, result_blob_version_id, metadata)
            VALUES
                (%s, 'retained-artifact', 'mapping-replay', 'processing',
                 %s, %s, %s, %s, %s, %s)
            RETURNING parse_attempt_id
            """,
            (
                document_id,
                location.backend,
                location.storage_account_name,
                location.container,
                location.blob_name,
                location.version_id,
                Jsonb({"source_parse_attempt_id": source_attempt_id}),
            ),
        ).fetchone()
        if not row:  # pragma: no cover - INSERT RETURNING always returns
            raise RuntimeError("replay attempt insert returned no ID")
        return int(row["parse_attempt_id"])

    def create_job_resume_attempt(
        self,
        *,
        document_id: int,
        source_attempt_id: int,
        provider_job_id: str,
    ) -> int:
        row = self.connection.execute(
            """
            INSERT INTO coi.parse_attempts
                (document_id, provider, parser_version, status, metadata)
            VALUES
                (%s, 'pdf.co-resume', 'ai-invoice-parser', 'processing', %s)
            RETURNING parse_attempt_id
            """,
            (
                document_id,
                Jsonb(
                    {
                        "source_parse_attempt_id": source_attempt_id,
                        "provider_job_id": provider_job_id,
                    }
                ),
            ),
        ).fetchone()
        if not row:  # pragma: no cover - INSERT RETURNING always returns
            raise RuntimeError("job-resume attempt insert returned no ID")
        return int(row["parse_attempt_id"])

    def set_attempt_job(self, *, parse_attempt_id: int, job_id: str) -> None:
        row = self.connection.execute(
            """
            UPDATE coi.parse_attempts
            SET provider_job_id = %s, status = 'processing'
            WHERE parse_attempt_id = %s
              AND provider_job_id IS NULL
              AND completed_at IS NULL
              AND status IN ('started', 'processing')
            RETURNING parse_attempt_id
            """,
            (job_id, parse_attempt_id),
        ).fetchone()
        if row:
            return
        existing = self.connection.execute(
            """
            SELECT provider_job_id
            FROM coi.parse_attempts
            WHERE parse_attempt_id = %s
            """,
            (parse_attempt_id,),
        ).fetchone()
        if existing and existing["provider_job_id"] == job_id:
            return
        raise RuntimeError("parse-attempt provider job evidence is already set or missing")

    def set_attempt_result(
        self,
        *,
        parse_attempt_id: int,
        location: ArtifactLocation,
    ) -> None:
        row = self.connection.execute(
            """
            UPDATE coi.parse_attempts
            SET result_artifact_backend = %s,
                result_storage_account_name = %s,
                result_container = %s,
                result_blob_name = %s,
                result_blob_version_id = %s
            WHERE parse_attempt_id = %s
              AND result_artifact_backend IS NULL
              AND result_storage_account_name IS NULL
              AND result_container IS NULL
              AND result_blob_name IS NULL
              AND result_blob_version_id IS NULL
              AND completed_at IS NULL
            RETURNING parse_attempt_id
            """,
            (
                location.backend,
                location.storage_account_name,
                location.container,
                location.blob_name,
                location.version_id,
                parse_attempt_id,
            ),
        ).fetchone()
        if row:
            return
        existing = self.connection.execute(
            """
            SELECT result_artifact_backend, result_storage_account_name,
                   result_container, result_blob_name, result_blob_version_id
            FROM coi.parse_attempts
            WHERE parse_attempt_id = %s
            """,
            (parse_attempt_id,),
        ).fetchone()
        if existing and (
            existing["result_artifact_backend"],
            existing["result_storage_account_name"],
            existing["result_container"],
            existing["result_blob_name"],
            existing["result_blob_version_id"],
        ) == (
            location.backend,
            location.storage_account_name,
            location.container,
            location.blob_name,
            location.version_id,
        ):
            return
        raise RuntimeError("parse-attempt result evidence is already set or missing")

    def mark_needs_review(
        self,
        *,
        document_id: int,
        parse_attempt_id: int | None,
        error_code: str,
        error_message: str,
    ) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                UPDATE coi.documents
                SET status = 'needs_review',
                    review_status = 'needs_review',
                    error_code = %s,
                    error_message = %s,
                    updated_at = now()
                WHERE document_id = %s
                """,
                (error_code, error_message[:2000], document_id),
            )
            if parse_attempt_id is not None:
                self.connection.execute(
                    """
                    UPDATE coi.parse_attempts
                    SET status = 'succeeded',
                        error_code = %s,
                        error_message = %s,
                        completed_at = now()
                    WHERE parse_attempt_id = %s
                    """,
                    (error_code, error_message[:2000], parse_attempt_id),
                )

    def mark_failed(
        self,
        *,
        document_id: int,
        parse_attempt_id: int | None,
        error_code: str,
        error_message: str,
    ) -> None:
        safe_message = error_message.replace("\n", " ")[:2000]
        with self.connection.transaction():
            self.connection.execute(
                """
                UPDATE coi.documents
                SET status = 'failed',
                    review_status = 'needs_review',
                    error_code = %s,
                    error_message = %s,
                    updated_at = now()
                WHERE document_id = %s
                """,
                (error_code, safe_message, document_id),
            )
            if parse_attempt_id is not None:
                self.connection.execute(
                    """
                    UPDATE coi.parse_attempts
                    SET status = 'failed',
                        error_code = %s,
                        error_message = %s,
                        completed_at = now()
                    WHERE parse_attempt_id = %s
                    """,
                    (error_code, safe_message, parse_attempt_id),
                )

    def store_invoice(
        self,
        *,
        document_id: int,
        parse_attempt_id: int,
        record: InvoiceRecord,
    ) -> StoreResult:
        with self.connection.transaction():
            row = self.connection.execute(
                """
                INSERT INTO coi.invoice_summary
                    (document_id, vendor, invoice_no, order_no, po, account_no,
                     salesman, invoice_date, order_date, terms, freight_terms,
                     subtotal, freight, misc, tax, less_prepaid_deposit, total,
                     currency)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vendor, invoice_no) DO NOTHING
                RETURNING invoice_id
                """,
                (
                    document_id,
                    record.vendor,
                    record.invoice_no,
                    record.order_no,
                    record.po,
                    record.account_no,
                    record.salesman,
                    record.invoice_date,
                    record.order_date,
                    record.terms,
                    record.freight_terms,
                    record.subtotal,
                    record.freight,
                    record.misc,
                    record.tax,
                    record.less_prepaid_deposit,
                    record.total,
                    record.currency,
                ),
            ).fetchone()
            if not row:
                return self._invoice_conflict(document_id, record.invoice_no, record.vendor)

            invoice_id = int(row["invoice_id"])
            for line in record.lines:
                self.connection.execute(
                    """
                    INSERT INTO coi.invoice_line_items
                        (invoice_id, line_position, source_line_no, ord_qty, ship_qty,
                         bo_qty, product_code, description, price_list, discount_pct,
                         net_price, extension)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        invoice_id,
                        line.line_position,
                        line.source_line_no,
                        line.ord_qty,
                        line.ship_qty,
                        line.bo_qty,
                        line.product_code,
                        line.description,
                        line.price_list,
                        line.discount_pct,
                        line.net_price,
                        line.extension,
                    ),
                )
            self._mark_stored(document_id, parse_attempt_id)
            return StoreResult(outcome="stored", parent_id=invoice_id, line_count=len(record.lines))

    def _invoice_conflict(self, document_id: int, invoice_no: str, vendor: str) -> StoreResult:
        existing = self.connection.execute(
            """
            SELECT invoice_id, document_id
            FROM coi.invoice_summary
            WHERE vendor = %s AND invoice_no = %s
            """,
            (vendor, invoice_no),
        ).fetchone()
        if existing and int(existing["document_id"]) == document_id:
            return StoreResult(outcome="duplicate", parent_id=int(existing["invoice_id"]))
        return StoreResult(
            outcome="business_key_conflict",
            conflicting_document_id=(int(existing["document_id"]) if existing else None),
        )

    def store_oa(
        self,
        *,
        document_id: int,
        parse_attempt_id: int,
        record: OaRecord,
    ) -> StoreResult:
        with self.connection.transaction():
            row = self.connection.execute(
                """
                INSERT INTO coi.oa_summary
                    (document_id, vendor, order_no, po, account_no, salesman,
                     order_date, ship_date, terms, reference, freight_terms, fob,
                     subtotal, freight, total, retail_extension_total, currency)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vendor, order_no) DO NOTHING
                RETURNING oa_id
                """,
                (
                    document_id,
                    record.vendor,
                    record.order_no,
                    record.po,
                    record.account_no,
                    record.salesman,
                    record.order_date,
                    record.ship_date,
                    record.terms,
                    record.reference,
                    record.freight_terms,
                    record.fob,
                    record.subtotal,
                    record.freight,
                    record.total,
                    record.retail_extension_total,
                    record.currency,
                ),
            ).fetchone()
            if not row:
                return self._oa_conflict(document_id, record.order_no, record.vendor)

            oa_id = int(row["oa_id"])
            for line in record.lines:
                self.connection.execute(
                    """
                    INSERT INTO coi.oa_line_items
                        (oa_id, line_position, source_line_no, qty, product_code,
                         description, retail_price, retail_extension, discount_pct,
                         net_price, extension)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        oa_id,
                        line.line_position,
                        line.source_line_no,
                        line.qty,
                        line.product_code,
                        line.description,
                        line.retail_price,
                        line.retail_extension,
                        line.discount_pct,
                        line.net_price,
                        line.extension,
                    ),
                )
            self._mark_stored(document_id, parse_attempt_id)
            return StoreResult(outcome="stored", parent_id=oa_id, line_count=len(record.lines))

    def complete_duplicate_attempt(self, *, document_id: int, parse_attempt_id: int) -> None:
        with self.connection.transaction():
            self._mark_stored(document_id, parse_attempt_id)

    def _oa_conflict(self, document_id: int, order_no: str, vendor: str) -> StoreResult:
        existing = self.connection.execute(
            """
            SELECT oa_id, document_id
            FROM coi.oa_summary
            WHERE vendor = %s AND order_no = %s
            """,
            (vendor, order_no),
        ).fetchone()
        if existing and int(existing["document_id"]) == document_id:
            return StoreResult(outcome="duplicate", parent_id=int(existing["oa_id"]))
        return StoreResult(
            outcome="business_key_conflict",
            conflicting_document_id=(int(existing["document_id"]) if existing else None),
        )

    def _mark_stored(self, document_id: int, parse_attempt_id: int) -> None:
        self.connection.execute(
            """
            UPDATE coi.documents
            SET status = 'parsed',
                review_status = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE document_id = %s
            """,
            (document_id,),
        )
        self.connection.execute(
            """
            UPDATE coi.parse_attempts
            SET status = 'succeeded', completed_at = now()
            WHERE parse_attempt_id = %s
            """,
            (parse_attempt_id,),
        )
