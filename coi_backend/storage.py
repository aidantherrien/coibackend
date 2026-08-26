"""Local and Azure Blob-backed immutable ingestion artifact storage."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .config import DEFAULT_MAX_PARSER_JSON_BYTES, DEFAULT_MAX_PDF_BYTES, Settings


@dataclass(frozen=True)
class ArtifactLocation:
    backend: str
    storage_account_name: str | None
    container: str | None
    blob_name: str
    version_id: str | None = None


class ArtifactError(RuntimeError):
    """Base class for retained-artifact failures."""


class ArtifactIntegrityError(ArtifactError):
    """The named evidence is definitively missing, corrupt, or unsafe."""


class ArtifactUnavailableError(ArtifactError):
    """Evidence could not be checked because its backend is temporarily unavailable."""


class ArtifactTooLargeError(ArtifactError):
    """An artifact exceeds the configured materialization limit."""


class ArtifactStore(Protocol):
    def store_raw_pdf_bytes(
        self,
        payload: bytes,
        *,
        sha256: str,
        vendor: str,
        repair: bool = False,
    ) -> ArtifactLocation: ...

    def store_raw_pdf(self, path: Path, *, sha256: str, vendor: str) -> ArtifactLocation: ...

    def load_raw_pdf(
        self,
        location: ArtifactLocation,
        *,
        sha256: str,
        content_length_bytes: int,
    ) -> bytes: ...

    def store_parser_json(
        self,
        raw_json: bytes,
        *,
        sha256: str,
        job_id: str,
    ) -> ArtifactLocation: ...

    def load_parser_json(self, location: ArtifactLocation) -> dict[str, Any]: ...


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._")
    return cleaned or "document.pdf"


def _safe_vendor(vendor: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", vendor):
        raise RuntimeError("vendor must be a safe path component")
    return vendor.lower()


def _validated_sha256(sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise RuntimeError("artifact SHA-256 must be 64 lowercase hexadecimal characters")
    return sha256


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ensure_within_limit(payload: bytes, *, max_bytes: int, label: str) -> None:
    if len(payload) > max_bytes:
        raise ArtifactTooLargeError(f"{label} exceeds the configured size limit")


def _read_local_bytes(path: Path, *, max_bytes: int) -> bytes:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ArtifactUnavailableError("unable to inspect retained local artifact") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise ArtifactIntegrityError("retained local artifact must not be a symbolic link")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ArtifactIntegrityError("retained local artifact is not a regular file")
    if file_stat.st_size > max_bytes:
        raise ArtifactTooLargeError("retained local artifact exceeds the configured size limit")

    chunks: list[bytes] = []
    count = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(min(1024 * 1024, max_bytes - count + 1)):
                count += len(block)
                if count > max_bytes:
                    raise ArtifactTooLargeError(
                        "retained local artifact exceeds the configured size limit"
                    )
                chunks.append(block)
    except (ArtifactError, FileNotFoundError):
        raise
    except OSError as exc:
        raise ArtifactUnavailableError("unable to read retained local artifact") from exc
    return b"".join(chunks)


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError("retained parser artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ArtifactIntegrityError("retained parser artifact must contain a JSON object")
    return payload


def _result_sha256_from_blob_name(blob_name: str) -> str:
    match = re.search(r"-([0-9a-f]{64})\.json$", blob_name)
    if not match:
        raise ArtifactIntegrityError("parser artifact blob name has no content hash")
    return match.group(1)


class LocalArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        max_parser_json_bytes: int = DEFAULT_MAX_PARSER_JSON_BYTES,
    ) -> None:
        self.root = root
        self.max_pdf_bytes = max_pdf_bytes
        self.max_parser_json_bytes = max_parser_json_bytes

    def store_raw_pdf(self, path: Path, *, sha256: str, vendor: str) -> ArtifactLocation:
        try:
            payload = _read_local_bytes(path, max_bytes=self.max_pdf_bytes)
        except FileNotFoundError as exc:
            raise ArtifactUnavailableError("source PDF does not exist") from exc
        return self.store_raw_pdf_bytes(payload, sha256=sha256, vendor=vendor)

    def store_raw_pdf_bytes(
        self,
        payload: bytes,
        *,
        sha256: str,
        vendor: str,
        repair: bool = False,
    ) -> ArtifactLocation:
        _ensure_within_limit(payload, max_bytes=self.max_pdf_bytes, label="source PDF")
        expected_sha256 = _validated_sha256(sha256)
        if _sha256_bytes(payload) != expected_sha256:
            raise RuntimeError("source PDF bytes do not match the supplied SHA-256")
        filename = f"source-repair-{uuid.uuid4().hex}.pdf" if repair else "source.pdf"
        relative = Path("raw-pdfs") / _safe_vendor(vendor) / expected_sha256 / filename
        root = self.root.resolve()
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:  # pragma: no cover - guarded by safe components
            raise RuntimeError("local artifact path escapes the configured root") from exc
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (
                    _sha256_bytes(_read_local_bytes(destination, max_bytes=self.max_pdf_bytes))
                    != expected_sha256
                ):
                    raise ArtifactIntegrityError(
                        f"existing local artifact has the wrong hash: {relative}"
                    )
            else:
                temporary.write_bytes(payload)
                temporary.replace(destination)
        except RuntimeError:
            raise
        except OSError as exc:
            raise ArtifactUnavailableError("unable to retain local raw artifact") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return ArtifactLocation(
            backend="local",
            storage_account_name=None,
            container=None,
            blob_name=relative.as_posix(),
        )

    def load_raw_pdf(
        self,
        location: ArtifactLocation,
        *,
        sha256: str,
        content_length_bytes: int,
    ) -> bytes:
        if (
            location.backend != "local"
            or location.storage_account_name is not None
            or location.container is not None
        ):
            raise ArtifactUnavailableError("raw artifact is not in the configured local store")
        if content_length_bytes > self.max_pdf_bytes:
            raise ArtifactTooLargeError("retained raw PDF exceeds the configured size limit")
        root = self.root.resolve()
        path = (root / location.blob_name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                "local raw artifact path escapes the configured root"
            ) from exc
        try:
            raw = _read_local_bytes(path, max_bytes=self.max_pdf_bytes)
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("retained local raw artifact does not exist") from exc
        if len(raw) != content_length_bytes or _sha256_bytes(raw) != sha256:
            raise ArtifactIntegrityError(
                "retained local raw artifact failed integrity verification"
            )
        return raw

    def store_parser_json(
        self,
        raw_json: bytes,
        *,
        sha256: str,
        job_id: str,
    ) -> ArtifactLocation:
        body = raw_json
        _ensure_within_limit(
            body,
            max_bytes=self.max_parser_json_bytes,
            label="parser JSON",
        )
        result_sha256 = _sha256_bytes(body)
        relative = (
            Path("pdfco-json")
            / _validated_sha256(sha256)
            / f"{_safe_filename(job_id)}-{result_sha256}.json"
        )
        root = self.root.resolve()
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:  # pragma: no cover - guarded by safe components
            raise RuntimeError("local artifact path escapes the configured root") from exc
        temporary = destination.with_suffix(".json.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if _read_local_bytes(destination, max_bytes=self.max_parser_json_bytes) != body:
                    raise RuntimeError(
                        "existing parser artifact failed immutable-content verification"
                    )
            else:
                temporary.write_bytes(body)
                temporary.replace(destination)
        except RuntimeError:
            raise
        except OSError as exc:
            raise ArtifactUnavailableError("unable to retain local parser artifact") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return ArtifactLocation(
            backend="local",
            storage_account_name=None,
            container=None,
            blob_name=relative.as_posix(),
        )

    def load_parser_json(self, location: ArtifactLocation) -> dict[str, Any]:
        if (
            location.backend != "local"
            or location.storage_account_name is not None
            or location.container is not None
        ):
            raise ArtifactUnavailableError("parser artifact is not in the configured local store")
        root = self.root.resolve()
        path = (root / location.blob_name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactIntegrityError("local artifact path escapes the configured root") from exc
        try:
            raw = _read_local_bytes(path, max_bytes=self.max_parser_json_bytes)
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("retained parser artifact does not exist") from exc
        if _sha256_bytes(raw) != _result_sha256_from_blob_name(location.blob_name):
            raise ArtifactIntegrityError("retained parser artifact content hash does not match")
        return _decode_json_object(raw)


class AzureBlobArtifactStore:
    def __init__(
        self,
        *,
        account_url: str,
        raw_container: str,
        parser_container: str,
        prefix: str,
        encryption_scope: str | None,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        max_parser_json_bytes: int = DEFAULT_MAX_PARSER_JSON_BYTES,
    ) -> None:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "azure-identity and azure-storage-blob are required when Azure Blob "
                "storage is configured"
            ) from exc
        try:
            parts = urlsplit(account_url)
            hostname = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise RuntimeError("Azure Blob account URL is invalid") from exc
        account_match = (
            re.fullmatch(r"([a-z0-9]{3,24})\.blob\.core\.windows\.net", hostname or "")
            if hostname
            else None
        )
        if (
            parts.scheme != "https"
            or not account_match
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or port not in {None, 443}
        ):
            raise RuntimeError(
                "Azure Blob account URL must be the exact public account HTTPS endpoint"
            )
        self.storage_account_name = account_match.group(1)
        self.raw_container = raw_container
        self.parser_container = parser_container
        self.prefix = prefix.strip("/")
        self.encryption_scope = encryption_scope
        self.max_pdf_bytes = max_pdf_bytes
        self.max_parser_json_bytes = max_parser_json_bytes
        self.credential = DefaultAzureCredential()
        self.service_client = BlobServiceClient(
            account_url=account_url,
            credential=self.credential,
        )

    def _blob_name(self, *parts: str) -> str:
        components = [self.prefix, *[part.strip("/") for part in parts]]
        return "/".join(component for component in components if component)

    def _blob_client(
        self,
        *,
        container: str,
        blob_name: str,
        version_id: str | None = None,
    ) -> Any:
        return self.service_client.get_blob_client(
            container=container,
            blob=blob_name,
            version_id=version_id,
        )

    @staticmethod
    def _response_value(response: Any, name: str, default: Any = None) -> Any:
        if isinstance(response, dict):
            return response.get(name, default)
        return getattr(response, name, default)

    @classmethod
    def _is_not_found(cls, exc: Exception) -> bool:
        return (
            type(exc).__name__ == "ResourceNotFoundError"
            or getattr(exc, "status_code", None) == 404
            or getattr(exc, "error_code", None) in {"BlobNotFound", "ContainerNotFound"}
        )

    @classmethod
    def _is_existing_blob_conflict(cls, exc: Exception) -> bool:
        return type(exc).__name__ == "ResourceExistsError" or (
            getattr(exc, "status_code", None) == 409
            and getattr(exc, "error_code", None) in {"BlobAlreadyExists", "BlobAlreadyPresent"}
        )

    def _get_properties(
        self,
        *,
        container: str,
        blob_name: str,
        version_id: str | None = None,
        missing_ok: bool,
    ) -> Any | None:
        client = self._blob_client(
            container=container,
            blob_name=blob_name,
            version_id=version_id,
        )
        try:
            return client.get_blob_properties()
        except Exception as exc:
            if missing_ok and self._is_not_found(exc):
                return None
            if self._is_not_found(exc):
                raise ArtifactIntegrityError("retained Azure Blob artifact does not exist") from exc
            raise ArtifactUnavailableError(
                "unable to inspect retained Azure Blob artifact"
            ) from exc

    @classmethod
    def _verify_properties(
        cls,
        properties: Any,
        *,
        sha256: str,
        content_length_bytes: int,
        metadata_key: str,
    ) -> None:
        metadata = cls._response_value(properties, "metadata", {}) or {}
        if (
            int(cls._response_value(properties, "size", -1)) != content_length_bytes
            or metadata.get(metadata_key) != sha256
        ):
            raise ArtifactIntegrityError(
                "retained Azure Blob artifact failed integrity verification"
            )

    def _upload_blob(
        self,
        *,
        container: str,
        blob_name: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> Any:
        from azure.storage.blob import ContentSettings

        arguments: dict[str, Any] = {
            "data": body,
            "length": len(body),
            "overwrite": False,
            "metadata": metadata,
            "content_settings": ContentSettings(content_type=content_type),
            "validate_content": True,
        }
        if self.encryption_scope:
            arguments["encryption_scope"] = self.encryption_scope
        return self._blob_client(container=container, blob_name=blob_name).upload_blob(**arguments)

    def _store_blob(
        self,
        *,
        container: str,
        blob_name: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        content_sha256: str,
        metadata_key: str,
        skip_existing_check: bool = False,
    ) -> ArtifactLocation:
        properties = None
        if not skip_existing_check:
            properties = self._get_properties(
                container=container,
                blob_name=blob_name,
                missing_ok=True,
            )
        if properties is not None:
            self._verify_properties(
                properties,
                sha256=content_sha256,
                content_length_bytes=len(body),
                metadata_key=metadata_key,
            )
            return ArtifactLocation(
                backend="azure_blob",
                storage_account_name=self.storage_account_name,
                container=container,
                blob_name=blob_name,
                version_id=self._response_value(properties, "version_id"),
            )

        try:
            response = self._upload_blob(
                container=container,
                blob_name=blob_name,
                body=body,
                content_type=content_type,
                metadata=metadata,
            )
        except Exception as exc:
            if not self._is_existing_blob_conflict(exc):
                raise ArtifactUnavailableError("unable to retain Azure Blob artifact") from exc
            properties = self._get_properties(
                container=container,
                blob_name=blob_name,
                missing_ok=False,
            )
            self._verify_properties(
                properties,
                sha256=content_sha256,
                content_length_bytes=len(body),
                metadata_key=metadata_key,
            )
            return ArtifactLocation(
                backend="azure_blob",
                storage_account_name=self.storage_account_name,
                container=container,
                blob_name=blob_name,
                version_id=self._response_value(properties, "version_id"),
            )

        return ArtifactLocation(
            backend="azure_blob",
            storage_account_name=self.storage_account_name,
            container=container,
            blob_name=blob_name,
            version_id=self._response_value(response, "version_id"),
        )

    def _download_blob_bytes(
        self,
        location: ArtifactLocation,
        *,
        max_bytes: int,
    ) -> bytes:
        if (
            location.backend != "azure_blob"
            or location.storage_account_name != self.storage_account_name
            or location.container is None
        ):
            raise ArtifactUnavailableError("artifact is not in the configured Azure Blob store")
        client = self._blob_client(
            container=location.container,
            blob_name=location.blob_name,
            version_id=location.version_id,
        )
        try:
            properties = client.get_blob_properties()
            content_length = int(self._response_value(properties, "size", -1))
            if content_length > max_bytes:
                raise ArtifactTooLargeError(
                    "retained Azure Blob artifact exceeds configured size limit"
                )
            downloader = client.download_blob(offset=0, length=max_bytes + 1)
            chunks: list[bytes] = []
            count = 0
            for block in downloader.chunks():
                count += len(block)
                if count > max_bytes:
                    raise ArtifactTooLargeError(
                        "retained Azure Blob artifact exceeds configured size limit"
                    )
                chunks.append(block)
            return b"".join(chunks)
        except ArtifactError:
            raise
        except Exception as exc:
            if self._is_not_found(exc):
                raise ArtifactIntegrityError("retained Azure Blob artifact does not exist") from exc
            if "checksum" in type(exc).__name__.lower():
                raise ArtifactIntegrityError(
                    "retained Azure Blob artifact checksum failed"
                ) from exc
            raise ArtifactUnavailableError("unable to read retained Azure Blob artifact") from exc

    def store_raw_pdf(self, path: Path, *, sha256: str, vendor: str) -> ArtifactLocation:
        try:
            payload = _read_local_bytes(path, max_bytes=self.max_pdf_bytes)
        except FileNotFoundError as exc:
            raise ArtifactUnavailableError("source PDF does not exist") from exc
        return self.store_raw_pdf_bytes(payload, sha256=sha256, vendor=vendor)

    def store_raw_pdf_bytes(
        self,
        payload: bytes,
        *,
        sha256: str,
        vendor: str,
        repair: bool = False,
    ) -> ArtifactLocation:
        _ensure_within_limit(payload, max_bytes=self.max_pdf_bytes, label="source PDF")
        expected_sha256 = _validated_sha256(sha256)
        if _sha256_bytes(payload) != expected_sha256:
            raise RuntimeError("source PDF bytes do not match the supplied SHA-256")
        filename = f"source-repair-{uuid.uuid4().hex}.pdf" if repair else "source.pdf"
        blob_name = self._blob_name(
            _safe_vendor(vendor),
            expected_sha256,
            filename,
        )
        try:
            return self._store_blob(
                container=self.raw_container,
                blob_name=blob_name,
                body=payload,
                content_type="application/pdf",
                metadata={"sha256": expected_sha256, "vendor": vendor},
                content_sha256=expected_sha256,
                metadata_key="sha256",
                skip_existing_check=repair,
            )
        except ArtifactUnavailableError as exc:
            raise ArtifactUnavailableError("unable to retain Azure Blob raw artifact") from exc

    def load_raw_pdf(
        self,
        location: ArtifactLocation,
        *,
        sha256: str,
        content_length_bytes: int,
    ) -> bytes:
        if (
            location.backend != "azure_blob"
            or location.storage_account_name != self.storage_account_name
            or location.container != self.raw_container
        ):
            raise ArtifactUnavailableError(
                "raw artifact is outside the configured Azure Blob backend, account, or container"
            )
        if content_length_bytes > self.max_pdf_bytes:
            raise ArtifactTooLargeError("retained raw PDF exceeds the configured size limit")
        raw = self._download_blob_bytes(location, max_bytes=self.max_pdf_bytes)
        if len(raw) != content_length_bytes or _sha256_bytes(raw) != sha256:
            raise ArtifactIntegrityError(
                "retained Azure Blob raw artifact failed integrity verification"
            )
        return raw

    def store_parser_json(
        self,
        raw_json: bytes,
        *,
        sha256: str,
        job_id: str,
    ) -> ArtifactLocation:
        body = raw_json
        _ensure_within_limit(
            body,
            max_bytes=self.max_parser_json_bytes,
            label="parser JSON",
        )
        result_sha256 = _sha256_bytes(body)
        blob_name = self._blob_name(
            _validated_sha256(sha256),
            f"{_safe_filename(job_id)}-{result_sha256}.json",
        )
        try:
            return self._store_blob(
                container=self.parser_container,
                blob_name=blob_name,
                body=body,
                content_type="application/json",
                metadata={
                    "document_sha256": sha256,
                    "result_sha256": result_sha256,
                    "pdfco_job_id": _safe_filename(job_id),
                },
                content_sha256=result_sha256,
                metadata_key="result_sha256",
            )
        except ArtifactUnavailableError as exc:
            raise ArtifactUnavailableError("unable to retain Azure Blob parser artifact") from exc

    def load_parser_json(self, location: ArtifactLocation) -> dict[str, Any]:
        if (
            location.backend != "azure_blob"
            or location.storage_account_name != self.storage_account_name
            or location.container != self.parser_container
        ):
            raise ArtifactUnavailableError(
                "parser artifact is outside the configured Azure Blob backend, "
                "account, or container"
            )
        raw = self._download_blob_bytes(location, max_bytes=self.max_parser_json_bytes)
        if _sha256_bytes(raw) != _result_sha256_from_blob_name(location.blob_name):
            raise ArtifactIntegrityError("retained parser artifact content hash does not match")
        return _decode_json_object(raw)


def artifact_store_from_settings(settings: Settings) -> ArtifactStore:
    if settings.artifact_store == "azure_blob":
        if not settings.azure_storage_account_url:
            raise RuntimeError("ARTIFACT_STORE=azure_blob requires AZURE_STORAGE_ACCOUNT_URL")
        return AzureBlobArtifactStore(
            account_url=settings.azure_storage_account_url,
            raw_container=settings.azure_raw_container,
            parser_container=settings.azure_parser_container,
            prefix=settings.azure_storage_prefix,
            encryption_scope=settings.azure_storage_encryption_scope,
            max_pdf_bytes=settings.max_pdf_bytes,
            max_parser_json_bytes=settings.max_parser_json_bytes,
        )
    if settings.artifact_store == "local":
        return LocalArtifactStore(
            settings.local_artifact_dir,
            max_pdf_bytes=settings.max_pdf_bytes,
            max_parser_json_bytes=settings.max_parser_json_bytes,
        )
    raise RuntimeError("ARTIFACT_STORE must be 'local' or 'azure_blob'")
