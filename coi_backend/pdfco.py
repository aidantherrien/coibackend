"""Small, bounded PDF.co AI Invoice Parser client."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    DEFAULT_MAX_PARSER_JSON_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_PDFCO_RESULT_HOSTS,
)

MAX_API_RESPONSE_BYTES = 1024 * 1024


class PdfCoError(RuntimeError):
    """Raised for transport, job, or result errors from PDF.co."""

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


class PdfCoResponseTooLargeError(PdfCoError):
    """A response exceeded a local policy limit without implying corruption."""


@dataclass(frozen=True)
class ParseJob:
    job_id: str
    result_url: str | None


def _read_response_bytes(
    response: requests.Response,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    try:
        if response.is_redirect or response.is_permanent_redirect:
            raise PdfCoError("PDF.co returned an unexpected redirect")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise PdfCoError(
                f"PDF.co HTTP request failed with status {response.status_code}"
            ) from exc

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise PdfCoError("PDF.co returned an invalid Content-Length") from exc
            if declared_length > max_bytes:
                raise PdfCoResponseTooLargeError(
                    f"PDF.co {label} exceeded the configured size limit"
                )

        chunks: list[bytes] = []
        count = 0
        try:
            for block in response.iter_content(chunk_size=64 * 1024):
                if not block:
                    continue
                count += len(block)
                if count > max_bytes:
                    raise PdfCoResponseTooLargeError(
                        f"PDF.co {label} exceeded the configured size limit"
                    )
                chunks.append(block)
        except requests.RequestException as exc:
            raise PdfCoError(f"PDF.co {label} download failed: {type(exc).__name__}") from exc
        return b"".join(chunks)
    finally:
        response.close()


def _response_json_with_raw(
    response: requests.Response,
    *,
    max_bytes: int,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    raw = _read_response_bytes(response, max_bytes=max_bytes, label=label)
    try:
        payload = json.loads(
            raw.decode("utf-8-sig"),
            parse_float=Decimal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PdfCoError("PDF.co returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PdfCoError("PDF.co returned a non-object JSON response")
    return payload, raw


def _response_json(
    response: requests.Response,
    *,
    max_bytes: int = MAX_API_RESPONSE_BYTES,
) -> dict[str, Any]:
    payload, _raw = _response_json_with_raw(
        response,
        max_bytes=max_bytes,
        label="API response",
    )
    return payload


def _message(payload: dict[str, Any]) -> str:
    raw = payload.get("message") or payload.get("error") or "unspecified API error"
    message = str(raw).replace("\n", " ")
    # Provider errors occasionally echo signed result URLs. Keep diagnostics
    # useful without writing query-string credentials into logs or the database.
    message = re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?[redacted]", message)
    return message[:500]


class PdfCoClient:
    """Authenticate only calls to the configured PDF.co API origin."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        poll_timeout_seconds: int = 180,
        poll_interval_seconds: float = 2.0,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        max_parser_json_bytes: int = DEFAULT_MAX_PARSER_JSON_BYTES,
        result_hosts: tuple[str, ...] = DEFAULT_PDFCO_RESULT_HOSTS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        base_parts = urlsplit(self.base_url)
        if (
            base_parts.scheme != "https"
            or not base_parts.hostname
            or base_parts.username
            or base_parts.password
            or base_parts.query
            or base_parts.fragment
        ):
            raise PdfCoError(
                "PDF.co base URL must use HTTPS without credentials, query, or fragment"
            )
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_pdf_bytes = max_pdf_bytes
        self.max_parser_json_bytes = max_parser_json_bytes
        self.result_hosts = frozenset(host.lower().rstrip(".") for host in result_hosts)
        if not self.result_hosts or any(not host for host in self.result_hosts):
            raise PdfCoError("PDF.co result host allowlist must not be empty")
        hostname_pattern = re.compile(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        )
        for host in self.result_hosts:
            try:
                ip_address(host)
            except ValueError:
                pass
            else:
                raise PdfCoError("PDF.co result host allowlist must not contain IP addresses")
            if "." not in host or not hostname_pattern.fullmatch(host):
                raise PdfCoError("PDF.co result host allowlist contains an invalid hostname")

        self._api = requests.Session()
        self._api.headers.update({"x-api-key": api_key})
        self._api.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    connect=3,
                    read=3,
                    backoff_factor=0.75,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=True,
                )
            ),
        )

        # Result URLs may be on temporary object-storage hosts. This session
        # intentionally has no PDF.co API key header.
        self._results = requests.Session()
        self._results.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    connect=3,
                    read=3,
                    backoff_factor=0.75,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=True,
                )
            ),
        )

    def __enter__(self) -> PdfCoClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._api.close()
        self._results.close()

    def upload(self, path: Path) -> str:
        chunks: list[bytes] = []
        count = 0
        with path.open("rb") as stream:
            while block := stream.read(min(1024 * 1024, self.max_pdf_bytes - count + 1)):
                count += len(block)
                if count > self.max_pdf_bytes:
                    raise PdfCoError("source PDF exceeded the configured size limit")
                chunks.append(block)
        return self.upload_bytes(b"".join(chunks), filename=path.name)

    def upload_bytes(self, payload_bytes: bytes, *, filename: str) -> str:
        if len(payload_bytes) > self.max_pdf_bytes:
            raise PdfCoError("source PDF exceeded the configured size limit")
        response = self._api.post(
            f"{self.base_url}/file/upload",
            files={
                "file": (Path(filename).name or "document.pdf", payload_bytes, "application/pdf")
            },
            timeout=(15, 120),
            allow_redirects=False,
            stream=True,
        )
        payload = _response_json(response)
        if payload.get("error"):
            raise PdfCoError(f"PDF.co upload failed: {_message(payload)}")
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PdfCoError("PDF.co upload response did not contain an HTTPS URL")
        return url

    def start_parse(self, file_url: str) -> ParseJob:
        parts = urlsplit(file_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise PdfCoError("PDF.co upload URL is unsafe")
        # Do not automatically retry this paid operation: a timed-out request
        # may still have created a job remotely.
        response = self._api.post(
            f"{self.base_url}/ai-invoice-parser",
            json={"url": file_url},
            timeout=(15, 60),
            allow_redirects=False,
            stream=True,
        )
        payload = _response_json(response)
        if payload.get("error"):
            raise PdfCoError(f"PDF.co parse start failed: {_message(payload)}")
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id.strip():
            raise PdfCoError("PDF.co parse response did not contain a jobId")
        result_url = payload.get("url")
        if result_url is not None and not isinstance(result_url, str):
            raise PdfCoError("PDF.co parse response contained an invalid result URL")
        return ParseJob(job_id=job_id, result_url=result_url)

    def wait_for_result(self, job: ParseJob) -> tuple[dict[str, Any], str, bytes]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        result_url = job.result_url
        transient_errors = 0

        while time.monotonic() < deadline:
            try:
                response = self._api.post(
                    f"{self.base_url}/job/check",
                    json={"jobId": job.job_id},
                    timeout=(15, 60),
                    allow_redirects=False,
                    stream=True,
                )
                payload = _response_json(response)
                transient_errors = 0
            except (requests.RequestException, PdfCoError) as exc:
                transient_errors += 1
                if transient_errors > 4:
                    raise PdfCoError(
                        f"PDF.co job check repeatedly failed for job {job.job_id}"
                    ) from exc
                time.sleep(min(2**transient_errors, 10))
                continue

            if payload.get("error"):
                raise PdfCoError(
                    f"PDF.co job check failed: {_message(payload)}",
                    terminal=True,
                )
            status = str(payload.get("status", "")).strip().lower()
            candidate_url = payload.get("url")
            if isinstance(candidate_url, str) and candidate_url:
                result_url = candidate_url

            if status == "success":
                if not result_url:
                    raise PdfCoError(f"PDF.co job {job.job_id} succeeded without a result URL")
                parsed, raw_json = self.fetch_result(result_url)
                return parsed, result_url, raw_json
            if status in {"failed", "aborted"}:
                raise PdfCoError(
                    f"PDF.co job {job.job_id} ended with status {status}",
                    terminal=True,
                )
            if status not in {"", "working", "pending", "processing"}:
                raise PdfCoError(f"PDF.co job {job.job_id} returned unknown status {status!r}")
            time.sleep(self.poll_interval_seconds)

        raise PdfCoError(f"PDF.co parse job timed out (job {job.job_id})")

    def fetch_result(self, result_url: str) -> tuple[dict[str, Any], bytes]:
        parts = urlsplit(result_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise PdfCoError("PDF.co supplied an unsafe result URL")
        try:
            port = parts.port
        except ValueError as exc:
            raise PdfCoError("PDF.co supplied an unsafe result URL") from exc
        if port not in {None, 443}:
            raise PdfCoError("PDF.co supplied a result URL on an untrusted port")
        if parts.hostname.lower().rstrip(".") not in self.result_hosts:
            raise PdfCoError("PDF.co supplied a result URL on an untrusted host")
        try:
            response = self._results.get(
                result_url,
                timeout=(15, 120),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise PdfCoError(f"PDF.co result download failed: {type(exc).__name__}") from exc
        payload, raw_json = _response_json_with_raw(
            response,
            max_bytes=self.max_parser_json_bytes,
            label="result response",
        )
        if payload.get("error"):
            raise PdfCoError(f"PDF.co result fetch failed: {_message(payload)}")
        return payload, raw_json
