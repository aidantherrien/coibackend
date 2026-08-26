"""Resumable, hash-first document ingestion orchestration."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Settings
from .mapping import MappingNeedsReview, map_invoice, map_oa
from .pdfco import ParseJob, PdfCoClient, PdfCoError, PdfCoResponseTooLargeError
from .repository import DocumentRepository
from .storage import (
    ArtifactIntegrityError,
    ArtifactLocation,
    ArtifactStore,
    ArtifactTooLargeError,
    ArtifactUnavailableError,
)

OutcomeName = Literal[
    "stored",
    "duplicate",
    "needs_review",
    "failed",
    "skipped",
    "deferred",
]
LifecycleDisposition = Literal["archive", "quarantine", "retain"]


@dataclass(frozen=True)
class IngestionOutcome:
    filename: str
    outcome: OutcomeName
    document_id: int | None = None
    message: str = ""
    line_count: int = 0
    content_sha256: str = ""
    disposition: LifecycleDisposition = "retain"


class SourceSnapshotError(RuntimeError):
    """The source cannot be safely materialized for ingestion."""


class SourceNotReadyError(SourceSnapshotError):
    """The source should be left in place and retried by a later run."""


@dataclass(frozen=True)
class SourceSnapshot:
    canonical_path: Path
    filename: str
    payload: bytes
    sha256: str
    content_length_bytes: int
    device: int
    inode: int
    ctime_ns: int
    birthtime_ns: int | None
    mtime_ns: int


def _birthtime_ns(value: os.stat_result) -> int | None:
    birthtime_ns = getattr(value, "st_birthtime_ns", None)
    if birthtime_ns is not None:
        return int(birthtime_ns)
    birthtime = getattr(value, "st_birthtime", None)
    return round(float(birthtime) * 1_000_000_000) if birthtime is not None else None


def _stable_ctime_ns(value: os.stat_result) -> int:
    # Windows reports creation time as ctime, but a path stat and an open-handle
    # stat can expose it through different fields. Prefer the explicit birth time
    # there so an unchanged file has the same signature in both observations.
    birthtime_ns = _birthtime_ns(value)
    if os.name == "nt" and birthtime_ns is not None:
        return birthtime_ns
    ctime_ns = getattr(value, "st_ctime_ns", None)
    if ctime_ns is not None:
        return int(ctime_ns)
    return round(float(value.st_ctime) * 1_000_000_000)


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(value.st_mode),
        value.st_dev,
        value.st_ino,
        value.st_size,
        _stable_ctime_ns(value),
        value.st_mtime_ns,
    )


def _local_source_reference(
    snapshot: SourceSnapshot,
    *,
    vendor: str,
    document_type: str,
) -> str:
    """Return a versioned occurrence key without exposing its filesystem path."""

    canonical_path = os.fsencode(os.path.normcase(os.fspath(snapshot.canonical_path)))
    fields = (
        (b"vendor", vendor.encode("utf-8")),
        (b"document_type", document_type.encode("utf-8")),
        (b"canonical_path", canonical_path),
        (b"device", str(snapshot.device).encode("ascii")),
        (b"inode", str(snapshot.inode).encode("ascii")),
        (b"ctime_ns", str(snapshot.ctime_ns).encode("ascii")),
        (
            b"birthtime_ns",
            (
                str(snapshot.birthtime_ns).encode("ascii")
                if snapshot.birthtime_ns is not None
                else b"unavailable"
            ),
        ),
        (b"mtime_ns", str(snapshot.mtime_ns).encode("ascii")),
        (b"content_length_bytes", str(snapshot.content_length_bytes).encode("ascii")),
        (b"content_sha256", snapshot.sha256.encode("ascii")),
    )
    reference_digest = hashlib.sha256(b"coi-local-file-source-reference-v1\0")
    for name, value in fields:
        reference_digest.update(len(name).to_bytes(2, "big"))
        reference_digest.update(name)
        reference_digest.update(len(value).to_bytes(8, "big"))
        reference_digest.update(value)
    return f"local-file:v1:sha256:{reference_digest.hexdigest()}"


def snapshot_source(
    path: Path,
    *,
    max_bytes: int,
    min_age_seconds: int,
) -> SourceSnapshot:
    """Read one bounded, stable source snapshot and compute identity in that pass."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if min_age_seconds < 0:
        raise ValueError("min_age_seconds must be zero or greater")

    try:
        before = path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(f"unable to inspect source file: {type(exc).__name__}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise SourceSnapshotError("source PDF must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise SourceSnapshotError("source PDF must be a regular file")
    if before.st_size > max_bytes:
        raise SourceSnapshotError("source PDF exceeds MAX_PDF_BYTES")
    if time.time_ns() - before.st_mtime_ns < min_age_seconds * 1_000_000_000:
        raise SourceNotReadyError("source PDF is newer than SOURCE_MIN_AGE_SECONDS")

    try:
        canonical_path = path.resolve(strict=True)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        count = 0
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            if _stat_signature(opened_before) != _stat_signature(before):
                raise SourceNotReadyError("source PDF changed before it could be read")
            while block := stream.read(min(1024 * 1024, max_bytes - count + 1)):
                count += len(block)
                if count > max_bytes:
                    raise SourceSnapshotError("source PDF exceeds MAX_PDF_BYTES")
                digest.update(block)
                chunks.append(block)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
        canonical_after = path.resolve(strict=True)
    except SourceSnapshotError:
        raise
    except FileNotFoundError as exc:
        raise SourceNotReadyError("source PDF changed or disappeared while being read") from exc
    except OSError as exc:
        raise SourceSnapshotError(f"unable to read source PDF: {type(exc).__name__}") from exc

    if stat.S_ISLNK(after.st_mode):
        raise SourceNotReadyError("source PDF was replaced by a symbolic link")
    signatures = {
        _stat_signature(before),
        _stat_signature(opened_before),
        _stat_signature(opened_after),
        _stat_signature(after),
    }
    if len(signatures) != 1 or canonical_after != canonical_path or count != before.st_size:
        raise SourceNotReadyError("source PDF changed while it was being read")

    return SourceSnapshot(
        canonical_path=canonical_path,
        filename=canonical_path.name,
        payload=b"".join(chunks),
        sha256=digest.hexdigest(),
        content_length_bytes=count,
        device=before.st_dev,
        inode=before.st_ino,
        ctime_ns=_stable_ctime_ns(before),
        birthtime_ns=_birthtime_ns(before),
        mtime_ns=before.st_mtime_ns,
    )


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: DocumentRepository,
        artifact_store: ArtifactStore,
        pdfco: PdfCoClient,
        logger: logging.LoggerAdapter[logging.Logger],
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifact_store = artifact_store
        self.pdfco = pdfco
        self.logger = logger

    def ingest(
        self,
        path: Path,
        *,
        force_retry: bool = False,
        allow_new_paid_parse: bool = False,
    ) -> IngestionOutcome:
        try:
            snapshot = snapshot_source(
                path,
                max_bytes=self.settings.max_pdf_bytes,
                min_age_seconds=self.settings.source_min_age_seconds,
            )
        except SourceNotReadyError as exc:
            message = str(exc)
            self.logger.warning("source not ready file=%s error=%s", path.name, message)
            return IngestionOutcome(path.name, "deferred", message=message)
        except SourceSnapshotError as exc:
            message = str(exc)
            self.logger.error("source rejected file=%s error=%s", path.name, message)
            return IngestionOutcome(
                path.name,
                "failed",
                message=message,
                disposition="quarantine",
            )

        digest = snapshot.sha256
        document = self.repository.register_document(
            sha256=digest,
            filename=snapshot.filename,
            content_length_bytes=snapshot.content_length_bytes,
            document_type=self.settings.document_type,
            vendor=self.settings.vendor,
        )
        source_id = self.repository.add_local_source(
            document_id=document.document_id,
            source_reference=_local_source_reference(
                snapshot,
                vendor=self.settings.vendor,
                document_type=self.settings.document_type,
            ),
            source_filename=snapshot.filename,
            observed_document_type=self.settings.document_type,
            observed_vendor=self.settings.vendor,
        )

        if (
            document.content_length_bytes is None
            or document.content_length_bytes != snapshot.content_length_bytes
        ):
            message = "registered document length is missing or conflicts with the source snapshot"
            self.repository.mark_source_needs_review(
                document_source_id=source_id,
                error_code="registered_length_conflict",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )

        if (
            document.document_type != self.settings.document_type
            or document.vendor != self.settings.vendor
        ):
            message = "same file hash was previously registered under another type/vendor"
            self.repository.mark_source_needs_review(
                document_source_id=source_id,
                error_code="source_classification_conflict",
                error_message=message,
            )
            self.logger.warning(
                "source classification conflict file=%s document_id=%s",
                path.name,
                document.document_id,
            )
            return IngestionOutcome(
                path.name,
                "needs_review",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )

        if document.status == "parsed":
            return IngestionOutcome(
                path.name,
                "duplicate",
                document.document_id,
                "content hash already parsed",
                content_sha256=digest,
                disposition="archive",
            )

        if not self.repository.try_claim(
            document_id=document.document_id,
            force_retry=force_retry,
            stale_after_seconds=self.settings.processing_stale_after_seconds,
        ):
            return IngestionOutcome(
                path.name,
                "skipped",
                document.document_id,
                f"document status is {document.status!r}; use --force-retry when appropriate",
                content_sha256=digest,
                disposition="retain",
            )

        parse_attempt_id: int | None = None
        try:
            content_length_bytes = document.content_length_bytes
            if document.blob_name:
                if document.artifact_backend is None:
                    raise ArtifactUnavailableError("retained raw artifact has no backend identity")
                raw_location = ArtifactLocation(
                    backend=document.artifact_backend,
                    storage_account_name=document.storage_account_name,
                    container=document.storage_container,
                    blob_name=document.blob_name,
                    version_id=document.blob_version_id,
                )
            else:
                try:
                    raw_location = self.artifact_store.store_raw_pdf_bytes(
                        snapshot.payload,
                        sha256=digest,
                        vendor=self.settings.vendor,
                    )
                except ArtifactIntegrityError:
                    raw_location = self.artifact_store.store_raw_pdf_bytes(
                        snapshot.payload,
                        sha256=digest,
                        vendor=self.settings.vendor,
                        repair=True,
                    )
                self.repository.set_raw_location(
                    document_id=document.document_id, location=raw_location
                )

            reusable = self.repository.find_reusable_parse(document_id=document.document_id)
            parsed = None
            while reusable and reusable.result_location:
                parse_attempt_id = self.repository.create_replay_attempt(
                    document_id=document.document_id,
                    source_attempt_id=reusable.parse_attempt_id,
                    location=reusable.result_location,
                )
                try:
                    parsed = self.artifact_store.load_parser_json(reusable.result_location)
                    break
                except ArtifactIntegrityError:
                    self.repository.mark_failed(
                        document_id=document.document_id,
                        parse_attempt_id=parse_attempt_id,
                        error_code="retained_artifact_unusable",
                        error_message="retained parser artifact could not be loaded",
                    )
                    self.logger.exception(
                        "retained parser artifact unusable document_id=%s attempt_id=%s",
                        document.document_id,
                        reusable.parse_attempt_id,
                    )
                    reusable = self.repository.find_reusable_parse(document_id=document.document_id)

            if parsed is not None:
                pass
            elif reusable and reusable.provider_job_id:
                parse_attempt_id = self.repository.create_job_resume_attempt(
                    document_id=document.document_id,
                    source_attempt_id=reusable.parse_attempt_id,
                    provider_job_id=reusable.provider_job_id,
                )
                job = ParseJob(reusable.provider_job_id, None)
                parsed, _result_url, raw_json = self.pdfco.wait_for_result(job)
                result_location = self.artifact_store.store_parser_json(
                    raw_json, sha256=digest, job_id=job.job_id
                )
                self.repository.set_attempt_result(
                    parse_attempt_id=parse_attempt_id, location=result_location
                )
            else:
                if document.status != "discovered" and not allow_new_paid_parse:
                    message = (
                        "retry has no retained result or known provider job; "
                        "use --allow-new-paid-parse with --force-retry to authorize a new charge"
                    )
                    self.repository.mark_failed(
                        document_id=document.document_id,
                        parse_attempt_id=None,
                        error_code="paid_parse_authorization_required",
                        error_message=message,
                    )
                    return IngestionOutcome(
                        path.name,
                        "failed",
                        document.document_id,
                        message,
                        content_sha256=digest,
                        disposition="quarantine",
                    )
                parse_attempt_id = self.repository.create_parse_attempt(
                    document_id=document.document_id
                )
                try:
                    retained_pdf = self.artifact_store.load_raw_pdf(
                        raw_location,
                        sha256=digest,
                        content_length_bytes=content_length_bytes,
                    )
                except ArtifactIntegrityError:
                    repaired_location = self.artifact_store.store_raw_pdf_bytes(
                        snapshot.payload,
                        sha256=digest,
                        vendor=self.settings.vendor,
                        repair=True,
                    )
                    self.repository.repair_raw_location(
                        document_id=document.document_id,
                        location=repaired_location,
                    )
                    raw_location = repaired_location
                    retained_pdf = self.artifact_store.load_raw_pdf(
                        raw_location,
                        sha256=digest,
                        content_length_bytes=content_length_bytes,
                    )
                uploaded_url = self.pdfco.upload_bytes(
                    retained_pdf,
                    filename=snapshot.filename,
                )
                job = self.pdfco.start_parse(uploaded_url)
                self.repository.set_attempt_job(
                    parse_attempt_id=parse_attempt_id, job_id=job.job_id
                )
                parsed, _result_url, raw_json = self.pdfco.wait_for_result(job)

                result_location = self.artifact_store.store_parser_json(
                    raw_json, sha256=digest, job_id=job.job_id
                )
                self.repository.set_attempt_result(
                    parse_attempt_id=parse_attempt_id, location=result_location
                )

            if self.settings.document_type == "invoice":
                record = map_invoice(parsed, vendor=self.settings.vendor)
                stored = self.repository.store_invoice(
                    document_id=document.document_id,
                    parse_attempt_id=parse_attempt_id,
                    record=record,
                )
            else:
                record = map_oa(parsed, vendor=self.settings.vendor)
                stored = self.repository.store_oa(
                    document_id=document.document_id,
                    parse_attempt_id=parse_attempt_id,
                    record=record,
                )

            if stored.outcome == "stored":
                return IngestionOutcome(
                    path.name,
                    "stored",
                    document.document_id,
                    line_count=stored.line_count,
                    content_sha256=digest,
                    disposition="archive",
                )
            if stored.outcome == "duplicate":
                self.repository.complete_duplicate_attempt(
                    document_id=document.document_id,
                    parse_attempt_id=parse_attempt_id,
                )
                return IngestionOutcome(
                    path.name,
                    "duplicate",
                    document.document_id,
                    "business record already exists for this document",
                    content_sha256=digest,
                    disposition="archive",
                )

            message = "business key belongs to a different source document" + (
                f" (document {stored.conflicting_document_id})"
                if stored.conflicting_document_id
                else ""
            )
            self.repository.mark_needs_review(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="business_key_conflict",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "needs_review",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )
        except (MappingNeedsReview, ValueError) as exc:
            message = str(exc)
            self.repository.mark_needs_review(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="mapping_needs_review",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "needs_review",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )
        except PdfCoResponseTooLargeError as exc:
            message = str(exc)
            self.repository.mark_failed(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="parser_response_too_large",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )
        except ArtifactTooLargeError as exc:
            message = str(exc)
            self.repository.mark_failed(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="artifact_policy_limit",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="quarantine",
            )
        except ArtifactUnavailableError as exc:
            message = str(exc)
            self.repository.mark_failed(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="artifact_store_unavailable",
                error_message=message,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
                disposition="retain",
            )
        except PdfCoError as exc:
            message = str(exc)
            self.repository.mark_failed(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code=("pdfco_terminal_error" if exc.terminal else "pdfco_retryable_error"),
                error_message=message,
            )
            self.logger.error(
                "PDF.co failure file=%s document_id=%s error=%s",
                path.name,
                document.document_id,
                message,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
                disposition=("quarantine" if exc.terminal else "retain"),
            )
        except Exception as exc:  # batch isolation; traceback remains in local logs
            message = f"{type(exc).__name__}: {str(exc)[:1000]}"
            self.repository.mark_failed(
                document_id=document.document_id,
                parse_attempt_id=parse_attempt_id,
                error_code="ingestion_error",
                error_message=message,
            )
            self.logger.exception(
                "ingestion failure file=%s document_id=%s",
                path.name,
                document.document_id,
            )
            return IngestionOutcome(
                path.name,
                "failed",
                document.document_id,
                message,
                content_sha256=digest,
            )
