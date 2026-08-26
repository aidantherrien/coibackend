import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from coi_backend.storage import (
    ArtifactIntegrityError,
    ArtifactLocation,
    ArtifactTooLargeError,
    ArtifactUnavailableError,
    AzureBlobArtifactStore,
    LocalArtifactStore,
)


def test_local_artifact_store_keeps_raw_and_parser_result(tmp_path: Path) -> None:
    source = tmp_path / "source" / "invoice.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF synthetic test")
    store = LocalArtifactStore(tmp_path / "artifacts")

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = store.store_raw_pdf(source, sha256=digest, vendor="ARTOPEX")
    raw_json = b'{"paymentDetails":{"total":10.20}}\n'
    parsed = store.store_parser_json(
        raw_json,
        sha256=digest,
        job_id="job-123",
    )

    assert raw.backend == "local"
    assert raw.storage_account_name is None
    assert raw.container is None
    assert (tmp_path / "artifacts" / raw.blob_name).read_bytes() == source.read_bytes()
    retained = (tmp_path / "artifacts" / parsed.blob_name).read_bytes()
    assert retained == raw_json
    assert store.load_parser_json(parsed)["paymentDetails"]["total"] == Decimal("10.20")


def test_local_artifact_store_rejects_corrupt_existing_content(tmp_path: Path) -> None:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF original")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = LocalArtifactStore(tmp_path / "artifacts")
    location = store.store_raw_pdf(source, sha256=digest, vendor="ARTOPEX")
    (tmp_path / "artifacts" / location.blob_name).write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="wrong hash"):
        store.store_raw_pdf(source, sha256=digest, vendor="ARTOPEX")


def test_local_artifact_store_rejects_path_components(tmp_path: Path) -> None:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF original")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(RuntimeError, match="safe path"):
        store.store_raw_pdf(source, sha256=digest, vendor="../../outside")
    with pytest.raises(RuntimeError, match="SHA-256"):
        store.store_parser_json(b"{}", sha256="../../outside", job_id="job")


def test_parser_artifact_is_content_addressed_and_tamper_evident(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    first = store.store_parser_json(b'{"total":10.20}', sha256="a" * 64, job_id="job")
    same = store.store_parser_json(b'{"total":10.20}', sha256="a" * 64, job_id="job")
    changed = store.store_parser_json(b'{"total":10.21}', sha256="a" * 64, job_id="job")

    assert first == same
    assert first.blob_name != changed.blob_name
    (tmp_path / "artifacts" / first.blob_name).write_bytes(b'{"total":99.99}')
    with pytest.raises(ArtifactIntegrityError, match="content hash"):
        store.load_parser_json(first)


def test_local_store_bounds_raw_and_parser_materialization(tmp_path: Path) -> None:
    store = LocalArtifactStore(
        tmp_path / "artifacts",
        max_pdf_bytes=4,
        max_parser_json_bytes=4,
    )
    with pytest.raises(ArtifactTooLargeError, match="source PDF"):
        store.store_raw_pdf_bytes(b"12345", sha256=hashlib.sha256(b"12345").hexdigest(), vendor="V")
    with pytest.raises(ArtifactTooLargeError, match="parser JSON"):
        store.store_parser_json(b'{"a":1}', sha256="a" * 64, job_id="job")

    oversized = tmp_path / "artifacts" / "pdfco-json" / "oversized.json"
    oversized.parent.mkdir(parents=True)
    oversized.write_bytes(b"12345")
    with pytest.raises(ArtifactTooLargeError):
        store.load_parser_json(
            ArtifactLocation(
                backend="local",
                storage_account_name=None,
                container=None,
                blob_name="pdfco-json/oversized.json",
            )
        )


def test_store_configuration_mismatch_is_unavailable_not_corruption(tmp_path: Path) -> None:
    local = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactUnavailableError):
        local.load_parser_json(
            ArtifactLocation(
                backend="azure_blob",
                storage_account_name="stother",
                container="parser-results",
                blob_name="result.json",
            )
        )

    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.raw_container = "raw-pdfs"
    azure.max_pdf_bytes = 100
    mismatches = (
        ArtifactLocation("local", None, None, "raw.pdf"),
        ArtifactLocation("azure_blob", "stother", "raw-pdfs", "raw.pdf"),
        ArtifactLocation("azure_blob", "stcoiportaldev", "another-container", "raw.pdf"),
    )
    for mismatch in mismatches:
        with pytest.raises(ArtifactUnavailableError):
            azure.load_raw_pdf(
                mismatch,
                sha256="a" * 64,
                content_length_bytes=10,
            )


def test_azure_blob_store_persists_exact_account_identity() -> None:
    store = AzureBlobArtifactStore(
        account_url="https://stcoiportaldev.blob.core.windows.net",
        raw_container="raw-pdfs",
        parser_container="pdfco-json",
        prefix="",
        encryption_scope=None,
    )
    try:
        assert store.storage_account_name == "stcoiportaldev"
    finally:
        store.service_client.close()
        store.credential.close()

    with pytest.raises(RuntimeError, match="exact public account HTTPS endpoint"):
        AzureBlobArtifactStore(
            account_url="https://st-coi.blob.core.windows.net",
            raw_container="raw-pdfs",
            parser_container="pdfco-json",
            prefix="",
            encryption_scope=None,
        )


def test_raw_repair_keeps_old_local_artifact_coordinate(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    payload = b"%PDF repair test"
    digest = hashlib.sha256(payload).hexdigest()
    original = store.store_raw_pdf_bytes(payload, sha256=digest, vendor="ARTOPEX")
    repaired = store.store_raw_pdf_bytes(
        payload,
        sha256=digest,
        vendor="ARTOPEX",
        repair=True,
    )

    assert repaired.blob_name != original.blob_name
    assert (
        store.load_raw_pdf(
            repaired,
            sha256=digest,
            content_length_bytes=len(payload),
        )
        == payload
    )


def test_oversized_azure_blob_is_not_downloaded_before_materialization() -> None:
    class BlobClient:
        @staticmethod
        def get_blob_properties() -> dict[str, object]:
            return {"size": 5}

        @staticmethod
        def download_blob(**_: object) -> object:
            raise AssertionError("declared oversized blob must not be downloaded")

    class ServiceClient:
        @staticmethod
        def get_blob_client(**_: object) -> BlobClient:
            return BlobClient()

    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.service_client = ServiceClient()

    with pytest.raises(ArtifactTooLargeError):
        azure._download_blob_bytes(  # noqa: SLF001
            ArtifactLocation(
                backend="azure_blob",
                storage_account_name="stcoiportaldev",
                container="pdfco-json",
                blob_name="large.json",
            ),
            max_bytes=4,
        )


def test_azure_blob_download_is_bounded_when_properties_understate_size() -> None:
    class Downloader:
        @staticmethod
        def chunks() -> list[bytes]:
            return [b"123", b"45"]

    class BlobClient:
        @staticmethod
        def get_blob_properties() -> dict[str, object]:
            return {"size": 4}

        @staticmethod
        def download_blob(**_: object) -> Downloader:
            return Downloader()

    class ServiceClient:
        @staticmethod
        def get_blob_client(**_: object) -> BlobClient:
            return BlobClient()

    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.service_client = ServiceClient()

    with pytest.raises(ArtifactTooLargeError):
        azure._download_blob_bytes(  # noqa: SLF001
            ArtifactLocation(
                backend="azure_blob",
                storage_account_name="stcoiportaldev",
                container="pdfco-json",
                blob_name="large.json",
            ),
            max_bytes=4,
        )


def test_local_raw_write_failure_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    payload = b"%PDF write failure"

    def denied(*_: object, **__: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_bytes", denied)

    with pytest.raises(ArtifactUnavailableError, match="local raw"):
        store.store_raw_pdf_bytes(
            payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            vendor="ARTOPEX",
        )


def test_local_parser_write_failure_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    def denied(*_: object, **__: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_bytes", denied)

    with pytest.raises(ArtifactUnavailableError, match="local parser"):
        store.store_parser_json(b"{}", sha256="a" * 64, job_id="job")


def test_azure_blob_write_failures_are_reported_as_unavailable() -> None:
    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.raw_container = "raw-pdfs"
    azure.parser_container = "pdfco-json"
    azure.prefix = "documents"
    azure.encryption_scope = None
    azure.max_pdf_bytes = 1024
    azure.max_parser_json_bytes = 1024
    azure._get_properties = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda **_kwargs: None
    )

    def fail_upload(**_: object) -> object:
        raise TimeoutError("backend timeout")

    azure._upload_blob = fail_upload  # type: ignore[method-assign]  # noqa: SLF001

    payload = b"%PDF Azure failure"
    with pytest.raises(ArtifactUnavailableError, match="Azure Blob raw"):
        azure.store_raw_pdf_bytes(
            payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            vendor="ARTOPEX",
        )
    with pytest.raises(ArtifactUnavailableError, match="Azure Blob parser"):
        azure.store_parser_json(b"{}", sha256="a" * 64, job_id="job")


def test_azure_blob_existing_content_is_verified_before_reuse() -> None:
    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.raw_container = "raw-pdfs"
    azure.parser_container = "pdfco-json"
    azure.prefix = "documents"
    azure.max_pdf_bytes = 1024
    payload = b"%PDF retained"
    digest = hashlib.sha256(payload).hexdigest()
    azure._get_properties = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda **_kwargs: {
            "size": len(payload),
            "metadata": {"sha256": digest},
            "version_id": "version-1",
        }
    )

    location = azure.store_raw_pdf_bytes(payload, sha256=digest, vendor="ARTOPEX")

    assert location.backend == "azure_blob"
    assert location.storage_account_name == "stcoiportaldev"
    assert location.container == "raw-pdfs"
    assert location.version_id == "version-1"


def test_azure_blob_rejects_existing_content_with_wrong_integrity_metadata() -> None:
    azure = object.__new__(AzureBlobArtifactStore)
    azure.storage_account_name = "stcoiportaldev"
    azure.raw_container = "raw-pdfs"
    azure.parser_container = "pdfco-json"
    azure.prefix = ""
    azure.max_pdf_bytes = 1024
    payload = b"%PDF retained"
    digest = hashlib.sha256(payload).hexdigest()
    azure._get_properties = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda **_kwargs: {
            "size": len(payload),
            "metadata": {"sha256": "0" * 64},
        }
    )

    with pytest.raises(ArtifactIntegrityError, match="integrity verification"):
        azure.store_raw_pdf_bytes(payload, sha256=digest, vendor="ARTOPEX")
