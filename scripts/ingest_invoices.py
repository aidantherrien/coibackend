"""Compatibility wrapper for invoice-folder ingestion."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coi_backend.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["ingest", "--type", "invoice", *sys.argv[1:]]))
