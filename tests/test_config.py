from pathlib import Path

import pytest

from coi_backend.config import ConfigurationError, Settings

CONFIG_KEYS = (
    "DATABASE_URL",
    "DATABASE_SECRET_NAME",
    "PDFCO_API_KEY",
    "PDFCO_API_KEY_SECRET_NAME",
    "INVOICE_INPUT_DIR",
    "OA_INPUT_DIR",
    "AZURE_KEY_VAULT_URL",
    "AZURE_STORAGE_ACCOUNT_URL",
    "AZURE_RAW_CONTAINER",
    "AZURE_PARSER_CONTAINER",
    "AZURE_STORAGE_PREFIX",
    "AZURE_STORAGE_ENCRYPTION_SCOPE",
    "ARTIFACT_STORE",
    "PDFCO_BASE_URL",
    "PDFCO_RESULT_HOSTS",
    "PDFCO_POLL_TIMEOUT_SECONDS",
    "MAX_PDF_BYTES",
    "MAX_PARSER_JSON_BYTES",
    "SOURCE_MIN_AGE_SECONDS",
    "PROCESSING_STALE_AFTER_SECONDS",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "LOCAL_ARTIFACT_DIR",
    "VENDOR_NAME",
    "LOG_LEVEL",
    "INVOICE_ARCHIVE_DIR",
    "OA_ARCHIVE_DIR",
    "INVOICE_QUARANTINE_DIR",
    "OA_QUARANTINE_DIR",
)


@pytest.fixture(autouse=True)
def _do_not_load_developer_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coi_backend.config.load_local_environment", lambda: None)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_use_direct_environment_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "super-secret")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    settings = Settings.from_environment(document_type="invoice")
    assert settings.input_dir == tmp_path.resolve()
    assert settings.vendor == "ARTOPEX"
    assert "super-secret" not in repr(settings)
    assert "postgresql://" not in repr(settings)
    assert settings.max_pdf_bytes == 25 * 1024 * 1024
    assert settings.max_parser_json_bytes == 10 * 1024 * 1024
    assert settings.source_min_age_seconds == 60
    assert settings.artifact_store == "local"
    assert "pdf-temp-files.s3.amazonaws.com" in settings.pdfco_result_hosts


def test_settings_fail_closed_when_direct_and_secret_values_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("DATABASE_SECRET_NAME", "database-url")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("OA_INPUT_DIR", str(tmp_path))
    with pytest.raises(ConfigurationError, match="only one"):
        Settings.from_environment(document_type="oa")


def test_settings_require_pdfco_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    with pytest.raises(ConfigurationError, match="PDFCO_API_KEY"):
        Settings.from_environment(document_type="invoice")


def test_settings_reject_credentialed_pdfco_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("PDFCO_BASE_URL", "https://trusted.example@untrusted.invalid/v1")
    with pytest.raises(ConfigurationError, match="without credentials"):
        Settings.from_environment(document_type="invoice")


def test_settings_reject_unsafe_vendor_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("VENDOR_NAME", "../../outside")
    with pytest.raises(ConfigurationError, match="safe slug"):
        Settings.from_environment(document_type="invoice")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MAX_PDF_BYTES", "0", "greater than zero"),
        ("MAX_PARSER_JSON_BYTES", "-1", "greater than zero"),
        ("SOURCE_MIN_AGE_SECONDS", "-1", "zero or greater"),
    ],
)
def test_settings_reject_invalid_io_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_environment(document_type="invoice")


def test_azure_blob_store_requires_explicit_complete_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "azure_blob")
    with pytest.raises(ConfigurationError, match="AZURE_STORAGE_ACCOUNT_URL"):
        Settings.from_environment(document_type="invoice")

    monkeypatch.setenv(
        "AZURE_STORAGE_ACCOUNT_URL",
        "https://stcoiportaldev.blob.core.windows.net",
    )
    monkeypatch.setenv("AZURE_STORAGE_PREFIX", "documents")
    settings = Settings.from_environment(document_type="invoice")
    assert settings.artifact_store == "azure_blob"
    assert settings.azure_storage_account_url == ("https://stcoiportaldev.blob.core.windows.net")
    assert settings.azure_raw_container == "raw-pdfs"
    assert settings.azure_parser_container == "pdfco-json"
    assert settings.azure_storage_prefix == "documents"


@pytest.mark.parametrize(
    "account_url",
    (
        "http://stcoiportaldev.blob.core.windows.net",
        "https://stcoiportaldev.blob.core.windows.net.attacker.example",
        "https://stcoiportaldev.blob.core.windows.net@attacker.example",
        "https://attacker@stcoiportaldev.blob.core.windows.net",
        "https://stcoiportaldev.blob.core.windows.net/container",
        "https://stcoiportaldev.blob.core.windows.net?comp=list",
        "https://stcoiportaldev.blob.core.windows.net#fragment",
        "https://stcoiportaldev.blob.core.windows.net:8443",
        "https://[::1",
        "https://st-coi.blob.core.windows.net",
        "https://stcoiportaldev.queue.core.windows.net",
        "https://stcoiportaldev.privatelink.blob.core.windows.net",
    ),
)
def test_settings_reject_untrusted_azure_storage_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    account_url: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "azure_blob")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", account_url)

    with pytest.raises(ConfigurationError, match="AZURE_STORAGE_ACCOUNT_URL"):
        Settings.from_environment(document_type="invoice")


def test_settings_accept_public_storage_fqdn_with_private_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "azure_blob")
    monkeypatch.setenv(
        "AZURE_STORAGE_ACCOUNT_URL",
        "https://stcoiportaldev.blob.core.windows.net:443",
    )

    settings = Settings.from_environment(document_type="invoice")

    assert settings.azure_storage_account_url == ("https://stcoiportaldev.blob.core.windows.net")


def test_settings_accept_arm_storage_endpoint_with_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "azure_blob")
    monkeypatch.setenv(
        "AZURE_STORAGE_ACCOUNT_URL",
        "https://stcoiportaldev.blob.core.windows.net/",
    )

    settings = Settings.from_environment(document_type="invoice")

    assert settings.azure_storage_account_url == ("https://stcoiportaldev.blob.core.windows.net")


def test_key_vault_secret_name_requires_vault_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_SECRET_NAME", "database-url")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))

    with pytest.raises(ConfigurationError, match="AZURE_KEY_VAULT_URL"):
        Settings.from_environment(document_type="invoice")


@pytest.mark.parametrize(
    "vault_url",
    (
        "http://kv-coi-portal-dev.vault.azure.net",
        "https://kv-coi-portal-dev.vault.azure.net.attacker.example",
        "https://kv-coi-portal-dev.vault.azure.net@attacker.example",
        "https://attacker@kv-coi-portal-dev.vault.azure.net",
        "https://kv-coi-portal-dev.vault.azure.net/secrets/database-url",
        "https://kv-coi-portal-dev.vault.azure.net?api-version=7.4",
        "https://kv-coi-portal-dev.vault.azure.net#fragment",
        "https://kv-coi-portal-dev.vault.azure.net:444",
        "https://[::1",
        "https://1-coi-portal-dev.vault.azure.net",
        "https://kv--coi.vault.azure.net",
        "https://kv-coi-portal-dev.privatelink.vaultcore.azure.net",
    ),
)
def test_settings_reject_untrusted_key_vault_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_url: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", vault_url)

    with pytest.raises(ConfigurationError, match="AZURE_KEY_VAULT_URL"):
        Settings.from_environment(document_type="invoice")


@pytest.mark.parametrize(
    "secret_name",
    ("database_url", "folder/database-url", "database.url", "has space", "a" * 128),
)
def test_settings_reject_invalid_key_vault_secret_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_name: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY_SECRET_NAME", secret_name)
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://kv-coi-portal-dev.vault.azure.net")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))

    with pytest.raises(ConfigurationError, match="PDFCO_API_KEY_SECRET_NAME"):
        Settings.from_environment(document_type="invoice")


def test_settings_accept_valid_key_vault_secret_name_and_canonical_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    observed: dict[str, str | None] = {}

    def secret_value(secret_name: str, *, vault_url: str | None) -> str:
        observed["secret_name"] = secret_name
        observed["vault_url"] = vault_url
        return "key-from-vault"

    monkeypatch.setattr("coi_backend.config._secret_value", secret_value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY_SECRET_NAME", "pdfco-api-key")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://kv-coi-portal-dev.vault.azure.net:443")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))

    settings = Settings.from_environment(document_type="invoice")

    assert settings.pdfco_api_key == "key-from-vault"
    assert observed == {
        "secret_name": "pdfco-api-key",
        "vault_url": "https://kv-coi-portal-dev.vault.azure.net",
    }


def test_settings_accept_arm_key_vault_uri_with_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    observed: dict[str, str | None] = {}

    def secret_value(secret_name: str, *, vault_url: str | None) -> str:
        observed["secret_name"] = secret_name
        observed["vault_url"] = vault_url
        return "key-from-vault"

    monkeypatch.setattr("coi_backend.config._secret_value", secret_value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY_SECRET_NAME", "pdfco-api-key")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://kv-coi-portal-dev.vault.azure.net/")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))

    Settings.from_environment(document_type="invoice")

    assert observed["vault_url"] == "https://kv-coi-portal-dev.vault.azure.net"


@pytest.mark.parametrize("container", ("UPPERCASE", "ab", "bad--name", "bad_name"))
def test_settings_reject_invalid_azure_container_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    container: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AZURE_RAW_CONTAINER", container)

    with pytest.raises(ConfigurationError, match="container name"):
        Settings.from_environment(document_type="invoice")


def test_lifecycle_directories_must_be_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("INVOICE_ARCHIVE_DIR", str(tmp_path))
    with pytest.raises(ConfigurationError, match="must differ from input"):
        Settings.from_environment(document_type="invoice")


@pytest.mark.parametrize(
    "hosts",
    ("", "127.0.0.1", "localhost", "*.amazonaws.com", "trusted.example/path"),
)
def test_result_host_allowlist_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hosts: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("PDFCO_API_KEY", "key")
    monkeypatch.setenv("INVOICE_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("PDFCO_RESULT_HOSTS", hosts)
    with pytest.raises(ConfigurationError, match="PDFCO_RESULT_HOSTS"):
        Settings.from_environment(document_type="invoice")
