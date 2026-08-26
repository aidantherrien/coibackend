-- Idempotent post-migration ownership and least-privilege grants for COI.
--
-- First run sql/grants/bootstrap_roles.sql and grant coi_migrator to the actual
-- deployment login. Run migrations with DATABASE_MIGRATION_ROLE=coi_owner, then
-- run this file as the database administrator. Finally grant coi_runtime to the
-- application login (or Microsoft Entra-authenticated database role). Never set the migration
-- role in the runtime service. Do not put passwords in this file.

BEGIN;

DO $required_roles$
BEGIN
    IF (SELECT count(*)
        FROM pg_roles
        WHERE rolname IN ('coi_owner', 'coi_migrator', 'coi_runtime')) <> 3 THEN
        RAISE EXCEPTION
            'COI roles are missing; run sql/grants/bootstrap_roles.sql first';
    END IF;
END;
$required_roles$;

-- The deployment principal can SET ROLE to the non-login owner. The runtime
-- role is intentionally not a member of either privileged role.
GRANT coi_owner TO coi_migrator WITH INHERIT FALSE, SET TRUE;

-- Azure Database for PostgreSQL administrators are CREATEROLE database owners,
-- not true PostgreSQL
-- superusers. Give the invoking administrator owner membership only for this
-- transaction when object ownership must be normalized. Preserve an existing
-- membership rather than revoking it unexpectedly on reapplication.
CREATE TEMPORARY TABLE pg_temp.coi_grant_actor (
    role_name        NAME NOT NULL,
    membership_added BOOLEAN NOT NULL
) ON COMMIT DROP;

INSERT INTO pg_temp.coi_grant_actor (role_name, membership_added)
SELECT current_user, NOT pg_has_role(current_user, 'coi_owner', 'SET');

DO $temporary_owner_membership$
DECLARE
    actor NAME;
    should_grant BOOLEAN;
BEGIN
    SELECT role_name, membership_added
    INTO actor, should_grant
    FROM pg_temp.coi_grant_actor;

    IF should_grant THEN
        EXECUTE format('GRANT coi_owner TO %I', actor);
    END IF;
END;
$temporary_owner_membership$;

ALTER SCHEMA coi OWNER TO coi_owner;

ALTER TABLE coi.schema_migrations OWNER TO coi_owner;
ALTER TABLE coi.documents OWNER TO coi_owner;
ALTER TABLE coi.document_artifacts OWNER TO coi_owner;
ALTER TABLE coi.document_sources OWNER TO coi_owner;
ALTER TABLE coi.parse_attempts OWNER TO coi_owner;
ALTER TABLE coi.invoice_summary OWNER TO coi_owner;
ALTER TABLE coi.invoice_line_items OWNER TO coi_owner;
ALTER TABLE coi.oa_summary OWNER TO coi_owner;
ALTER TABLE coi.oa_line_items OWNER TO coi_owner;
ALTER VIEW coi.po_document_overview OWNER TO coi_owner;
ALTER VIEW coi.po_product_reconciliation OWNER TO coi_owner;
ALTER VIEW coi.document_review_queue OWNER TO coi_owner;
ALTER FUNCTION coi.touch_updated_at() OWNER TO coi_owner;
ALTER FUNCTION coi.guard_document_artifact_evidence() OWNER TO coi_owner;
ALTER FUNCTION coi.guard_parse_attempt_evidence() OWNER TO coi_owner;

ALTER SEQUENCE coi.documents_document_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.document_artifacts_document_artifact_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.document_sources_document_source_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.parse_attempts_parse_attempt_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.invoice_summary_invoice_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.invoice_line_items_invoice_line_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.oa_summary_oa_id_seq OWNER TO coi_owner;
ALTER SEQUENCE coi.oa_line_items_oa_line_id_seq OWNER TO coi_owner;

REVOKE ALL ON SCHEMA coi FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA coi FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA coi FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coi FROM PUBLIC;

-- PostgreSQL grants function EXECUTE to PUBLIC by default. Keep future owner-
-- created objects private until a migration explicitly grants what is needed.
ALTER DEFAULT PRIVILEGES FOR ROLE coi_owner IN SCHEMA coi
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE coi_owner IN SCHEMA coi
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE coi_owner IN SCHEMA coi
    REVOKE ALL ON FUNCTIONS FROM PUBLIC;

GRANT USAGE ON SCHEMA coi TO coi_runtime;

GRANT SELECT, INSERT ON TABLE
    coi.documents,
    coi.document_artifacts,
    coi.document_sources,
    coi.parse_attempts,
    coi.invoice_summary,
    coi.invoice_line_items,
    coi.oa_summary,
    coi.oa_line_items
TO coi_runtime;

-- Remove any table-wide UPDATE left by an older grant-script revision before
-- applying the narrower column grants below.
REVOKE UPDATE ON TABLE
    coi.documents,
    coi.document_artifacts,
    coi.document_sources,
    coi.parse_attempts,
    coi.invoice_summary,
    coi.invoice_line_items,
    coi.oa_summary,
    coi.oa_line_items
FROM coi_runtime;

-- Runtime state transitions are column-scoped. External identities and
-- artifact coordinates are additionally protected by write-once triggers.
GRANT UPDATE (
    artifact_backend,
    storage_account_name,
    storage_container,
    blob_name,
    blob_version_id,
    status,
    review_status,
    error_code,
    error_message,
    updated_at
) ON TABLE coi.documents TO coi_runtime;

GRANT UPDATE (
    provider_job_id,
    status,
    result_artifact_backend,
    result_storage_account_name,
    result_container,
    result_blob_name,
    result_blob_version_id,
    error_code,
    error_message,
    completed_at
) ON TABLE coi.parse_attempts TO coi_runtime;

GRANT UPDATE (
    review_status,
    error_code,
    error_message
) ON TABLE coi.document_sources TO coi_runtime;

GRANT SELECT ON TABLE
    coi.schema_migrations,
    coi.document_review_queue,
    coi.po_document_overview,
    coi.po_product_reconciliation
TO coi_runtime;

GRANT USAGE, SELECT ON SEQUENCE
    coi.documents_document_id_seq,
    coi.document_artifacts_document_artifact_id_seq,
    coi.document_sources_document_source_id_seq,
    coi.parse_attempts_parse_attempt_id_seq,
    coi.invoice_summary_invoice_id_seq,
    coi.invoice_line_items_invoice_line_id_seq,
    coi.oa_summary_oa_id_seq,
    coi.oa_line_items_oa_line_id_seq
TO coi_runtime;

GRANT EXECUTE ON FUNCTION coi.touch_updated_at() TO coi_runtime;

-- Trigger functions do not require caller EXECUTE; revoke it explicitly so
-- they cannot be invoked as ordinary functions by the runtime role.
REVOKE ALL ON FUNCTION coi.guard_document_artifact_evidence() FROM PUBLIC, coi_runtime;
REVOKE ALL ON FUNCTION coi.guard_parse_attempt_evidence() FROM PUBLIC, coi_runtime;

-- Defense in depth: runtime can ingest and correct records, but cannot erase,
-- truncate, change ownership, alter schema, or modify the migration ledger.
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA coi
    FROM coi_runtime;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLE coi.schema_migrations FROM coi_runtime;

DO $remove_temporary_owner_membership$
DECLARE
    actor NAME;
    should_revoke BOOLEAN;
BEGIN
    SELECT role_name, membership_added
    INTO actor, should_revoke
    FROM pg_temp.coi_grant_actor;

    IF should_revoke THEN
        EXECUTE format('REVOKE coi_owner FROM %I', actor);
    END IF;
END;
$remove_temporary_owner_membership$;

COMMIT;
