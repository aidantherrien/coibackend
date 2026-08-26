import hashlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coi_backend.ingestion import (
    IngestionService,
    SourceNotReadyError,
    snapshot_source,
)
from coi_backend.pdfco import ParseJob, PdfCoError, PdfCoResponseTooLargeError
from coi_backend.repository import DocumentRecord, ReusableParse, StoreResult
from coi_backend.storage import (
    ArtifactIntegrityError,
    ArtifactLocation,
    ArtifactUnavailableError,
)


class FakeRepository:
    def __init__(self, *, document_type: str = "invoice", vendor: str = "ARTOPEX") -> None:
        self.document = DocumentRecord(
            document_id=42,
            sha256="",
            document_type=document_type,
            vendor=vendor,
            status="discovered",
            review_status="pending",
            created=True,
        )
        self.review: tuple[str, str] | None = None
        self.failed: tuple[str, str] | None = None
        self.source_review: tuple[str, str] | None = None
        self.source_metadata: dict[str, str] = {}
        self.source_references: list[str] = []

    def register_document(self, **values: object) -> DocumentRecord:
        self.document = DocumentRecord(
            **{
                **self.document.__dict__,
                "sha256": str(values["sha256"]),
                "content_length_bytes": int(values["content_length_bytes"]),
            }
        )
        return self.document

    def add_local_source(self, **values: object) -> int:
        self.source_references.append(str(values["source_reference"]))
        self.source_metadata = {
            "source_filename": str(values["source_filename"]),
            "observed_document_type": str(values["observed_document_type"]),
            "observed_vendor": str(values["observed_vendor"]),
        }
        return 11

    def mark_source_needs_review(
        self,
        *,
        document_source_id: int,
        error_code: str,
        error_message: str,
    ) -> None:
        assert document_source_id == 11
        self.source_review = (error_code, error_message)

    def try_claim(self, **_: object) -> bool:
        return True

    def set_raw_location(self, **_: object) -> None:
        pass

    def repair_raw_location(self, **_: object) -> None:
        pass

    def create_parse_attempt(self, **_: object) -> int:
        return 7

    def find_reusable_parse(self, **_: object) -> None:
        return None

    def set_attempt_job(self, **_: object) -> None:
        pass

    def set_attempt_result(self, **_: object) -> None:
        pass

    def store_invoice(self, **_: object) -> StoreResult:
        return StoreResult(outcome="stored", parent_id=3, line_count=1)

    def mark_needs_review(self, **values: object) -> None:
        self.review = (str(values["error_code"]), str(values["error_message"]))

    def mark_failed(self, **values: object) -> None:
        self.failed = (str(values["error_code"]), str(values["error_message"]))


class FakeStore:
    raw_payload: bytes | None = None

    def store_raw_pdf(self, *_: object, **__: object) -> ArtifactLocation:
        return ArtifactLocation("local", None, None, "raw.pdf")

    def store_raw_pdf_bytes(self, payload: bytes, **__: object) -> ArtifactLocation:
        self.raw_payload = payload
        return ArtifactLocation("local", None, None, "raw.pdf")

    def load_raw_pdf(self, *_: object, **__: object) -> bytes:
        return self.raw_payload or b"%PDF synthetic"

    def store_parser_json(self, *_: object, **__: object) -> ArtifactLocation:
        return ArtifactLocation("local", None, None, "parsed.json")

    def load_parser_json(self, *_: object, **__: object) -> dict[str, object]:
        raise AssertionError("a fresh document must not load a retained result")


class FakePdfCo:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.uploaded_payload: bytes | None = None

    def upload(self, _: Path) -> str:
        return "https://example.invalid/upload"

    def upload_bytes(self, payload: bytes, *, filename: str) -> str:
        assert filename.endswith(".pdf")
        self.uploaded_payload = payload
        return "https://example.invalid/upload"

    def start_parse(self, _: str) -> ParseJob:
        return ParseJob("job-1", None)

    def wait_for_result(self, _: ParseJob) -> tuple[dict[str, object], str, bytes]:
        raw = json.dumps(self.payload).encode("utf-8")
        return self.payload, "https://example.invalid/result", raw


def _service(repository: FakeRepository, payload: dict[str, object]) -> IngestionService:
    settings = SimpleNamespace(
        document_type="invoice",
        vendor="ARTOPEX",
        processing_stale_after_seconds=3600,
        max_pdf_bytes=1024,
        source_min_age_seconds=0,
    )
    logger = logging.LoggerAdapter(logging.getLogger("test.ingestion"), {})
    return IngestionService(
        settings=settings,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=FakeStore(),  # type: ignore[arg-type]
        pdfco=FakePdfCo(payload),  # type: ignore[arg-type]
        logger=logger,
    )


def test_ingestion_records_provenance_and_stores_valid_invoice(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    repository = FakeRepository()
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-1", "poNo": "jf-1"},
        "paymentDetails": {"total": "12.34"},
        "lineItems": [{"lineNo": "001", "productCode": "P-1"}],
    }

    outcome = _service(repository, payload).ingest(path)

    assert outcome.outcome == "stored"
    assert outcome.disposition == "archive"
    assert outcome.line_count == 1
    assert repository.document.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert repository.source_metadata == {
        "source_filename": "invoice.pdf",
        "observed_document_type": "invoice",
        "observed_vendor": "ARTOPEX",
    }
    source_reference = repository.source_references[0]
    prefix, version, algorithm, opaque_digest = source_reference.split(":")
    assert (prefix, version, algorithm) == ("local-file", "v1", "sha256")
    assert len(opaque_digest) == 64
    assert set(opaque_digest) <= set("0123456789abcdef")
    assert str(path.resolve()) not in source_reference
    assert path.name not in source_reference
    assert "ARTOPEX" not in source_reference
    assert "invoice" not in source_reference


def test_unchanged_local_file_retry_reuses_source_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF retry occurrence")

    class UnclaimedRepository(FakeRepository):
        def try_claim(self, **_: object) -> bool:
            return False

    repository = UnclaimedRepository()
    service = _service(repository, {})

    first = service.ingest(path)
    second = service.ingest(path)

    assert first.outcome == second.outcome == "skipped"
    assert len(repository.source_references) == 2
    assert repository.source_references[0] == repository.source_references[1]


def test_identical_rearrival_at_same_path_creates_new_source_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    payload = b"%PDF identical rearrival"
    path.write_bytes(payload)

    class UnclaimedRepository(FakeRepository):
        def try_claim(self, **_: object) -> bool:
            return False

    repository = UnclaimedRepository()
    service = _service(repository, {})
    first = service.ingest(path)

    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(payload)
    original_mtime_ns = path.stat().st_mtime_ns
    replacement_mtime_ns = max(1, original_mtime_ns - 5_000_000_000)
    os.utime(replacement, ns=(replacement_mtime_ns, replacement_mtime_ns))
    os.replace(replacement, path)

    second = service.ingest(path)

    assert first.outcome == second.outcome == "skipped"
    assert len(repository.source_references) == 2
    assert repository.source_references[0] != repository.source_references[1]


def test_source_occurrence_is_scoped_to_vendor_and_document_type(tmp_path: Path) -> None:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF classification scope")

    class UnclaimedRepository(FakeRepository):
        def try_claim(self, **_: object) -> bool:
            return False

    configurations = (
        ("ARTOPEX", "invoice"),
        ("OTHER", "invoice"),
        ("ARTOPEX", "oa"),
    )
    references: list[str] = []
    for vendor, document_type in configurations:
        repository = UnclaimedRepository(document_type=document_type, vendor=vendor)
        service = _service(repository, {})
        service.settings.vendor = vendor
        service.settings.document_type = document_type
        service.ingest(path)
        references.append(repository.source_references[0])

    assert len(set(references)) == len(configurations)


def test_malformed_business_value_is_review_not_infrastructure_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    repository = FakeRepository()
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-1", "poNo": "JF-1"},
        "paymentDetails": {"total": "not a number"},
        "lineItems": [],
    }

    outcome = _service(repository, payload).ingest(path)

    assert outcome.outcome == "needs_review"
    assert outcome.disposition == "quarantine"
    assert repository.review is not None
    assert repository.review[0] == "mapping_needs_review"
    assert repository.failed is None


def test_misclassified_duplicate_does_not_downgrade_canonical_document(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    repository = FakeRepository(document_type="oa")

    outcome = _service(repository, {}).ingest(path)

    assert outcome.outcome == "needs_review"
    assert repository.review is None
    assert repository.source_review is not None
    assert repository.source_review[0] == "source_classification_conflict"


def test_registered_length_must_match_stable_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")

    class LengthConflictRepository(FakeRepository):
        def register_document(self, **values: object) -> DocumentRecord:
            document = super().register_document(**values)
            return DocumentRecord(
                **{
                    **document.__dict__,
                    "content_length_bytes": document.content_length_bytes + 1,  # type: ignore[operator]
                }
            )

    repository = LengthConflictRepository()
    outcome = _service(repository, {}).ingest(path)

    assert outcome.outcome == "failed"
    assert repository.source_review is not None
    assert repository.source_review[0] == "registered_length_conflict"


def test_force_retry_replays_retained_json_without_new_paid_parse(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-2", "poNo": "JF-2"},
        "paymentDetails": {"total": "20.00"},
        "lineItems": [],
    }

    class ReplayRepository(FakeRepository):
        replay_created = False

        def find_reusable_parse(self, **_: object) -> ReusableParse:
            return ReusableParse(
                5,
                "paid-job",
                ArtifactLocation("local", None, None, "parsed.json"),
            )

        def create_replay_attempt(self, **_: object) -> int:
            self.replay_created = True
            return 8

    class ReplayStore(FakeStore):
        def load_parser_json(self, *_: object, **__: object) -> dict[str, object]:
            return payload

    class NoPaidParse(FakePdfCo):
        def upload_bytes(self, _: bytes, *, filename: str) -> str:
            raise AssertionError("retained JSON should avoid a new upload and parse")

    repository = ReplayRepository()
    settings = SimpleNamespace(
        document_type="invoice",
        vendor="ARTOPEX",
        processing_stale_after_seconds=3600,
        max_pdf_bytes=1024,
        source_min_age_seconds=0,
    )
    service = IngestionService(
        settings=settings,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=ReplayStore(),  # type: ignore[arg-type]
        pdfco=NoPaidParse(payload),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path, force_retry=True)

    assert outcome.outcome == "stored"
    assert repository.replay_created


def test_retry_without_recovery_requires_explicit_new_parse_authorization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    repository = FakeRepository()
    repository.document = DocumentRecord(
        **{**repository.document.__dict__, "status": "failed", "created": False}
    )

    class NoPaidParse(FakePdfCo):
        def upload_bytes(self, _: bytes, *, filename: str) -> str:
            raise AssertionError("retry must require explicit authorization")

    settings = SimpleNamespace(
        document_type="invoice",
        vendor="ARTOPEX",
        processing_stale_after_seconds=3600,
        max_pdf_bytes=1024,
        source_min_age_seconds=0,
    )
    service = IngestionService(
        settings=settings,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=FakeStore(),  # type: ignore[arg-type]
        pdfco=NoPaidParse({}),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path, force_retry=True)

    assert outcome.outcome == "failed"
    assert outcome.disposition == "quarantine"
    assert repository.failed is not None
    assert repository.failed[0] == "paid_parse_authorization_required"


def test_unclaimed_document_is_retained_for_explicit_retry(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF awaiting explicit retry")

    class UnclaimedRepository(FakeRepository):
        def try_claim(self, **_: object) -> bool:
            return False

    repository = UnclaimedRepository()
    repository.document = DocumentRecord(
        **{**repository.document.__dict__, "status": "failed", "created": False}
    )

    outcome = _service(repository, {}).ingest(path)

    assert outcome.outcome == "skipped"
    assert outcome.disposition == "retain"


def test_corrupt_retained_json_is_invalidated_before_resuming_known_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-3", "poNo": "JF-3"},
        "paymentDetails": {"total": "30.00"},
        "lineItems": [],
    }

    class RecoveryRepository(FakeRepository):
        invalidated = False
        resumed = False

        def find_reusable_parse(self, **_: object) -> ReusableParse:
            location = (
                None if self.invalidated else ArtifactLocation("local", None, None, "bad.json")
            )
            return ReusableParse(5, "known-job", location)

        def create_replay_attempt(self, **_: object) -> int:
            return 8

        def create_job_resume_attempt(self, **_: object) -> int:
            self.resumed = True
            return 9

        def mark_failed(self, **values: object) -> None:
            super().mark_failed(**values)
            if values["error_code"] == "retained_artifact_unusable":
                self.invalidated = True

    class CorruptStore(FakeStore):
        def load_parser_json(self, *_: object, **__: object) -> dict[str, object]:
            raise ArtifactIntegrityError("content hash does not match")

    class ResumeOnlyPdfCo(FakePdfCo):
        def upload_bytes(self, _: bytes, *, filename: str) -> str:
            raise AssertionError("a known provider job should be resumed")

    repository = RecoveryRepository()
    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=CorruptStore(),  # type: ignore[arg-type]
        pdfco=ResumeOnlyPdfCo(payload),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path, force_retry=True)

    assert outcome.outcome == "stored"
    assert repository.invalidated
    assert repository.resumed


def test_source_snapshot_rejects_oversize_and_defers_recent_file(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"12345")

    repository = FakeRepository()
    service = _service(repository, {})
    service.settings.max_pdf_bytes = 4
    outcome = service.ingest(path)
    assert outcome.outcome == "failed"
    assert outcome.disposition == "quarantine"
    assert "MAX_PDF_BYTES" in outcome.message
    assert repository.document.sha256 == ""

    service.settings.max_pdf_bytes = 10
    service.settings.source_min_age_seconds = 60
    outcome = service.ingest(path)
    assert outcome.outcome == "deferred"
    assert outcome.disposition == "retain"
    assert "SOURCE_MIN_AGE_SECONDS" in outcome.message
    assert repository.document.sha256 == ""


def test_source_snapshot_detects_change_during_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF stable-looking")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            values = list(result)
            values[8] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr("coi_backend.ingestion.os.fstat", changing_fstat)
    with pytest.raises(SourceNotReadyError, match="changed while"):
        snapshot_source(path, max_bytes=1024, min_age_seconds=0)


def test_source_snapshot_detects_ctime_change_during_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF ctime changes")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_ctime_ns=result.st_ctime_ns + 1,
                st_mtime_ns=result.st_mtime_ns,
            )  # type: ignore[return-value]
        return result

    monkeypatch.setattr("coi_backend.ingestion.os.fstat", changing_fstat)
    with pytest.raises(SourceNotReadyError, match="changed while"):
        snapshot_source(path, max_bytes=1024, min_age_seconds=0)


def test_source_snapshot_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF target")
    link = tmp_path / "link.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available to this test user")

    with pytest.raises(RuntimeError, match="symbolic link"):
        snapshot_source(link, max_bytes=1024, min_age_seconds=0)


def test_source_snapshot_bytes_are_the_only_bytes_retained_and_uploaded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    source_bytes = b"%PDF exact snapshot"
    path.write_bytes(source_bytes)
    repository = FakeRepository()
    store = FakeStore()
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-4", "poNo": "JF-4"},
        "paymentDetails": {"total": "1.00"},
        "lineItems": [],
    }
    pdfco = FakePdfCo(payload)
    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
        pdfco=pdfco,  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path)

    assert outcome.outcome == "stored"
    assert store.raw_payload == source_bytes
    assert pdfco.uploaded_payload == source_bytes
    assert repository.document.sha256 == hashlib.sha256(source_bytes).hexdigest()


def test_oversized_parser_response_is_policy_failure_not_artifact_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF synthetic")
    repository = FakeRepository()

    class OversizedResult(FakePdfCo):
        def wait_for_result(self, _: ParseJob) -> tuple[dict[str, object], str, bytes]:
            raise PdfCoResponseTooLargeError("PDF.co result exceeded configured size limit")

    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=FakeStore(),  # type: ignore[arg-type]
        pdfco=OversizedResult({}),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path)

    assert outcome.outcome == "failed"
    assert outcome.disposition == "quarantine"
    assert repository.failed is not None
    assert repository.failed[0] == "parser_response_too_large"


def test_transient_artifact_failure_retains_source_for_retry(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF transient artifact failure")
    repository = FakeRepository()

    class UnavailableStore(FakeStore):
        def store_raw_pdf_bytes(self, *_: object, **__: object) -> ArtifactLocation:
            raise ArtifactUnavailableError("artifact backend unavailable")

    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=UnavailableStore(),  # type: ignore[arg-type]
        pdfco=FakePdfCo({}),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path)

    assert outcome.outcome == "failed"
    assert outcome.disposition == "retain"
    assert repository.failed is not None
    assert repository.failed[0] == "artifact_store_unavailable"


@pytest.mark.parametrize(
    ("terminal", "expected_disposition"),
    [(False, "retain"), (True, "quarantine")],
)
def test_pdfco_failure_disposition_distinguishes_retryable_from_terminal(
    tmp_path: Path,
    terminal: bool,
    expected_disposition: str,
) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF provider failure")
    repository = FakeRepository()

    class FailedPdfCo(FakePdfCo):
        def wait_for_result(self, _: ParseJob) -> tuple[dict[str, object], str, bytes]:
            raise PdfCoError("provider failed safely", terminal=terminal)

    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=FakeStore(),  # type: ignore[arg-type]
        pdfco=FailedPdfCo({}),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path)

    assert outcome.outcome == "failed"
    assert outcome.disposition == expected_disposition


def test_corrupt_raw_artifact_is_repaired_from_verified_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    source_bytes = b"%PDF repair source"
    path.write_bytes(source_bytes)
    payload: dict[str, object] = {
        "invoice": {"invoiceNo": "INV-5", "poNo": "JF-5"},
        "paymentDetails": {"total": "5.00"},
        "lineItems": [],
    }

    class RepairRepository(FakeRepository):
        repaired = False

        def repair_raw_location(self, **_: object) -> None:
            self.repaired = True

    class RepairStore(FakeStore):
        repaired = False

        def store_raw_pdf_bytes(
            self,
            payload: bytes,
            *,
            repair: bool = False,
            **_: object,
        ) -> ArtifactLocation:
            assert payload == source_bytes
            assert repair
            self.repaired = True
            return ArtifactLocation("local", None, None, "repaired.pdf")

        def load_raw_pdf(self, location: ArtifactLocation, **_: object) -> bytes:
            if location.blob_name == "old-corrupt.pdf":
                raise ArtifactIntegrityError("bad retained content")
            return source_bytes

    repository = RepairRepository()
    repository.document = DocumentRecord(
        **{
            **repository.document.__dict__,
            "artifact_backend": "local",
            "blob_name": "old-corrupt.pdf",
        }
    )
    store = RepairStore()
    service = IngestionService(
        settings=SimpleNamespace(
            document_type="invoice",
            vendor="ARTOPEX",
            processing_stale_after_seconds=3600,
            max_pdf_bytes=1024,
            source_min_age_seconds=0,
        ),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
        pdfco=FakePdfCo(payload),  # type: ignore[arg-type]
        logger=logging.LoggerAdapter(logging.getLogger("test.ingestion"), {}),
    )

    outcome = service.ingest(path)

    assert outcome.outcome == "stored"
    assert store.repaired
    assert repository.repaired
