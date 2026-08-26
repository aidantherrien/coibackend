from decimal import Decimal
from io import BytesIO

import pytest
import requests

from coi_backend.pdfco import PdfCoClient, PdfCoError


def test_result_session_never_inherits_api_key() -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        poll_timeout_seconds=1,
    )
    try:
        assert client._api.headers["x-api-key"] == "secret-key"  # noqa: SLF001
        assert "x-api-key" not in client._results.headers  # noqa: SLF001
    finally:
        client.close()


def test_authenticated_calls_refuse_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        poll_timeout_seconds=1,
    )
    response = requests.Response()
    response.status_code = 302
    response.headers["Location"] = "https://untrusted.invalid/capture"
    response.raw = BytesIO()
    observed: dict[str, object] = {}

    def redirected_post(*_: object, **kwargs: object) -> requests.Response:
        observed.update(kwargs)
        return response

    monkeypatch.setattr(client._api, "post", redirected_post)  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError, match="redirect"):
            client.start_parse("https://example.invalid/upload.pdf")
        assert observed["allow_redirects"] is False
        assert observed["stream"] is True
    finally:
        client.close()


def test_result_response_is_streamed_and_bounded_without_exposing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        poll_timeout_seconds=1,
        max_parser_json_bytes=4,
        result_hosts=("objects.invalid",),
    )
    signed_url = "https://objects.invalid/result?token=do-not-log"
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(b"12345")
    observed: dict[str, object] = {}

    def oversized_get(*_: object, **kwargs: object) -> requests.Response:
        observed.update(kwargs)
        return response

    monkeypatch.setattr(client._results, "get", oversized_get)  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError, match="size limit") as caught:
            client.fetch_result(signed_url)
        assert observed["stream"] is True
        assert "do-not-log" not in str(caught.value)
    finally:
        client.close()


def test_upload_rejects_oversized_snapshot_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        max_pdf_bytes=4,
    )
    monkeypatch.setattr(
        client._api,
        "post",
        lambda *_args, **_kwargs: pytest.fail("oversized bytes must not be uploaded"),
    )  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError, match="size limit"):
            client.upload_bytes(b"12345", filename="invoice.pdf")
    finally:
        client.close()


def test_api_metadata_response_has_a_one_mib_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
    )
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Length"] = str(1024 * 1024 + 1)
    response.raw = BytesIO()
    monkeypatch.setattr(client._api, "post", lambda *_args, **_kwargs: response)  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError, match="size limit"):
            client.start_parse("https://example.invalid/upload.pdf")
    finally:
        client.close()


def test_result_transport_error_does_not_expose_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        poll_timeout_seconds=1,
        result_hosts=("objects.invalid",),
    )
    signed_url = "https://objects.invalid/result?X-Amz-Signature=do-not-log"

    def failed_get(*_: object, **__: object) -> requests.Response:
        raise requests.ConnectionError(f"could not reach {signed_url}")

    monkeypatch.setattr(client._results, "get", failed_get)  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError) as caught:
            client.fetch_result(signed_url)
        assert "X-Amz-Signature" not in str(caught.value)
        assert "do-not-log" not in str(caught.value)
    finally:
        client.close()


def test_result_preserves_exact_provider_json_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        poll_timeout_seconds=1,
        result_hosts=("objects.invalid",),
    )
    raw = b'{"paymentDetails":{"total":10.20}}\n'
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(raw)

    monkeypatch.setattr(client._results, "get", lambda *_args, **_kwargs: response)  # noqa: SLF001
    try:
        parsed, retained = client.fetch_result("https://objects.invalid/result")
        assert retained == raw
        assert parsed["paymentDetails"]["total"] == Decimal("10.20")
    finally:
        client.close()


def test_result_url_host_must_be_an_exact_allowlist_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PdfCoClient(
        api_key="secret-key",
        base_url="https://api.pdf.co/v1",
        result_hosts=("trusted.example",),
    )
    monkeypatch.setattr(
        client._results,
        "get",
        lambda *_args, **_kwargs: pytest.fail("untrusted result URL must not be requested"),
    )  # noqa: SLF001
    try:
        with pytest.raises(PdfCoError, match="untrusted host"):
            client.fetch_result("https://trusted.example.attacker.invalid/result")
        with pytest.raises(PdfCoError, match="untrusted host"):
            client.fetch_result("https://127.0.0.1/result")
        with pytest.raises(PdfCoError, match="untrusted port"):
            client.fetch_result("https://trusted.example:8443/result")
    finally:
        client.close()
