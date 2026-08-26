import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from coi_backend.health import check_database
from coi_backend.mapping import map_invoice, map_oa
from coi_backend.migrations import apply_migrations
from coi_backend.repository import DocumentRepository, connect
from coi_backend.storage import ArtifactLocation


@pytest.mark.integration
def test_azure_postgresql_non_superuser_bootstrap_and_runtime_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    repository_root = Path(__file__).resolve().parents[1]
    bootstrap_sql = (repository_root / "sql" / "grants" / "bootstrap_roles.sql").read_text(
        encoding="utf-8"
    )
    least_privilege_sql = (repository_root / "sql" / "grants" / "least_privilege.sql").read_text(
        encoding="utf-8"
    )

    token = uuid.uuid4().hex[:12]
    admin_role = f"coi_azure_admin_{token}"
    deployment_role = f"coi_deploy_{token}"
    runtime_role = f"coi_app_{token}"
    database_name = f"coi_azure_test_{token}"
    admin_password = f"azure-admin-{token}-A1!"
    deployment_password = f"azure-deploy-{token}-A1!"
    runtime_password = f"azure-runtime-{token}-A1!"
    fixed_roles = ("coi_runtime", "coi_migrator", "coi_owner")

    with connect(database_url, timeout_seconds=5) as setup:
        current_role = setup.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if not current_role or not current_role["rolsuper"]:
            pytest.skip("managed-database role setup requires a superuser test connection")
        existing = setup.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(fixed_roles),)
        ).fetchall()
        if existing:
            pytest.skip(
                "Azure PostgreSQL role regression requires a cluster without COI group roles"
            )

        try:
            setup.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} CREATEDB CREATEROLE "
                    "NOSUPERUSER INHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(admin_role), sql.Literal(admin_password))
            )
            setup.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database_name), sql.Identifier(admin_role)
                )
            )
        except Exception:
            setup.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
            setup.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(admin_role)))
            raise

    admin_url = make_conninfo(
        database_url,
        dbname=database_name,
        user=admin_role,
        password=admin_password,
    )
    deployment_url = make_conninfo(
        database_url,
        dbname=database_name,
        user=deployment_role,
        password=deployment_password,
    )
    runtime_url = make_conninfo(
        database_url,
        dbname=database_name,
        user=runtime_role,
        password=runtime_password,
    )

    try:
        with connect(admin_url, timeout_seconds=5) as admin:
            attributes = admin.execute(
                """
                SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication,
                       rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
            assert attributes == {
                "rolsuper": False,
                "rolcreatedb": True,
                "rolcreaterole": True,
                "rolinherit": True,
                "rolreplication": False,
                "rolbypassrls": False,
            }

            # Reapplication must work for a database-owning CREATEROLE login,
            # even though it is not a PostgreSQL superuser and cannot retain
            # SET access to the NOLOGIN owner role between operations.
            admin.execute(bootstrap_sql)
            # Simulate the membership created by the older, inheriting
            # bootstrap revision. Reapplication must update its stored
            # PostgreSQL 16 membership options, not merely alter the role's
            # default for future grants.
            admin.execute("GRANT coi_owner TO coi_migrator WITH INHERIT TRUE, SET TRUE")
            admin.execute(bootstrap_sql)
            migrator_membership = admin.execute(
                """
                SELECT membership.inherit_option, membership.set_option,
                       member.rolinherit
                FROM pg_auth_members AS membership
                JOIN pg_roles AS granted ON granted.oid = membership.roleid
                JOIN pg_roles AS member ON member.oid = membership.member
                WHERE granted.rolname = 'coi_owner'
                  AND member.rolname = 'coi_migrator'
                """
            ).fetchone()
            assert migrator_membership == {
                "inherit_option": False,
                "set_option": True,
                "rolinherit": False,
            }
            membership = admin.execute(
                """
                SELECT pg_has_role(current_user, 'coi_owner', 'SET') AS may_set_owner,
                       pg_has_role(current_user, 'coi_owner', 'USAGE') AS may_use_owner
                """
            ).fetchone()
            assert membership == {"may_set_owner": False, "may_use_owner": False}

            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOCREATEDB NOCREATEROLE "
                    "NOSUPERUSER INHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(deployment_role), sql.Literal(deployment_password))
            )
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOCREATEDB NOCREATEROLE "
                    "NOSUPERUSER INHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(runtime_role), sql.Literal(runtime_password))
            )
            admin.execute(
                sql.SQL("GRANT coi_migrator TO {}").format(sql.Identifier(deployment_role))
            )
            admin.execute(sql.SQL("GRANT coi_runtime TO {}").format(sql.Identifier(runtime_role)))

        monkeypatch.setenv("DATABASE_MIGRATION_ROLE", "coi_owner")
        with connect(deployment_url, timeout_seconds=5) as deployment:
            deployment_membership = deployment.execute(
                """
                SELECT pg_has_role(current_user, 'coi_migrator', 'SET') AS may_set_migrator,
                       pg_has_role(current_user, 'coi_owner', 'SET') AS may_set_owner,
                       pg_has_role(current_user, 'coi_owner', 'USAGE') AS may_use_owner
                """
            ).fetchone()
            assert deployment_membership == {
                "may_set_migrator": True,
                "may_set_owner": True,
                "may_use_owner": False,
            }
            assert apply_migrations(deployment) == ("0001", "0002", "0003")
            assert apply_migrations(deployment) == ()
            identity = deployment.execute("SELECT current_user, session_user").fetchone()
            assert identity == {
                "current_user": deployment_role,
                "session_user": deployment_role,
            }

        with connect(admin_url, timeout_seconds=5) as admin:
            admin.execute(least_privilege_sql)
            admin.execute(least_privilege_sql)
            membership = admin.execute(
                """
                SELECT pg_has_role(current_user, 'coi_owner', 'SET') AS may_set_owner,
                       pg_has_role(current_user, 'coi_owner', 'USAGE') AS may_use_owner
                """
            ).fetchone()
            assert membership == {"may_set_owner": False, "may_use_owner": False}

        with connect(runtime_url, timeout_seconds=5) as runtime:
            assert check_database(runtime)
            runtime_membership = runtime.execute(
                """
                SELECT pg_has_role(current_user, 'coi_runtime', 'USAGE') AS may_use_runtime,
                       pg_has_role(current_user, 'coi_migrator', 'SET') AS may_set_migrator,
                       pg_has_role(current_user, 'coi_owner', 'SET') AS may_set_owner
                """
            ).fetchone()
            assert runtime_membership == {
                "may_use_runtime": True,
                "may_set_migrator": False,
                "may_set_owner": False,
            }

            inserted = runtime.execute(
                """
                INSERT INTO coi.documents
                    (sha256, original_filename, content_length_bytes,
                     document_type, vendor)
                VALUES (%s, 'azure-runtime.pdf', 1, 'invoice', 'ARTOPEX')
                RETURNING document_id
                """,
                (token.ljust(64, "0"),),
            ).fetchone()
            assert inserted is not None
            runtime.execute(
                "UPDATE coi.documents SET status = 'stored' WHERE document_id = %s",
                (inserted["document_id"],),
            )

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(
                    "UPDATE coi.documents SET sha256 = %s WHERE document_id = %s",
                    ("f" * 64, inserted["document_id"]),
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(
                    "DELETE FROM coi.documents WHERE document_id = %s",
                    (inserted["document_id"],),
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute("CREATE TABLE coi.runtime_must_not_create (id integer)")
    finally:
        with connect(database_url, timeout_seconds=5) as cleanup:
            cleanup.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
            for role_name in (deployment_role, runtime_role, *fixed_roles, admin_role):
                cleanup.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))


@pytest.mark.integration
def test_fresh_database_migrates_and_is_idempotent(tmp_path: Path) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    with connect(database_url, timeout_seconds=5) as connection:
        repository_root = Path(__file__).resolve().parents[1]
        pre_evidence_migrations = tmp_path / "pre-evidence-migrations"
        pre_evidence_migrations.mkdir()
        for filename in ("0001_initial_schema.sql", "0002_reconciliation_views.sql"):
            shutil.copy2(
                repository_root / "sql" / "migrations" / filename,
                pre_evidence_migrations / filename,
            )
        first = apply_migrations(connection, directory=pre_evidence_migrations)
        assert first == ("0001", "0002")
        connection.execute(
            """
            INSERT INTO coi.documents
                (sha256, original_filename, content_length_bytes, document_type,
                 vendor, artifact_backend, blob_name, status)
            VALUES (%s, 'pre-0003.pdf', 50, 'invoice', 'ARTOPEX', 'local',
                    'raw/pre-0003.pdf', 'parsed')
            """,
            ("c" * 64,),
        )
        assert apply_migrations(connection) == ("0003",)
        backfill = connection.execute(
            """
            SELECT artifact_kind, artifact_backend, storage_account_name,
                   blob_name, sha256, content_length_bytes,
                   metadata ->> 'backfilled_by_migration' AS migration
            FROM coi.document_artifacts
            WHERE sha256 = %s
            """,
            ("c" * 64,),
        ).fetchone()
        assert backfill == {
            "artifact_kind": "source_pdf_retained",
            "artifact_backend": "local",
            "storage_account_name": None,
            "blob_name": "raw/pre-0003.pdf",
            "sha256": "c" * 64,
            "content_length_bytes": 50,
            "migration": "0003",
        }
        assert check_database(connection)

        with connection.transaction(force_rollback=True):
            pre_retention = connection.execute(
                """
                INSERT INTO coi.documents
                    (sha256, original_filename, content_length_bytes,
                     document_type, vendor)
                VALUES (%s, 'pre-retention-correction.pdf', 1,
                        'invoice', 'ARTOPEX')
                RETURNING document_id
                """,
                ("d" * 64,),
            ).fetchone()
            assert pre_retention is not None
            corrected = connection.execute(
                """
                UPDATE coi.documents
                SET sha256 = %s, content_length_bytes = 2
                WHERE document_id = %s
                RETURNING sha256, content_length_bytes
                """,
                ("e" * 64, pre_retention["document_id"]),
            ).fetchone()
            assert corrected == {"sha256": "e" * 64, "content_length_bytes": 2}

        repository = DocumentRepository(connection)
        invoice_document = repository.register_document(
            sha256="a" * 64,
            filename="invoice.pdf",
            content_length_bytes=100,
            document_type="invoice",
            vendor="ARTOPEX",
        )
        repository.add_local_source(
            document_id=invoice_document.document_id,
            source_reference="integration:invoice",
            source_filename="invoice.pdf",
            observed_document_type="invoice",
            observed_vendor="ARTOPEX",
        )
        assert repository.try_claim(
            document_id=invoice_document.document_id,
            force_retry=False,
            stale_after_seconds=3600,
        )
        repository.set_raw_location(
            document_id=invoice_document.document_id,
            location=ArtifactLocation("local", None, None, "raw/invoice.pdf"),
        )
        repository.set_raw_location(
            document_id=invoice_document.document_id,
            location=ArtifactLocation("local", None, None, "raw/invoice.pdf"),
        )
        with pytest.raises(RuntimeError, match="raw-artifact evidence"):
            repository.set_raw_location(
                document_id=invoice_document.document_id,
                location=ArtifactLocation("local", None, None, "raw/replacement.pdf"),
            )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            connection.execute(
                """
                UPDATE coi.documents
                SET blob_name = 'raw/unrecorded.pdf'
                WHERE document_id = %s
                """,
                (invoice_document.document_id,),
            )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            connection.execute(
                """
                UPDATE coi.documents
                SET sha256 = %s, content_length_bytes = 101
                WHERE document_id = %s
                """,
                ("f" * 64, invoice_document.document_id),
            )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            connection.execute(
                """
                UPDATE coi.documents
                SET artifact_backend = NULL,
                    storage_account_name = NULL,
                    storage_container = NULL,
                    blob_name = NULL,
                    blob_version_id = NULL
                WHERE document_id = %s
                """,
                (invoice_document.document_id,),
            )
        repository.repair_raw_location(
            document_id=invoice_document.document_id,
            location=ArtifactLocation("local", None, None, "raw/invoice-repair.pdf"),
        )
        raw_artifact_history = connection.execute(
            """
            SELECT artifact_kind, artifact_backend, storage_account_name,
                   storage_container, blob_name, blob_version_id
            FROM coi.document_artifacts
            WHERE document_id = %s
            ORDER BY document_artifact_id
            """,
            (invoice_document.document_id,),
        ).fetchall()
        assert raw_artifact_history == [
            {
                "artifact_kind": "source_pdf_retained",
                "artifact_backend": "local",
                "storage_account_name": None,
                "storage_container": None,
                "blob_name": "raw/invoice.pdf",
                "blob_version_id": None,
            },
            {
                "artifact_kind": "source_pdf_repaired",
                "artifact_backend": "local",
                "storage_account_name": None,
                "storage_container": None,
                "blob_name": "raw/invoice-repair.pdf",
                "blob_version_id": None,
            },
        ]
        invoice_attempt = repository.create_parse_attempt(document_id=invoice_document.document_id)
        repository.set_attempt_job(parse_attempt_id=invoice_attempt, job_id="job-inv")
        repository.set_attempt_job(parse_attempt_id=invoice_attempt, job_id="job-inv")
        with pytest.raises(RuntimeError, match="provider job evidence"):
            repository.set_attempt_job(parse_attempt_id=invoice_attempt, job_id="replacement-job")
        repository.set_attempt_result(
            parse_attempt_id=invoice_attempt,
            location=ArtifactLocation("local", None, None, "json/invoice.json"),
        )
        repository.set_attempt_result(
            parse_attempt_id=invoice_attempt,
            location=ArtifactLocation("local", None, None, "json/invoice.json"),
        )
        with pytest.raises(RuntimeError, match="result evidence"):
            repository.set_attempt_result(
                parse_attempt_id=invoice_attempt,
                location=ArtifactLocation("local", None, None, "json/replacement.json"),
            )
        invoice = map_invoice(
            {
                "invoice": {"invoiceNo": "INV-1", "poNo": "PO-1"},
                "paymentDetails": {"total": "10.00"},
                "lineItems": [
                    {
                        "lineNo": "001",
                        "productCode": "PRODUCT-1",
                        "shipQty": "2",
                        "netPrice": "5.00",
                        "extension": "10.00",
                    }
                ],
            },
            vendor="ARTOPEX",
        )
        assert (
            repository.store_invoice(
                document_id=invoice_document.document_id,
                parse_attempt_id=invoice_attempt,
                record=invoice,
            ).outcome
            == "stored"
        )
        conflicting_source_id = repository.add_local_source(
            document_id=invoice_document.document_id,
            source_reference="integration:invoice:misclassified-as-oa",
            source_filename="invoice.pdf",
            observed_document_type="oa",
            observed_vendor="ARTOPEX",
        )
        repository.mark_source_needs_review(
            document_source_id=conflicting_source_id,
            error_code="source_classification_conflict",
            error_message="same bytes were observed as oa instead of invoice",
        )
        source_review = connection.execute(
            """
            SELECT review_item_kind, processing_status, review_status, error_code
            FROM coi.document_review_queue
            WHERE document_source_id = %s
            """,
            (conflicting_source_id,),
        ).fetchone()
        canonical_status = connection.execute(
            "SELECT status FROM coi.documents WHERE document_id = %s",
            (invoice_document.document_id,),
        ).fetchone()
        assert source_review == {
            "review_item_kind": "source",
            "processing_status": "parsed",
            "review_status": "needs_review",
            "error_code": "source_classification_conflict",
        }
        assert canonical_status == {"status": "parsed"}

        oa_document = repository.register_document(
            sha256="b" * 64,
            filename="oa.pdf",
            content_length_bytes=100,
            document_type="oa",
            vendor="ARTOPEX",
        )
        repository.add_local_source(
            document_id=oa_document.document_id,
            source_reference="integration:oa",
            source_filename="oa.pdf",
            observed_document_type="oa",
            observed_vendor="ARTOPEX",
        )
        assert repository.try_claim(
            document_id=oa_document.document_id,
            force_retry=False,
            stale_after_seconds=3600,
        )
        repository.set_raw_location(
            document_id=oa_document.document_id,
            location=ArtifactLocation(
                "azure_blob",
                "stcoiportaldev",
                "raw-pdfs",
                "raw/oa.pdf",
                "raw-version-1",
            ),
        )
        oa_attempt = repository.create_parse_attempt(document_id=oa_document.document_id)
        repository.set_attempt_job(parse_attempt_id=oa_attempt, job_id="job-oa")
        repository.set_attempt_result(
            parse_attempt_id=oa_attempt,
            location=ArtifactLocation(
                "azure_blob",
                "stcoiportaldev",
                "pdfco-json",
                "json/oa.json",
                "parser-version-1",
            ),
        )
        azure_coordinates = connection.execute(
            """
            SELECT doc.artifact_backend, doc.storage_account_name,
                   doc.storage_container, doc.blob_name, doc.blob_version_id,
                   attempt.result_artifact_backend,
                   attempt.result_storage_account_name,
                   attempt.result_container, attempt.result_blob_name,
                   attempt.result_blob_version_id
            FROM coi.documents AS doc
            JOIN coi.parse_attempts AS attempt
              ON attempt.document_id = doc.document_id
            WHERE doc.document_id = %s
              AND attempt.parse_attempt_id = %s
            """,
            (oa_document.document_id, oa_attempt),
        ).fetchone()
        assert azure_coordinates == {
            "artifact_backend": "azure_blob",
            "storage_account_name": "stcoiportaldev",
            "storage_container": "raw-pdfs",
            "blob_name": "raw/oa.pdf",
            "blob_version_id": "raw-version-1",
            "result_artifact_backend": "azure_blob",
            "result_storage_account_name": "stcoiportaldev",
            "result_container": "pdfco-json",
            "result_blob_name": "json/oa.json",
            "result_blob_version_id": "parser-version-1",
        }
        reusable_oa = repository.find_reusable_parse(document_id=oa_document.document_id)
        assert reusable_oa is not None
        assert reusable_oa.result_location == ArtifactLocation(
            "azure_blob",
            "stcoiportaldev",
            "pdfco-json",
            "json/oa.json",
            "parser-version-1",
        )
        oa = map_oa(
            {
                "invoice": {"invoiceNo": "OA-1", "poNo": "PO-1"},
                "paymentDetails": {"total": "10.00"},
                "lineItems": [
                    {
                        "line": "001",
                        "productCode": "PRODUCT-1",
                        "qty": "2",
                        "netPrice": "5.00",
                        "extension": "10.00",
                    }
                ],
            },
            vendor="ARTOPEX",
        )
        assert (
            repository.store_oa(
                document_id=oa_document.document_id,
                parse_attempt_id=oa_attempt,
                record=oa,
            ).outcome
            == "stored"
        )

        overview_count = connection.execute(
            "SELECT count(*) AS count FROM coi.po_document_overview WHERE po = 'PO-1'"
        ).fetchone()
        reconciliation = connection.execute(
            """
            SELECT reconciliation_status
            FROM coi.po_product_reconciliation
            WHERE vendor = 'ARTOPEX' AND po = 'PO-1' AND product_code = 'PRODUCT-1'
            """
        ).fetchone()
        assert overview_count and overview_count["count"] == 2
        assert reconciliation and reconciliation["reconciliation_status"] == "matched"

        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            connection.execute(
                """
                UPDATE coi.parse_attempts
                SET error_message = 'late mutation'
                WHERE parse_attempt_id = %s
                """,
                (invoice_attempt,),
            )

        # A missing monetary value must never be reported as a successful match.
        with connection.transaction(force_rollback=True):
            connection.execute(
                """
                UPDATE coi.invoice_line_items
                SET net_price = NULL
                WHERE invoice_id = (
                    SELECT invoice_id
                    FROM coi.invoice_summary
                    WHERE document_id = %s
                )
                """,
                (invoice_document.document_id,),
            )
            incomplete = connection.execute(
                """
                SELECT reconciliation_status
                FROM coi.po_product_reconciliation
                WHERE vendor = 'ARTOPEX' AND po = 'PO-1' AND product_code = 'PRODUCT-1'
                """
            ).fetchone()
            assert incomplete and incomplete["reconciliation_status"] == "incomplete_data"

        # Once an exact retained artifact is proven corrupt, recovery may still
        # resume its known provider job but must not select the artifact again.
        replay_attempt = repository.create_replay_attempt(
            document_id=invoice_document.document_id,
            source_attempt_id=invoice_attempt,
            location=ArtifactLocation("local", None, None, "json/invoice.json"),
        )
        chained_replay_attempt = repository.create_replay_attempt(
            document_id=invoice_document.document_id,
            source_attempt_id=replay_attempt,
            location=ArtifactLocation("local", None, None, "json/invoice.json"),
        )
        repository.mark_failed(
            document_id=invoice_document.document_id,
            parse_attempt_id=chained_replay_attempt,
            error_code="retained_artifact_unusable",
            error_message="synthetic integrity failure",
        )
        recovery = repository.find_reusable_parse(document_id=invoice_document.document_id)
        assert recovery is not None
        assert recovery.parse_attempt_id == invoice_attempt
        assert recovery.provider_job_id == "job-inv"
        assert recovery.result_location is None

        recovery_indexes = {
            row["indexname"]
            for row in connection.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'coi'
                  AND indexname IN (
                      'parse_attempts_unusable_artifact_idx',
                      'parse_attempts_terminal_resume_source_idx'
                  )
                """
            ).fetchall()
        }
        assert recovery_indexes == {
            "parse_attempts_unusable_artifact_idx",
            "parse_attempts_terminal_resume_source_idx",
        }

        assert apply_migrations(connection) == ()

        connection.execute(
            (repository_root / "sql" / "grants" / "bootstrap_roles.sql").read_text(encoding="utf-8")
        )
        # Simulate a database that ran the older, table-wide UPDATE grant before
        # reapplying the hardened idempotent grant script.
        connection.execute("GRANT UPDATE ON ALL TABLES IN SCHEMA coi TO coi_runtime")
        connection.execute(
            (repository_root / "sql" / "grants" / "least_privilege.sql").read_text(encoding="utf-8")
        )
        privileges = connection.execute(
            """
            SELECT
                has_column_privilege(
                    'coi_runtime', 'coi.documents', 'status', 'UPDATE'
                ) AS may_update_document_status,
                has_column_privilege(
                    'coi_runtime', 'coi.documents', 'artifact_backend', 'UPDATE'
                ) AS may_set_document_artifact_backend,
                has_column_privilege(
                    'coi_runtime', 'coi.documents', 'storage_account_name', 'UPDATE'
                ) AS may_set_document_storage_account,
                has_column_privilege(
                    'coi_runtime', 'coi.documents', 'sha256', 'UPDATE'
                ) AS may_update_document_hash,
                has_column_privilege(
                    'coi_runtime', 'coi.parse_attempts', 'provider_job_id', 'UPDATE'
                ) AS may_set_provider_job,
                has_column_privilege(
                    'coi_runtime', 'coi.parse_attempts',
                    'result_storage_account_name', 'UPDATE'
                ) AS may_set_result_storage_account,
                has_column_privilege(
                    'coi_runtime', 'coi.parse_attempts', 'provider', 'UPDATE'
                ) AS may_update_provider,
                has_column_privilege(
                    'coi_runtime', 'coi.invoice_summary', 'vendor', 'UPDATE'
                ) AS may_update_invoice,
                has_column_privilege(
                    'coi_runtime', 'coi.document_artifacts', 'blob_name', 'UPDATE'
                ) AS may_update_artifact_history,
                has_column_privilege(
                    'coi_runtime', 'coi.document_sources', 'review_status', 'UPDATE'
                ) AS may_flag_source_review,
                has_column_privilege(
                    'coi_runtime', 'coi.document_sources', 'source_filename', 'UPDATE'
                ) AS may_update_source_identity,
                has_table_privilege(
                    'coi_runtime', 'coi.schema_migrations', 'SELECT'
                ) AS may_read_migrations,
                has_table_privilege(
                    'coi_runtime', 'coi.schema_migrations', 'UPDATE'
                ) AS may_update_migrations,
                has_schema_privilege('coi_runtime', 'coi', 'CREATE') AS may_create_schema,
                has_function_privilege(
                    'coi_runtime', 'coi.guard_parse_attempt_evidence()', 'EXECUTE'
                ) AS may_call_guard
            """
        ).fetchone()
        assert privileges == {
            "may_update_document_status": True,
            "may_set_document_artifact_backend": True,
            "may_set_document_storage_account": True,
            "may_update_document_hash": False,
            "may_set_provider_job": True,
            "may_set_result_storage_account": True,
            "may_update_provider": False,
            "may_update_invoice": False,
            "may_update_artifact_history": False,
            "may_flag_source_review": True,
            "may_update_source_identity": False,
            "may_read_migrations": True,
            "may_update_migrations": False,
            "may_create_schema": False,
            "may_call_guard": False,
        }
