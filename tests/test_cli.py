import hashlib
from pathlib import Path

import pytest

from coi_backend.cli import _move_processed_source, _move_rejected_source
from coi_backend.ingestion import SourceSnapshotError


def test_processed_source_move_is_atomic_unique_and_content_verified(tmp_path: Path) -> None:
    source = tmp_path / "input" / "invoice.pdf"
    source.parent.mkdir()
    payload = b"%PDF archive me"
    source.write_bytes(payload)
    archive = tmp_path / "archive"

    destination = _move_processed_source(
        source,
        archive,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        max_pdf_bytes=1024,
    )

    assert not source.exists()
    assert destination.parent == archive
    assert destination.name.startswith("invoice-")
    assert destination.read_bytes() == payload


def test_processed_source_move_leaves_changed_file_in_place(tmp_path: Path) -> None:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"changed")

    with pytest.raises(SourceSnapshotError, match="changed after ingestion"):
        _move_processed_source(
            source,
            tmp_path / "archive",
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
            max_pdf_bytes=1024,
        )

    assert source.read_bytes() == b"changed"


def test_rejected_source_can_be_quarantined_without_reading_content(tmp_path: Path) -> None:
    source = tmp_path / "input" / "oversized.pdf"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)

    destination = _move_rejected_source(source, tmp_path / "quarantine")

    assert not source.exists()
    assert destination.stat().st_size == 100
