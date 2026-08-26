from pathlib import Path

import pytest

from coi_backend.migrations import MigrationError, discover_migrations


def test_discover_migrations_requires_ordered_names(tmp_path: Path) -> None:
    (tmp_path / "0001_initial.sql").write_text("SELECT 1;\n", encoding="utf-8")
    migrations = discover_migrations(tmp_path)
    assert migrations[0].version == "0001"
    assert len(migrations[0].checksum) == 64


def test_discover_migrations_rejects_unversioned_sql(tmp_path: Path) -> None:
    (tmp_path / "initial.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="filename"):
        discover_migrations(tmp_path)


def test_discover_migrations_rejects_variable_width_versions(tmp_path: Path) -> None:
    (tmp_path / "999_before.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "1000_after.sql").write_text("SELECT 2;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="NNNN"):
        discover_migrations(tmp_path)
