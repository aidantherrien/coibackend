-- Enforce write-once external evidence at the database boundary and add the
-- recovery indexes used when parse history grows.

ALTER TABLE coi.parse_attempts
    ADD CONSTRAINT parse_attempts_completion_matches_status
    CHECK (
        (status IN ('started', 'processing') AND completed_at IS NULL)
        OR (status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)
    );

ALTER TABLE coi.document_sources
    ADD COLUMN review_status TEXT NOT NULL DEFAULT 'not_required',
    ADD COLUMN error_code TEXT,
    ADD COLUMN error_message TEXT,
    ADD CONSTRAINT document_sources_review_status_valid
        CHECK (review_status IN
               ('pending', 'not_required', 'needs_review', 'approved', 'rejected')),
    ADD CONSTRAINT document_sources_error_code_normalized
        CHECK (error_code IS NULL
               OR (error_code = btrim(error_code) AND error_code <> ''));

CREATE INDEX document_sources_review_idx
    ON coi.document_sources (review_status, created_at)
    WHERE review_status = 'needs_review';

-- A document keeps one convenient current raw location, while every initial
-- retention and integrity repair is append-only evidence here. This permits a
-- missing Azure blob version/local file to be repaired without erasing its history.
CREATE TABLE coi.document_artifacts (
    document_artifact_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id           BIGINT      NOT NULL
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    artifact_kind         TEXT        NOT NULL,
    artifact_backend      TEXT        NOT NULL,
    storage_account_name  TEXT,
    storage_container     TEXT,
    blob_name             TEXT        NOT NULL,
    blob_version_id       TEXT,
    sha256                 TEXT        NOT NULL,
    content_length_bytes  BIGINT,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT document_artifacts_kind_valid
        CHECK (artifact_kind IN ('source_pdf_retained', 'source_pdf_repaired')),
    CONSTRAINT document_artifacts_backend_valid
        CHECK (artifact_backend IN ('local', 'azure_blob')),
    CONSTRAINT document_artifacts_storage_account_name_normalized
        CHECK (storage_account_name IS NULL
               OR storage_account_name ~ '^[a-z0-9]{3,24}$'),
    CONSTRAINT document_artifacts_container_normalized
        CHECK (storage_container IS NULL
               OR (storage_container = btrim(storage_container)
                   AND storage_container <> '')),
    CONSTRAINT document_artifacts_blob_name_normalized
        CHECK (blob_name = btrim(blob_name) AND blob_name <> ''),
    CONSTRAINT document_artifacts_blob_version_normalized
        CHECK (blob_version_id IS NULL
               OR (blob_version_id = btrim(blob_version_id)
                   AND blob_version_id <> '')),
    CONSTRAINT document_artifacts_coordinates_valid
        CHECK (
            (artifact_backend = 'local'
             AND storage_account_name IS NULL
             AND storage_container IS NULL
             AND blob_version_id IS NULL)
            OR
            (artifact_backend = 'azure_blob'
             AND storage_account_name IS NOT NULL
             AND storage_container IS NOT NULL)
        ),
    CONSTRAINT document_artifacts_sha256_format
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT document_artifacts_content_length_valid
        CHECK (content_length_bytes IS NULL OR content_length_bytes >= 0),
    CONSTRAINT document_artifacts_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX document_artifacts_document_created_idx
    ON coi.document_artifacts (document_id, created_at DESC, document_artifact_id DESC);
CREATE INDEX document_artifacts_blob_idx
    ON coi.document_artifacts
        (artifact_backend, storage_account_name, storage_container,
         blob_name, blob_version_id);

COMMENT ON TABLE coi.document_artifacts IS
    'Append-only raw source retention and integrity-repair evidence for each document.';

-- Preserve the current pointer of databases created before this migration.
-- A nullable legacy length is retained honestly rather than fabricated.
INSERT INTO coi.document_artifacts
    (document_id, artifact_kind, artifact_backend, storage_account_name,
     storage_container, blob_name, blob_version_id, sha256,
     content_length_bytes, metadata, created_at)
SELECT
    document_id,
    'source_pdf_retained',
    artifact_backend,
    storage_account_name,
    storage_container,
    blob_name,
    blob_version_id,
    sha256,
    content_length_bytes,
    jsonb_build_object('backfilled_by_migration', '0003'),
    updated_at
FROM coi.documents
WHERE blob_name IS NOT NULL;

CREATE FUNCTION coi.guard_document_artifact_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    -- Hash and length may be corrected before evidence is retained.
    -- After retention, the current pointer, hash, and size form one auditable
    -- identity and must move together to an already-recorded evidence row.
    IF OLD.blob_name IS NULL AND NEW.blob_name IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.blob_name IS NULL THEN
        RAISE EXCEPTION 'document raw-artifact pointer cannot be cleared'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF ROW(NEW.artifact_backend, NEW.storage_account_name,
           NEW.storage_container, NEW.blob_name, NEW.blob_version_id,
           NEW.sha256, NEW.content_length_bytes)
           IS DISTINCT FROM
           ROW(OLD.artifact_backend, OLD.storage_account_name,
               OLD.storage_container, OLD.blob_name, OLD.blob_version_id,
               OLD.sha256, OLD.content_length_bytes) THEN
        IF NOT EXISTS (
            SELECT 1
            FROM coi.document_artifacts AS artifact
            WHERE artifact.document_id = NEW.document_id
              AND artifact.artifact_backend = NEW.artifact_backend
              AND artifact.storage_account_name
                  IS NOT DISTINCT FROM NEW.storage_account_name
              AND artifact.storage_container
                  IS NOT DISTINCT FROM NEW.storage_container
              AND artifact.blob_name = NEW.blob_name
              AND artifact.blob_version_id
                  IS NOT DISTINCT FROM NEW.blob_version_id
              AND artifact.sha256 = NEW.sha256
              AND artifact.content_length_bytes
                  IS NOT DISTINCT FROM NEW.content_length_bytes
        ) THEN
            RAISE EXCEPTION 'document raw-artifact pointer has no retained audit record'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER documents_artifact_evidence_write_once
BEFORE UPDATE OF artifact_backend, storage_account_name, storage_container,
                 blob_name, blob_version_id, sha256, content_length_bytes
ON coi.documents
FOR EACH ROW
EXECUTE FUNCTION coi.guard_document_artifact_evidence();

CREATE FUNCTION coi.guard_parse_attempt_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'completed parse attempts are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.provider_job_id IS NOT NULL
       AND NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
        RAISE EXCEPTION 'parse-attempt provider job evidence is write-once'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.result_blob_name IS NOT NULL
       AND ROW(NEW.result_artifact_backend, NEW.result_storage_account_name,
               NEW.result_container, NEW.result_blob_name,
               NEW.result_blob_version_id)
           IS DISTINCT FROM
           ROW(OLD.result_artifact_backend, OLD.result_storage_account_name,
               OLD.result_container, OLD.result_blob_name,
               OLD.result_blob_version_id) THEN
        RAISE EXCEPTION 'parse-attempt result evidence is write-once'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER parse_attempt_evidence_write_once
BEFORE UPDATE ON coi.parse_attempts
FOR EACH ROW
EXECUTE FUNCTION coi.guard_parse_attempt_evidence();

CREATE INDEX parse_attempts_unusable_artifact_idx
    ON coi.parse_attempts
        (result_artifact_backend, result_storage_account_name,
         result_container, result_blob_name, result_blob_version_id)
    WHERE error_code = 'retained_artifact_unusable'
      AND result_blob_name IS NOT NULL;

CREATE INDEX parse_attempts_terminal_resume_source_idx
    ON coi.parse_attempts ((metadata ->> 'source_parse_attempt_id'))
    WHERE provider = 'pdf.co-resume'
      AND error_code = 'pdfco_terminal_error';

CREATE VIEW coi.document_review_queue AS
SELECT
    'document'::TEXT        AS review_item_kind,
    doc.document_id,
    NULL::BIGINT            AS document_source_id,
    doc.original_filename   AS source_filename,
    doc.document_type,
    doc.vendor,
    doc.status              AS processing_status,
    doc.review_status,
    doc.error_code,
    doc.error_message,
    doc.updated_at           AS occurred_at
FROM coi.documents AS doc
WHERE doc.review_status = 'needs_review'
   OR doc.status IN ('needs_review', 'failed')

UNION ALL

SELECT
    'source'::TEXT          AS review_item_kind,
    source.document_id,
    source.document_source_id,
    source.source_filename,
    doc.document_type,
    doc.vendor,
    doc.status              AS processing_status,
    source.review_status,
    source.error_code,
    source.error_message,
    source.created_at        AS occurred_at
FROM coi.document_sources AS source
JOIN coi.documents AS doc ON doc.document_id = source.document_id
WHERE source.review_status = 'needs_review';

COMMENT ON VIEW coi.document_review_queue IS
    'Document failures/reviews plus source-level classification conflicts without downgrading canonical documents.';

COMMENT ON FUNCTION coi.guard_document_artifact_evidence() IS
    'Requires every document raw-artifact pointer assignment to have append-only audit evidence.';
COMMENT ON FUNCTION coi.guard_parse_attempt_evidence() IS
    'Prevents mutation of completed attempts and replacement of provider job or result evidence.';
