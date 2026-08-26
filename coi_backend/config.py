"""Environment and Azure Key Vault backed application configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_PDF_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_PARSER_JSON_BYTES = 10 * 1024 * 1024
DEFAULT_PDFCO_RESULT_HOSTS = (
    "pdf-temp-files.s3.amazonaws.com",
    "pdf-temp-files.s3.us-west-2.amazonaws.com",
    "pdf-temp-files.s3-us-west-2.amazonaws.com",
)


def runtime_root() -> Path:
    """Use the checkout root in source mode and the current directory when installed."""

    return PROJECT_ROOT if (PROJECT_ROOT / "pyproject.toml").is_file() else Path.cwd()


class ConfigurationError(RuntimeError):
    """Raised when required configuration is absent or invalid."""


def load_local_environment() -> None:
    """Load a developer .env without overriding the process environment."""

    load_dotenv(runtime_root() / ".env", override=False)


def _secret_value(secret_name: str, *, vault_url: str | None) -> str:
    """Read a Key Vault secret using Azure's default credential chain."""

    if not vault_url:
        raise ConfigurationError(
            "AZURE_KEY_VAULT_URL is required when a Key Vault secret name is configured"
        )

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ConfigurationError(
            "azure-identity and azure-keyvault-secrets are required when a Key Vault "
            "secret name is configured"
        ) from exc

    client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    try:
        response = client.get_secret(secret_name)
    except Exception as exc:
        raise ConfigurationError(
            f"unable to read Azure Key Vault secret {secret_name!r}: {type(exc).__name__}"
        ) from exc
    value = response.value
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Azure Key Vault secret {secret_name!r} has no non-empty value")
    return str(value)


def _value_from_secret(secret_name: str, key: str, *, vault_url: str | None) -> str:
    raw = _secret_value(secret_name, vault_url=vault_url)
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw

    if isinstance(payload, str):
        value = payload
    elif isinstance(payload, dict):
        value = payload.get(key)
    else:
        value = None

    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"Azure Key Vault secret {secret_name!r} must be a non-empty string or contain {key!r}"
        )
    return value.strip()


def _required_value(
    env_name: str,
    secret_env_name: str,
    *,
    secret_key: str,
    vault_url: str | None,
) -> str:
    direct = os.getenv(env_name, "").strip()
    secret_name = _key_vault_secret_name(secret_env_name)
    if direct and secret_name:
        raise ConfigurationError(f"Set only one of {env_name} or {secret_env_name}, not both")
    if direct:
        return direct
    if secret_name:
        return _value_from_secret(secret_name, secret_key, vault_url=vault_url)
    raise ConfigurationError(f"Missing required configuration: set {env_name} or {secret_env_name}")


def _key_vault_secret_name(env_name: str) -> str:
    value = os.getenv(env_name, "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9-]{1,127}", value):
        raise ConfigurationError(
            f"{env_name} must be a 1-127 character Azure Key Vault object name "
            "using only letters, numbers, and hyphens"
        )
    return value


def _azure_service_url(name: str, *, service: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} is not a valid Azure service URL") from exc
    if (
        parts.scheme != "https"
        or not hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ConfigurationError(
            f"{name} must be the exact public Azure {service} HTTPS endpoint without "
            "credentials, path, query, fragment, or a non-443 port"
        )

    if service == "Blob Storage":
        match = re.fullmatch(r"([a-z0-9]{3,24})\.blob\.core\.windows\.net", hostname)
    else:
        match = re.fullmatch(
            r"([a-z](?:[a-z0-9]|-(?!-)){1,22}[a-z0-9])\.vault\.azure\.net",
            hostname,
        )
    canonical_netlocs = {hostname, f"{hostname}:443"}
    if not match or parts.netloc.lower() not in canonical_netlocs:
        expected = (
            "https://<storage-account>.blob.core.windows.net"
            if service == "Blob Storage"
            else "https://<vault-name>.vault.azure.net"
        )
        raise ConfigurationError(f"{name} must match {expected}")
    return f"https://{hostname}"


def _azure_storage_account_url() -> str | None:
    return _azure_service_url("AZURE_STORAGE_ACCOUNT_URL", service="Blob Storage")


def _azure_key_vault_url() -> str | None:
    return _azure_service_url("AZURE_KEY_VAULT_URL", service="Key Vault")


def _azure_container(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]", value) or "--" in value:
        raise ConfigurationError(
            f"{name} must be a valid 3-63 character lowercase Azure Blob container name"
        )
    return value


def _azure_blob_prefix() -> str:
    value = os.getenv("AZURE_STORAGE_PREFIX", "").strip().strip("/")
    if not value:
        return ""
    parts = value.split("/")
    if (
        "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 for character in value)
    ):
        raise ConfigurationError("AZURE_STORAGE_PREFIX must be a safe relative blob prefix")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


def _pdfco_result_hosts() -> tuple[str, ...]:
    configured = os.getenv("PDFCO_RESULT_HOSTS", ",".join(DEFAULT_PDFCO_RESULT_HOSTS))
    hosts = tuple(part.strip().lower().rstrip(".") for part in configured.split(","))
    if not hosts or any(not host for host in hosts):
        raise ConfigurationError("PDFCO_RESULT_HOSTS must contain one or more exact hostnames")
    hostname_pattern = re.compile(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    )
    for host in hosts:
        try:
            ip_address(host)
        except ValueError:
            pass
        else:
            raise ConfigurationError("PDFCO_RESULT_HOSTS must not contain IP addresses")
        if "." not in host or not hostname_pattern.fullmatch(host):
            raise ConfigurationError("PDFCO_RESULT_HOSTS contains an invalid hostname")
    return tuple(dict.fromkeys(hosts))


def database_url_from_environment() -> str:
    """Resolve the database URL without loading unrelated ingestion settings."""

    load_local_environment()
    vault_url = _azure_key_vault_url()
    return _required_value(
        "DATABASE_URL",
        "DATABASE_SECRET_NAME",
        secret_key="DATABASE_URL",
        vault_url=vault_url,
    )


def database_connect_timeout_from_environment() -> int:
    load_local_environment()
    return _positive_int("DATABASE_CONNECT_TIMEOUT_SECONDS", 10)


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings for one ingestion command."""

    database_url: str = field(repr=False)
    pdfco_api_key: str = field(repr=False)
    input_dir: Path
    document_type: str
    vendor: str
    pdfco_base_url: str
    pdfco_result_hosts: tuple[str, ...]
    poll_timeout_seconds: int
    processing_stale_after_seconds: int
    database_connect_timeout_seconds: int
    max_pdf_bytes: int
    max_parser_json_bytes: int
    source_min_age_seconds: int
    artifact_store: str
    local_artifact_dir: Path
    archive_dir: Path | None
    quarantine_dir: Path | None
    azure_storage_account_url: str | None
    azure_raw_container: str
    azure_parser_container: str
    azure_storage_prefix: str
    azure_storage_encryption_scope: str | None = field(default=None, repr=False)
    log_level: str = "INFO"

    @classmethod
    def from_environment(
        cls,
        *,
        document_type: str,
        input_dir: str | Path | None = None,
        vendor: str | None = None,
    ) -> Settings:
        load_local_environment()
        normalized_type = document_type.strip().lower()
        if normalized_type not in {"invoice", "oa"}:
            raise ConfigurationError("document_type must be 'invoice' or 'oa'")

        vault_url = _azure_key_vault_url()
        database_url = _required_value(
            "DATABASE_URL",
            "DATABASE_SECRET_NAME",
            secret_key="DATABASE_URL",
            vault_url=vault_url,
        )
        pdfco_api_key = _required_value(
            "PDFCO_API_KEY",
            "PDFCO_API_KEY_SECRET_NAME",
            secret_key="PDFCO_API_KEY",
            vault_url=vault_url,
        )

        input_env = "INVOICE_INPUT_DIR" if normalized_type == "invoice" else "OA_INPUT_DIR"
        configured_input = str(input_dir or os.getenv(input_env, "")).strip()
        if not configured_input:
            configured_input = str(
                runtime_root()
                / "data"
                / "artopex"
                / ("invoices" if normalized_type == "invoice" else "oas")
            )
        input_path = Path(configured_input).expanduser().resolve()

        vendor_name = (vendor or os.getenv("VENDOR_NAME", "ARTOPEX")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,63}", vendor_name):
            raise ConfigurationError(
                "VENDOR_NAME must be a 1-64 character safe slug using "
                "A-Z, 0-9, dot, underscore, or hyphen"
            )

        base_url = os.getenv("PDFCO_BASE_URL", "https://api.pdf.co/v1").strip().rstrip("/")
        base_parts = urlsplit(base_url)
        if (
            base_parts.scheme != "https"
            or not base_parts.hostname
            or base_parts.username
            or base_parts.password
            or base_parts.query
            or base_parts.fragment
        ):
            raise ConfigurationError(
                "PDFCO_BASE_URL must be an HTTPS URL without credentials, query, or fragment"
            )

        artifact_path = (
            Path(os.getenv("LOCAL_ARTIFACT_DIR", str(runtime_root() / "var" / "artifacts")))
            .expanduser()
            .resolve()
        )

        artifact_store = os.getenv("ARTIFACT_STORE", "local").strip().lower()
        if artifact_store not in {"local", "azure_blob"}:
            raise ConfigurationError("ARTIFACT_STORE must be 'local' or 'azure_blob'")
        storage_account_url = _azure_storage_account_url()
        raw_container = _azure_container("AZURE_RAW_CONTAINER", "raw-pdfs")
        parser_container = _azure_container("AZURE_PARSER_CONTAINER", "pdfco-json")
        prefix = _azure_blob_prefix()
        encryption_scope = os.getenv("AZURE_STORAGE_ENCRYPTION_SCOPE", "").strip() or None
        if artifact_store == "azure_blob" and not storage_account_url:
            raise ConfigurationError(
                "ARTIFACT_STORE=azure_blob requires non-empty AZURE_STORAGE_ACCOUNT_URL"
            )
        if artifact_store == "local" and (storage_account_url or encryption_scope):
            raise ConfigurationError(
                "Azure Blob storage settings require ARTIFACT_STORE=azure_blob"
            )

        archive_env = "INVOICE_ARCHIVE_DIR" if normalized_type == "invoice" else "OA_ARCHIVE_DIR"
        quarantine_env = (
            "INVOICE_QUARANTINE_DIR" if normalized_type == "invoice" else "OA_QUARANTINE_DIR"
        )

        def optional_path(name: str) -> Path | None:
            value = os.getenv(name, "").strip()
            return Path(value).expanduser().resolve() if value else None

        archive_dir = optional_path(archive_env)
        quarantine_dir = optional_path(quarantine_env)
        if archive_dir == input_path or quarantine_dir == input_path:
            raise ConfigurationError("archive and quarantine directories must differ from input")
        if archive_dir is not None and archive_dir == quarantine_dir:
            raise ConfigurationError("archive and quarantine directories must differ")

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is not a recognized Python log level")

        return cls(
            database_url=database_url,
            pdfco_api_key=pdfco_api_key,
            input_dir=input_path,
            document_type=normalized_type,
            vendor=vendor_name,
            pdfco_base_url=base_url,
            pdfco_result_hosts=_pdfco_result_hosts(),
            poll_timeout_seconds=_positive_int("PDFCO_POLL_TIMEOUT_SECONDS", 180),
            processing_stale_after_seconds=_positive_int("PROCESSING_STALE_AFTER_SECONDS", 3600),
            database_connect_timeout_seconds=_positive_int("DATABASE_CONNECT_TIMEOUT_SECONDS", 10),
            max_pdf_bytes=_positive_int("MAX_PDF_BYTES", DEFAULT_MAX_PDF_BYTES),
            max_parser_json_bytes=_positive_int(
                "MAX_PARSER_JSON_BYTES", DEFAULT_MAX_PARSER_JSON_BYTES
            ),
            source_min_age_seconds=_nonnegative_int("SOURCE_MIN_AGE_SECONDS", 60),
            artifact_store=artifact_store,
            local_artifact_dir=artifact_path,
            archive_dir=archive_dir,
            quarantine_dir=quarantine_dir,
            azure_storage_account_url=storage_account_url,
            azure_raw_container=raw_container,
            azure_parser_container=parser_container,
            azure_storage_prefix=prefix,
            azure_storage_encryption_scope=encryption_scope,
            log_level=log_level,
        )
