"""Command-line entry point for migration, validation, and ingestion."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import psycopg

from .config import (
    ConfigurationError,
    Settings,
    database_connect_timeout_from_environment,
    database_url_from_environment,
)
from .health import check_database
from .ingestion import IngestionOutcome, IngestionService, SourceSnapshotError, snapshot_source
from .migrations import MigrationError, apply_migrations
from .pdfco import PdfCoClient
from .repository import DocumentRepository, connect
from .storage import artifact_store_from_settings


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        return True


def _configure_logging(level: str, run_id: str) -> logging.LoggerAdapter:
    handler = logging.StreamHandler()
    handler.addFilter(_RunIdFilter(run_id))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s run_id=%(run_id)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return logging.LoggerAdapter(logging.getLogger("coi.ingestion"), {"run_id": run_id})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coi-backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="ingest a folder of PDFs")
    ingest.add_argument("--type", choices=("invoice", "oa"), required=True)
    ingest.add_argument("--input-dir", type=Path)
    ingest.add_argument("--vendor")
    ingest.add_argument(
        "--force-retry",
        action="store_true",
        help="retry documents currently marked failed or needs_review",
    )
    ingest.add_argument(
        "--allow-new-paid-parse",
        action="store_true",
        help=(
            "with --force-retry, allow a new PDF.co charge when no retained result "
            "or known job can be recovered"
        ),
    )

    subparsers.add_parser("migrate", help="apply pending SQL migrations")
    subparsers.add_parser("check", help="validate database connectivity and schema")
    return parser


def _migrate() -> int:
    url = database_url_from_environment()
    timeout = database_connect_timeout_from_environment()
    with connect(url, timeout_seconds=timeout) as connection:
        versions = apply_migrations(connection)
    if versions:
        print("Applied migrations:", ", ".join(versions))
    else:
        print("Database is already current.")
    return 0


def _check() -> int:
    url = database_url_from_environment()
    timeout = database_connect_timeout_from_environment()
    with connect(url, timeout_seconds=timeout) as connection:
        tables = check_database(connection)
        database = connection.execute("SELECT current_database() AS name").fetchone()
    print(f"Database OK: {database['name'] if database else 'unknown'}")
    print("COI tables:", ", ".join(tables))
    return 0


def _move_processed_source(
    path: Path,
    destination_dir: Path,
    *,
    expected_sha256: str,
    max_pdf_bytes: int,
) -> Path:
    """Atomically move an unchanged source to a unique name on the same filesystem."""

    verified = snapshot_source(path, max_bytes=max_pdf_bytes, min_age_seconds=0)
    if verified.sha256 != expected_sha256:
        raise SourceSnapshotError("source PDF changed after ingestion; leaving it in place")

    return _atomic_unique_move(verified.canonical_path, destination_dir)


def _move_rejected_source(path: Path, destination_dir: Path) -> Path:
    """Quarantine a rejected directory entry without materializing its content."""

    try:
        path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(
            f"unable to inspect rejected source: {type(exc).__name__}"
        ) from exc
    return _atomic_unique_move(path.absolute(), destination_dir)


def _atomic_unique_move(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    if destination_dir.is_symlink():
        raise SourceSnapshotError("archive/quarantine directory must not be a symbolic link")
    if source.lstat().st_dev != destination_dir.stat().st_dev:
        raise SourceSnapshotError("archive/quarantine move must stay on one filesystem")

    for _attempt in range(10):
        destination = destination_dir / (f"{source.stem}-{uuid.uuid4().hex}{source.suffix}")
        try:
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            continue
        os.close(descriptor)
        try:
            os.replace(source, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination
    raise SourceSnapshotError("unable to allocate a unique archive/quarantine filename")


def _ingest(args: argparse.Namespace) -> int:
    if args.allow_new_paid_parse and not args.force_retry:
        raise ConfigurationError("--allow-new-paid-parse requires --force-retry")
    settings = Settings.from_environment(
        document_type=args.type,
        input_dir=args.input_dir,
        vendor=args.vendor,
    )
    run_id = uuid.uuid4().hex
    logger = _configure_logging(settings.log_level, run_id)

    if not settings.input_dir.is_dir():
        raise ConfigurationError(f"input directory does not exist: {settings.input_dir}")
    pdfs = sorted(
        path
        for path in settings.input_dir.iterdir()
        if (path.is_symlink() or not path.is_dir()) and path.suffix.lower() == ".pdf"
    )
    if not pdfs:
        print(f"No PDFs found in {settings.input_dir}")
        return 0

    lifecycle_failures = 0
    with connect(
        settings.database_url,
        timeout_seconds=settings.database_connect_timeout_seconds,
    ) as connection:
        check_database(connection)
        repository = DocumentRepository(connection)
        if not repository.acquire_run_lock(
            document_type=settings.document_type, vendor=settings.vendor
        ):
            logger.error("another ingestion run holds the vendor/document-type lock")
            return 75
        try:
            store = artifact_store_from_settings(settings)
            with PdfCoClient(
                api_key=settings.pdfco_api_key,
                base_url=settings.pdfco_base_url,
                poll_timeout_seconds=settings.poll_timeout_seconds,
                max_pdf_bytes=settings.max_pdf_bytes,
                max_parser_json_bytes=settings.max_parser_json_bytes,
                result_hosts=settings.pdfco_result_hosts,
            ) as pdfco:
                service = IngestionService(
                    settings=settings,
                    repository=repository,
                    artifact_store=store,
                    pdfco=pdfco,
                    logger=logger,
                )
                outcomes: list[IngestionOutcome] = []
                for path in pdfs:
                    try:
                        outcome = service.ingest(
                            path,
                            force_retry=args.force_retry,
                            allow_new_paid_parse=args.allow_new_paid_parse,
                        )
                    except OSError as exc:
                        message = f"unable to read source file: {type(exc).__name__}"
                        logger.exception("source-file failure file=%s", path.name)
                        outcome = IngestionOutcome(
                            filename=path.name,
                            outcome="failed",
                            message=message,
                        )
                    outcomes.append(outcome)
                    destination_dir = None
                    if outcome.disposition == "archive":
                        destination_dir = settings.archive_dir
                    elif outcome.disposition == "quarantine":
                        destination_dir = settings.quarantine_dir
                    if destination_dir is not None and outcome.content_sha256:
                        try:
                            moved_to = _move_processed_source(
                                path,
                                destination_dir,
                                expected_sha256=outcome.content_sha256,
                                max_pdf_bytes=settings.max_pdf_bytes,
                            )
                            logger.info(
                                "source moved file=%s destination=%s",
                                path.name,
                                moved_to.name,
                            )
                        except (OSError, SourceSnapshotError) as exc:
                            lifecycle_failures += 1
                            logger.error(
                                "source lifecycle move failed file=%s error=%s",
                                path.name,
                                str(exc),
                            )
                    elif destination_dir is not None and outcome.outcome == "failed":
                        try:
                            moved_to = _move_rejected_source(path, destination_dir)
                            logger.info(
                                "rejected source quarantined file=%s destination=%s",
                                path.name,
                                moved_to.name,
                            )
                        except (OSError, SourceSnapshotError) as exc:
                            lifecycle_failures += 1
                            logger.error(
                                "rejected-source quarantine failed file=%s error=%s",
                                path.name,
                                str(exc),
                            )
        finally:
            repository.release_run_lock(
                document_type=settings.document_type, vendor=settings.vendor
            )

    counts = Counter(outcome.outcome for outcome in outcomes)
    for outcome in outcomes:
        details = f" ({outcome.message})" if outcome.message else ""
        print(
            f"{outcome.outcome.upper():12} {outcome.filename} "
            f"document_id={outcome.document_id} lines={outcome.line_count}{details}"
        )
    print("Summary:", " ".join(f"{name}={counts[name]}" for name in sorted(counts)))
    if lifecycle_failures:
        print(f"Source lifecycle move failures={lifecycle_failures}")
    has_problems = any(counts[name] for name in ("failed", "needs_review", "skipped"))
    return 1 if has_problems or lifecycle_failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "migrate":
            return _migrate()
        if args.command == "check":
            return _check()
        return _ingest(args)
    except (ConfigurationError, MigrationError, RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
