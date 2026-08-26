# Architecture and data invariants

## Scope

The current service is a private batch worker, not a public web application. It
discovers invoice and order-acknowledgement PDFs in configured folders, calls
PDF.co's AI Invoice Parser, retains the source evidence in Azure Blob Storage,
and writes normalized PostgreSQL records. Artopex is the first vendor. A new
vendor should add an explicit mapper rather than weakening the existing input
validation.

The development deployment runs on the existing Azure VM
`vm-coi-portal-dev-01`. PostgreSQL may run locally on that VM for the controlled
pilot. Azure Database for PostgreSQL Flexible Server is the production target
before live mailbox ingestion becomes business-critical or multiple employees
depend on the service. PostgreSQL remains the database in both stages.

The tracked application, SQL migrations, and Azure deployment runbook are the
source of truth. The ignored `codex_context/` notes explain the product history
but are not deployment inputs.

## Azure boundary

The durable cloud boundary is deliberately small:

```text
private folder inbox on Azure VM
    -> bounded source snapshot
    -> Azure Blob Storage (raw-pdfs)
    -> PDF.co asynchronous parse
    -> Azure Blob Storage (pdfco-json)
    -> PostgreSQL
```

The storage account also reserves `raw-emails` for a future Microsoft Graph
collector and `db-backups` for verified pilot database dumps. Those workflows
are not implemented by the ingestion worker yet. Blob containers are private;
the VM authenticates with its system-assigned managed identity. Local Azure CLI
credentials can be used by developers through the same `DefaultAzureCredential`
chain. Storage account keys and connection strings are not application
configuration.

The first deployment has no public application or database listener. SSH stays
restricted by the VM's network security group, and a future private search demo
should bind to `127.0.0.1` and be reached through an SSH tunnel. Nginx, public
DNS, Microsoft Graph collection, and an employee portal are separate milestones.

## Data model

`coi.documents` represents unique file bytes. Its lowercase SHA-256 is globally
unique. `artifact_backend`, `storage_account_name`, `storage_container`,
`blob_name`, and `blob_version_id` point to the currently verified source
artifact. Persisting the exact Azure Storage account is essential because a
container name is unique only within its account. A local artifact uses
`artifact_backend = 'local'`, null account/container values, and a root-relative
path; an Azure artifact uses `azure_blob` plus its exact account and container.
Classification, processing state, review state, bounded errors, and source size
live on the document.

`coi.document_artifacts` is the append-only evidence behind that convenient
current pointer. Initial retention and every verified integrity repair record
their own storage coordinate, document hash, byte length, kind, and timestamp.
A database trigger rejects a pointer change unless an exactly matching evidence
row already exists. Blob version IDs are recorded when Azure returns them; the
content-addressed blob name and SHA-256 metadata remain the integrity boundary.

Folder discovery does not hash and later reopen a live producer file. It first
rejects symlinks and non-regular entries, enforces `SOURCE_MIN_AGE_SECONDS`, and
reads one `MAX_PDF_BYTES`-bounded snapshot while hashing and counting. File
descriptor and directory-entry metadata are checked before and after the read.
Registration, raw retention, and PDF.co upload all use those same bytes, which
closes the source-file time-of-check/time-of-use gap.

`coi.document_sources` represents observations of those bytes. The same
attachment can arrive repeatedly without creating duplicate invoices. Mailbox,
message, and attachment columns are available for a future Microsoft Graph
collector; today's folder importer records a stable local-source reference and
the observed vendor/type in JSON metadata. If an occurrence is presented under
a different type or vendor than its canonical hash, review state is attached to
that occurrence without downgrading the accepted document.
`coi.document_review_queue` combines those source conflicts with failed or
review-required canonical documents for operator triage.

`coi.parse_attempts` is append-oriented retry evidence. Provider job IDs are
unique when known, and result backend/account/container/blob/version coordinates
identify the retained raw parser JSON. Mapping replays create a separate
`retained-artifact` attempt that refers to the source attempt in metadata. A
restart can therefore resume a known job or replay existing JSON without
silently purchasing another parse.

`coi.invoice_summary` and `coi.oa_summary` each have one unique `document_id`
and one vendor-scoped business key. Their child rows retain parser order in
`line_position` and the vendor's printed identifier separately in
`source_line_no`. The PO is held on the summary, where its functional dependency
belongs, and is indexed. Identifiers use `TEXT`; money and quantities use exact
`NUMERIC`/Python `Decimal` values.

## Reconciliation

`coi.po_document_overview` presents both document types in one searchable shape.

`coi.po_product_reconciliation` first aggregates invoice lines and OA lines by
vendor, PO, product, and currency, then full-joins those aggregates. Joining raw
line tables directly could create a many-to-many row explosion when a product
occurs repeatedly or a PO spans documents.

Rows with no product code remain available for review but are excluded from
product-level reconciliation. Different currencies never reconcile. A shared
product with a missing quantity, price, or extension is `incomplete_data`, not
`matched`.

## State and recovery

A normal document moves through:

```text
discovered -> processing -> parsed
                         -> needs_review
                         -> failed
```

The raw PDF is retained before a paid parse starts. Parser JSON is retained
before normalized records are committed. Invoice/OA parent rows, ordered child
rows, document completion, and attempt completion share one database
transaction. Raw and JSON retrieval is bounded before and during local or Blob
materialization.

A definitively missing or corrupt raw artifact can be repaired only from the
current verified source snapshot. The replacement receives a new coordinate and
the prior coordinate remains in artifact history. Backend/configuration
mismatches, temporary Azure Storage failures, and size-policy failures never
trigger a repair or invalidate retained evidence.

Content duplicates already in `parsed` return without calling PDF.co. Failed and
review documents require `--force-retry`. A processing document is reclaimable
only when forced and older than `PROCESSING_STALE_AFTER_SECONDS`; its unfinished
attempt is marked `stale_attempt_retried`. Recovery preference is:

1. Replay retained JSON.
2. Resume polling a known PDF.co job.
3. Start a new upload and parse only when neither exists.

Step 3 is automatic only for a new document. A retry with no retained result or
known job fails closed until an operator supplies both `--force-retry` and
`--allow-new-paid-parse`. A parse-start timeout can occur after PDF.co accepted
the billable request, so the service cannot safely infer that no job exists.

New or actively changing sources are deferred and remain in the inbox. When
configured, successful and content-duplicate files move atomically to a
same-filesystem archive. Permanent source, policy, mapping, business-key, and
terminal parser failures move to quarantine under collision-resistant names.
Transient storage or provider failures remain for retry and still produce a
nonzero exit. A second snapshot check prevents moving a file whose bytes changed
after ingestion.

PDF.co's billable parse-start POST has no automatic transport retry. Polling and
result downloads have bounded retries and a monotonic deadline. API metadata is
capped at 1 MiB and result JSON at `MAX_PARSER_JSON_BYTES`. Temporary result
URLs are accepted only from exact `PDFCO_RESULT_HOSTS`; redirects, IP literals,
credentials, alternate ports, and suffix matches are rejected. The result
session never carries the PDF.co API key.

## Transactions and concurrency

PostgreSQL advisory locks prevent two scheduled runs for the same vendor and
document type. Claims use compare-and-update operations, so another worker
cannot claim the same document concurrently. Business records and state changes
use transactions; source discovery and durable artifact retention happen before
the final database transaction by design.

The migration runner uses a separate advisory lock, ordered names, SHA-256
checksums, and a ledger in the `coi` schema. Applied migrations are immutable.
Unknown database versions or changed checksums fail closed. The owner, migrator,
and runtime roles work with both local PostgreSQL and Azure Database for
PostgreSQL's non-superuser administrator model.

## Security boundaries

- The runtime role can read and ingest required application records. It cannot
  delete or truncate evidence, mutate the migration ledger, create schema
  objects, or assume migration ownership.
- The deployment login receives `coi_migrator` and may set the password-free
  `coi_owner` role only while applying migrations.
- A secret is resolved from exactly one direct environment variable or one
  Azure Key Vault secret name. `DefaultAzureCredential` lets the VM use managed
  identity without an Azure credential stored on disk.
- Blob access is restricted to the configured storage account and expected
  container. Stored version IDs are used when available. A coordinate belonging
  to another backend, account, or container is unavailable configuration, not
  evidence of corrupt content.
- `ARTIFACT_STORE` explicitly chooses `local` or `azure_blob`; an incomplete
  Azure Blob configuration fails at startup.
- The protected VM-local `.env` is acceptable for the first private pilot, but
  it remains outside Git and is replaced by Key Vault references once RBAC is
  available.

## Intentional next-stage work

The database includes mailbox/message/attachment provenance, but collection is
not implemented. Before Microsoft Graph automation, confirm whether
`ap@commercialofficeinteriors.com` or
`accounting@commercialofficeinteriors.com` is authoritative and limit access to
that approved mailbox. A portal/API, Microsoft Entra user authentication,
operator review workflow, vendor registry, and project/client model remain
separate deliverables. They should consume this audit model instead of bypassing
it.
