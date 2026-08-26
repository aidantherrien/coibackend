-- COI database baseline for PostgreSQL 16.
--
-- The migration runner creates the coi schema and checksum-bearing migration
-- ledger, wraps this whole file in one transaction, and applies files in name
-- order. Do not execute this file statement-by-statement outside that runner.

-- A document is one distinct set of bytes retained in Azure Blob Storage (or
-- the local development store). Repeated
-- appearances of those bytes belong in document_sources, not in another row.
CREATE TABLE coi.documents (
    document_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256                    TEXT        NOT NULL,
    original_filename         TEXT        NOT NULL,
    document_type             TEXT        NOT NULL DEFAULT 'unknown',
    vendor                    TEXT,
    artifact_backend          TEXT,
    storage_account_name      TEXT,
    storage_container         TEXT,
    blob_name                 TEXT,
    blob_version_id           TEXT,
    content_type              TEXT        NOT NULL DEFAULT 'application/pdf',
    content_length_bytes      BIGINT,
    status                    TEXT        NOT NULL DEFAULT 'discovered',
    review_status             TEXT        NOT NULL DEFAULT 'pending',
    duplicate_of_document_id  BIGINT
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    error_code                TEXT,
    error_message             TEXT,
    metadata                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT documents_sha256_unique UNIQUE (sha256),
    CONSTRAINT documents_sha256_format
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT documents_original_filename_nonblank
        CHECK (original_filename = btrim(original_filename)
               AND original_filename <> ''),
    CONSTRAINT documents_document_type_valid
        CHECK (document_type IN
               ('unknown', 'invoice', 'oa', 'order_acknowledgement', 'other')),
    CONSTRAINT documents_vendor_normalized
        CHECK (vendor IS NULL OR (vendor = btrim(vendor) AND vendor <> '')),
    CONSTRAINT documents_artifact_backend_valid
        CHECK (artifact_backend IS NULL
               OR artifact_backend IN ('local', 'azure_blob')),
    CONSTRAINT documents_storage_account_name_normalized
        CHECK (storage_account_name IS NULL
               OR storage_account_name ~ '^[a-z0-9]{3,24}$'),
    CONSTRAINT documents_storage_container_normalized
        CHECK (storage_container IS NULL
               OR (storage_container = btrim(storage_container)
                   AND storage_container <> '')),
    CONSTRAINT documents_blob_name_normalized
        CHECK (blob_name IS NULL
               OR (blob_name = btrim(blob_name) AND blob_name <> '')),
    CONSTRAINT documents_blob_version_id_normalized
        CHECK (blob_version_id IS NULL
               OR (blob_version_id = btrim(blob_version_id)
                   AND blob_version_id <> '')),
    CONSTRAINT documents_artifact_coordinates_valid
        CHECK (
            (artifact_backend IS NULL
             AND storage_account_name IS NULL
             AND storage_container IS NULL
             AND blob_name IS NULL
             AND blob_version_id IS NULL)
            OR
            (artifact_backend IS NOT NULL
             AND artifact_backend = 'local'
             AND storage_account_name IS NULL
             AND storage_container IS NULL
             AND blob_name IS NOT NULL
             AND blob_version_id IS NULL)
            OR
            (artifact_backend IS NOT NULL
             AND artifact_backend = 'azure_blob'
             AND storage_account_name IS NOT NULL
             AND storage_container IS NOT NULL
             AND blob_name IS NOT NULL)
        ),
    CONSTRAINT documents_content_type_nonblank
        CHECK (content_type = btrim(content_type) AND content_type <> ''),
    CONSTRAINT documents_content_length_valid
        CHECK (content_length_bytes IS NULL OR content_length_bytes >= 0),
    CONSTRAINT documents_status_valid
        CHECK (status IN
               ('discovered', 'stored', 'processing', 'parsed',
                'needs_review', 'failed')),
    CONSTRAINT documents_review_status_valid
        CHECK (review_status IN
               ('pending', 'not_required', 'needs_review', 'approved', 'rejected')),
    CONSTRAINT documents_duplicate_not_self
        CHECK (duplicate_of_document_id IS NULL
               OR duplicate_of_document_id <> document_id),
    CONSTRAINT documents_error_code_normalized
        CHECK (error_code IS NULL
               OR (error_code = btrim(error_code) AND error_code <> '')),
    CONSTRAINT documents_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT documents_timestamps_ordered
        CHECK (updated_at >= created_at)
);

COMMENT ON TABLE coi.documents IS
    'Content-addressed physical documents and their durable Azure blob or local-artifact locations.';
COMMENT ON COLUMN coi.documents.sha256 IS
    'Lowercase hexadecimal SHA-256 of the exact source bytes; the content deduplication key.';
COMMENT ON COLUMN coi.documents.duplicate_of_document_id IS
    'Optional reviewer-confirmed business duplicate with different bytes; identical bytes reuse one document row.';
COMMENT ON COLUMN coi.documents.artifact_backend IS
    'Artifact backend identity: local or azure_blob; NULL only before raw retention succeeds.';
COMMENT ON COLUMN coi.documents.storage_account_name IS
    'Exact Azure Storage account name that scopes storage_container; NULL for local artifacts or before retention.';
COMMENT ON COLUMN coi.documents.blob_version_id IS
    'Azure Blob version identifier when versioning is enabled; NULL for an unversioned or local artifact.';
COMMENT ON COLUMN coi.documents.blob_name IS
    'Durable Azure blob name or local relative path; NULL only before storage succeeds. Local development has no container.';
COMMENT ON COLUMN coi.documents.metadata IS
    'Non-authoritative provider/source metadata that does not warrant a first-class column.';

CREATE INDEX documents_status_review_idx
    ON coi.documents (status, review_status, created_at);
CREATE INDEX documents_type_vendor_idx
    ON coi.documents (document_type, vendor, created_at);
CREATE INDEX documents_duplicate_of_idx
    ON coi.documents (duplicate_of_document_id)
    WHERE duplicate_of_document_id IS NOT NULL;
CREATE UNIQUE INDEX documents_storage_blob_unique_idx
    ON coi.documents
        (artifact_backend, storage_account_name, storage_container,
         blob_name, blob_version_id)
    NULLS NOT DISTINCT
    WHERE blob_name IS NOT NULL;

-- One document may arrive more than once (for example as a resent attachment).
-- source_reference is a stable, source-specific occurrence key, not a filename.
CREATE TABLE coi.document_sources (
    document_source_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id         BIGINT      NOT NULL
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    source_kind         TEXT        NOT NULL,
    source_reference    TEXT        NOT NULL,
    source_mailbox      TEXT,
    message_id          TEXT,
    attachment_id       TEXT,
    source_filename     TEXT,
    source_sender       TEXT,
    received_at         TIMESTAMPTZ,
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT document_sources_occurrence_unique
        UNIQUE (source_kind, source_reference),
    CONSTRAINT document_sources_source_kind_nonblank
        CHECK (source_kind = btrim(source_kind) AND source_kind <> ''),
    CONSTRAINT document_sources_source_reference_nonblank
        CHECK (source_reference = btrim(source_reference)
               AND source_reference <> ''),
    CONSTRAINT document_sources_mailbox_normalized
        CHECK (source_mailbox IS NULL
               OR (source_mailbox = btrim(source_mailbox)
                   AND source_mailbox <> '')),
    CONSTRAINT document_sources_message_id_normalized
        CHECK (message_id IS NULL
               OR (message_id = btrim(message_id) AND message_id <> '')),
    CONSTRAINT document_sources_attachment_id_normalized
        CHECK (attachment_id IS NULL
               OR (attachment_id = btrim(attachment_id)
                   AND attachment_id <> '')),
    CONSTRAINT document_sources_filename_normalized
        CHECK (source_filename IS NULL
               OR (source_filename = btrim(source_filename)
                   AND source_filename <> '')),
    CONSTRAINT document_sources_sender_normalized
        CHECK (source_sender IS NULL
               OR (source_sender = btrim(source_sender)
                   AND source_sender <> '')),
    CONSTRAINT document_sources_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object')
);

COMMENT ON TABLE coi.document_sources IS
    'Every observed arrival or source occurrence of a content-deduplicated document.';
COMMENT ON COLUMN coi.document_sources.source_reference IS
    'Stable identifier within source_kind, such as a Graph attachment locator or canonical import path.';

CREATE INDEX document_sources_document_idx
    ON coi.document_sources (document_id, created_at);
CREATE INDEX document_sources_mail_message_idx
    ON coi.document_sources (source_mailbox, message_id)
    WHERE source_mailbox IS NOT NULL AND message_id IS NOT NULL;
CREATE INDEX document_sources_received_idx
    ON coi.document_sources (received_at DESC)
    WHERE received_at IS NOT NULL;

-- Attempts are never overwritten by a retry. A new attempt preserves provider
-- job identity, output JSON coordinates, timing, and failure evidence.
CREATE TABLE coi.parse_attempts (
    parse_attempt_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id        BIGINT      NOT NULL
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    provider           TEXT        NOT NULL,
    provider_job_id    TEXT,
    parser_version     TEXT,
    status             TEXT        NOT NULL DEFAULT 'started',
    result_artifact_backend      TEXT,
    result_storage_account_name  TEXT,
    result_container             TEXT,
    result_blob_name             TEXT,
    result_blob_version_id       TEXT,
    error_code         TEXT,
    error_message      TEXT,
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at       TIMESTAMPTZ,

    CONSTRAINT parse_attempts_provider_nonblank
        CHECK (provider = btrim(provider) AND provider <> ''),
    CONSTRAINT parse_attempts_provider_job_id_normalized
        CHECK (provider_job_id IS NULL
               OR (provider_job_id = btrim(provider_job_id)
                   AND provider_job_id <> '')),
    CONSTRAINT parse_attempts_parser_version_normalized
        CHECK (parser_version IS NULL
               OR (parser_version = btrim(parser_version)
                   AND parser_version <> '')),
    CONSTRAINT parse_attempts_status_valid
        CHECK (status IN ('started', 'processing', 'succeeded', 'failed')),
    CONSTRAINT parse_attempts_result_artifact_backend_valid
        CHECK (result_artifact_backend IS NULL
               OR result_artifact_backend IN ('local', 'azure_blob')),
    CONSTRAINT parse_attempts_result_storage_account_name_normalized
        CHECK (result_storage_account_name IS NULL
               OR result_storage_account_name ~ '^[a-z0-9]{3,24}$'),
    CONSTRAINT parse_attempts_result_container_normalized
        CHECK (result_container IS NULL
               OR (result_container = btrim(result_container)
                   AND result_container <> '')),
    CONSTRAINT parse_attempts_result_blob_name_normalized
        CHECK (result_blob_name IS NULL
               OR (result_blob_name = btrim(result_blob_name)
                   AND result_blob_name <> '')),
    CONSTRAINT parse_attempts_result_blob_version_normalized
        CHECK (result_blob_version_id IS NULL
               OR (result_blob_version_id = btrim(result_blob_version_id)
                   AND result_blob_version_id <> '')),
    CONSTRAINT parse_attempts_result_artifact_coordinates_valid
        CHECK (
            (result_artifact_backend IS NULL
             AND result_storage_account_name IS NULL
             AND result_container IS NULL
             AND result_blob_name IS NULL
             AND result_blob_version_id IS NULL)
            OR
            (result_artifact_backend IS NOT NULL
             AND result_artifact_backend = 'local'
             AND result_storage_account_name IS NULL
             AND result_container IS NULL
             AND result_blob_name IS NOT NULL
             AND result_blob_version_id IS NULL)
            OR
            (result_artifact_backend IS NOT NULL
             AND result_artifact_backend = 'azure_blob'
             AND result_storage_account_name IS NOT NULL
             AND result_container IS NOT NULL
             AND result_blob_name IS NOT NULL)
        ),
    CONSTRAINT parse_attempts_error_code_normalized
        CHECK (error_code IS NULL
               OR (error_code = btrim(error_code) AND error_code <> '')),
    CONSTRAINT parse_attempts_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT parse_attempts_timestamps_ordered
        CHECK (completed_at IS NULL OR completed_at >= started_at)
);

COMMENT ON TABLE coi.parse_attempts IS
    'Immutable retry history for external parsing jobs and their stored result artifacts.';
COMMENT ON COLUMN coi.parse_attempts.result_artifact_backend IS
    'Artifact backend identity for the retained parser response: local or azure_blob.';
COMMENT ON COLUMN coi.parse_attempts.result_storage_account_name IS
    'Exact Azure Storage account name that scopes result_container; NULL for local artifacts.';
COMMENT ON COLUMN coi.parse_attempts.result_blob_name IS
    'Azure blob name or local relative path of the raw parser response retained for replay and audit.';

CREATE UNIQUE INDEX parse_attempts_provider_job_unique_idx
    ON coi.parse_attempts (provider, provider_job_id)
    WHERE provider_job_id IS NOT NULL;
CREATE INDEX parse_attempts_document_started_idx
    ON coi.parse_attempts (document_id, started_at DESC);
CREATE INDEX parse_attempts_active_idx
    ON coi.parse_attempts (status, started_at)
    WHERE status IN ('started', 'processing');

CREATE TABLE coi.invoice_summary (
    invoice_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id           BIGINT      NOT NULL UNIQUE
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    vendor                TEXT        NOT NULL,
    invoice_no            TEXT        NOT NULL,
    order_no              TEXT,
    po                    TEXT        NOT NULL,
    account_no            TEXT,
    salesman              TEXT,
    invoice_date          DATE,
    order_date            DATE,
    terms                 TEXT,
    freight_terms         TEXT,
    subtotal              NUMERIC(18,2),
    freight               NUMERIC(18,2),
    misc                  NUMERIC(18,2),
    tax                   NUMERIC(18,2),
    less_prepaid_deposit  NUMERIC(18,2),
    total                 NUMERIC(18,2) NOT NULL,
    currency              TEXT          NOT NULL DEFAULT 'USD',
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT invoice_summary_business_key_unique
        UNIQUE (vendor, invoice_no),
    CONSTRAINT invoice_summary_vendor_nonblank
        CHECK (vendor = btrim(vendor) AND vendor <> ''),
    CONSTRAINT invoice_summary_invoice_no_nonblank
        CHECK (invoice_no = btrim(invoice_no) AND invoice_no <> ''),
    CONSTRAINT invoice_summary_order_no_normalized
        CHECK (order_no IS NULL OR (order_no = btrim(order_no) AND order_no <> '')),
    CONSTRAINT invoice_summary_po_nonblank
        CHECK (po = btrim(po) AND po <> ''),
    CONSTRAINT invoice_summary_account_no_normalized
        CHECK (account_no IS NULL
               OR (account_no = btrim(account_no) AND account_no <> '')),
    CONSTRAINT invoice_summary_salesman_normalized
        CHECK (salesman IS NULL OR (salesman = btrim(salesman) AND salesman <> '')),
    CONSTRAINT invoice_summary_currency_format
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT invoice_summary_timestamps_ordered
        CHECK (updated_at >= created_at)
);

COMMENT ON TABLE coi.invoice_summary IS
    'One normalized vendor invoice per physical source document.';
COMMENT ON COLUMN coi.invoice_summary.document_id IS
    'One-to-one provenance link; business-key conflicts with a different document require review.';
COMMENT ON COLUMN coi.invoice_summary.currency IS
    'ISO 4217 alphabetic currency code; amounts deliberately allow negatives for credits/adjustments.';

CREATE INDEX invoice_summary_po_idx ON coi.invoice_summary (po);
CREATE INDEX invoice_summary_invoice_no_idx ON coi.invoice_summary (invoice_no);
CREATE INDEX invoice_summary_order_no_idx ON coi.invoice_summary (order_no)
    WHERE order_no IS NOT NULL;
CREATE INDEX invoice_summary_vendor_date_idx
    ON coi.invoice_summary (vendor, invoice_date DESC);

CREATE TABLE coi.invoice_line_items (
    invoice_line_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id       BIGINT      NOT NULL
        REFERENCES coi.invoice_summary (invoice_id) ON DELETE CASCADE,
    line_position    INTEGER     NOT NULL,
    source_line_no   TEXT,
    ord_qty          NUMERIC(18,3),
    ship_qty         NUMERIC(18,3),
    bo_qty           NUMERIC(18,3),
    product_code     TEXT,
    description      TEXT,
    price_list       NUMERIC(18,4),
    discount_pct     NUMERIC(9,4),
    net_price        NUMERIC(18,4),
    extension        NUMERIC(18,2),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT invoice_line_items_position_unique
        UNIQUE (invoice_id, line_position),
    CONSTRAINT invoice_line_items_position_positive
        CHECK (line_position > 0),
    CONSTRAINT invoice_line_items_source_line_no_normalized
        CHECK (source_line_no IS NULL
               OR (source_line_no = btrim(source_line_no) AND source_line_no <> '')),
    CONSTRAINT invoice_line_items_product_code_normalized
        CHECK (product_code IS NULL
               OR (product_code = btrim(product_code) AND product_code <> '')),
    CONSTRAINT invoice_line_items_timestamps_ordered
        CHECK (updated_at >= created_at)
);

COMMENT ON TABLE coi.invoice_line_items IS
    'Ordered invoice product lines; PO is obtained from invoice_summary and is not duplicated here.';
COMMENT ON COLUMN coi.invoice_line_items.line_position IS
    'Stable parser order within this document, independent of the vendor-printed source_line_no.';
COMMENT ON COLUMN coi.invoice_line_items.source_line_no IS
    'Vendor-provided line identifier preserved as text, including leading zeros or nonnumeric values.';

CREATE INDEX invoice_line_items_product_code_idx
    ON coi.invoice_line_items (product_code)
    WHERE product_code IS NOT NULL;

CREATE TABLE coi.oa_summary (
    oa_id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id             BIGINT      NOT NULL UNIQUE
        REFERENCES coi.documents (document_id) ON DELETE RESTRICT,
    vendor                  TEXT        NOT NULL,
    order_no                TEXT        NOT NULL,
    po                      TEXT        NOT NULL,
    account_no              TEXT,
    salesman                TEXT,
    order_date              DATE,
    ship_date               DATE,
    terms                   TEXT,
    reference               TEXT,
    freight_terms           TEXT,
    fob                     TEXT,
    subtotal                NUMERIC(18,2),
    freight                 NUMERIC(18,2),
    total                   NUMERIC(18,2) NOT NULL,
    retail_extension_total  NUMERIC(18,2),
    currency                TEXT          NOT NULL DEFAULT 'USD',
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT oa_summary_business_key_unique UNIQUE (vendor, order_no),
    CONSTRAINT oa_summary_vendor_nonblank
        CHECK (vendor = btrim(vendor) AND vendor <> ''),
    CONSTRAINT oa_summary_order_no_nonblank
        CHECK (order_no = btrim(order_no) AND order_no <> ''),
    CONSTRAINT oa_summary_po_nonblank
        CHECK (po = btrim(po) AND po <> ''),
    CONSTRAINT oa_summary_account_no_normalized
        CHECK (account_no IS NULL
               OR (account_no = btrim(account_no) AND account_no <> '')),
    CONSTRAINT oa_summary_salesman_normalized
        CHECK (salesman IS NULL OR (salesman = btrim(salesman) AND salesman <> '')),
    CONSTRAINT oa_summary_currency_format
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT oa_summary_timestamps_ordered
        CHECK (updated_at >= created_at)
);

COMMENT ON TABLE coi.oa_summary IS
    'One normalized vendor order acknowledgement per physical source document.';
COMMENT ON COLUMN coi.oa_summary.currency IS
    'ISO 4217 alphabetic currency code; amounts deliberately allow negatives for credits/adjustments.';

CREATE INDEX oa_summary_po_idx ON coi.oa_summary (po);
CREATE INDEX oa_summary_order_no_idx ON coi.oa_summary (order_no);
CREATE INDEX oa_summary_vendor_date_idx
    ON coi.oa_summary (vendor, order_date DESC);

CREATE TABLE coi.oa_line_items (
    oa_line_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    oa_id             BIGINT      NOT NULL
        REFERENCES coi.oa_summary (oa_id) ON DELETE CASCADE,
    line_position     INTEGER     NOT NULL,
    source_line_no    TEXT,
    qty               NUMERIC(18,3),
    product_code      TEXT,
    description       TEXT,
    retail_price      NUMERIC(18,4),
    retail_extension  NUMERIC(18,2),
    discount_pct      NUMERIC(9,4),
    net_price         NUMERIC(18,4),
    extension         NUMERIC(18,2),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT oa_line_items_position_unique UNIQUE (oa_id, line_position),
    CONSTRAINT oa_line_items_position_positive CHECK (line_position > 0),
    CONSTRAINT oa_line_items_source_line_no_normalized
        CHECK (source_line_no IS NULL
               OR (source_line_no = btrim(source_line_no) AND source_line_no <> '')),
    CONSTRAINT oa_line_items_product_code_normalized
        CHECK (product_code IS NULL
               OR (product_code = btrim(product_code) AND product_code <> '')),
    CONSTRAINT oa_line_items_timestamps_ordered
        CHECK (updated_at >= created_at)
);

COMMENT ON TABLE coi.oa_line_items IS
    'Ordered OA product lines; PO is obtained from oa_summary and is not duplicated here.';
COMMENT ON COLUMN coi.oa_line_items.line_position IS
    'Stable parser order within this document, independent of the vendor-printed source_line_no.';
COMMENT ON COLUMN coi.oa_line_items.source_line_no IS
    'Vendor-provided line identifier preserved as text, including leading zeros or nonnumeric values.';

CREATE INDEX oa_line_items_product_code_idx
    ON coi.oa_line_items (product_code)
    WHERE product_code IS NOT NULL;

CREATE FUNCTION coi.touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;

COMMENT ON FUNCTION coi.touch_updated_at() IS
    'Maintains updated_at whenever a mutable row is updated.';

CREATE TRIGGER documents_touch_updated_at
BEFORE UPDATE ON coi.documents
FOR EACH ROW EXECUTE FUNCTION coi.touch_updated_at();

CREATE TRIGGER invoice_summary_touch_updated_at
BEFORE UPDATE ON coi.invoice_summary
FOR EACH ROW EXECUTE FUNCTION coi.touch_updated_at();

CREATE TRIGGER invoice_line_items_touch_updated_at
BEFORE UPDATE ON coi.invoice_line_items
FOR EACH ROW EXECUTE FUNCTION coi.touch_updated_at();

CREATE TRIGGER oa_summary_touch_updated_at
BEFORE UPDATE ON coi.oa_summary
FOR EACH ROW EXECUTE FUNCTION coi.touch_updated_at();

CREATE TRIGGER oa_line_items_touch_updated_at
BEFORE UPDATE ON coi.oa_line_items
FOR EACH ROW EXECUTE FUNCTION coi.touch_updated_at();
