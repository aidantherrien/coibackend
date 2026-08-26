# COI document-ingestion backend

This repository ingests vendor invoices and order acknowledgements, retains the
source and parser evidence in Azure Blob Storage, maps PDF.co output into exact
PostgreSQL types, and provides safe purchase-order reconciliation views. Artopex
is the first vendor; the provenance, mapping, and migration boundaries support
adding more vendors deliberately.

The repository is prepared for COI's existing Azure development VM,
`vm-coi-portal-dev-01`. It does not recreate that VM and does not expose an
application or database port. PostgreSQL can run locally on the VM for the
controlled pilot; Azure Database for PostgreSQL Flexible Server is the later
managed production target.

## Design at a glance

```text
folder inbox on private Azure VM
   -> age gate + one bounded, stable byte snapshot
   -> SHA-256 identity + source occurrence
   -> private/versioned Azure Blob in raw-pdfs
   -> bounded PDF.co asynchronous job
   -> private/versioned Azure Blob in pdfco-json
   -> strict mapping (Decimal/date/identifier validation)
   -> PostgreSQL summary + ordered line items
   -> PO overview + aggregate reconciliation views
```

The main invariants are:

- Exact bytes are deduplicated by SHA-256. A repeated arrival becomes another
  `document_sources` occurrence, not another business row.
- A source is rejected if it is a symlink, changes during its one snapshot read,
  exceeds `MAX_PDF_BYTES`, or is too new to assume its producer finished writing.
- Each normalized invoice or OA links to one source document, while every parser
  retry remains visible in `parse_attempts`.
- Initial raw retention and verified integrity repairs are append-only in
  `document_artifacts`. The document pointer must match audited container,
  blob-name, version, SHA-256, and byte-length evidence.
- Financial values use PostgreSQL `NUMERIC` and Python `Decimal`, never binary
  floating point.
- Reconciliation aggregates both sides before joining, preventing repeated
  product lines from multiplying totals.
- Schema changes are ordered, transactional migrations with immutable checksums.
  The service refuses a stale, modified, or unknown schema.

See [architecture.md](docs/architecture.md) for the complete model and recovery
rules.

## Repository layout

```text
coi_backend/                 application package and CLI
sql/migrations/              ordered PostgreSQL migrations
sql/grants/                  owner/migrator/runtime role setup
sql/cross_validate.sql       reconciliation query example
sql/po_join.sql              document-overview query example
scripts/                     compatibility ingestion wrappers
tests/                       unit and opt-in PostgreSQL integration tests
infra/azure/                 Azure Bicep and safe parameter examples
deploy/systemd/              Azure VM timer/service units
docs/                        architecture and deployment runbooks
```

`data/`, `var/`, `.env`, and `codex_context/` are intentionally untracked. The
context notes are historical planning material, not deployment inputs. Invoice
PDFs are runtime inputs, not test fixtures, and the test suite does not inspect
them.

## Azure naming and identity

Deployment assets follow Microsoft Cloud Adoption Framework prefixes:
`rg-` resource groups, `vm-` virtual machines, `st` storage accounts, `kv-` Key
Vaults, `log-` Log Analytics workspaces, and `psql-` PostgreSQL servers. Storage
accounts omit hyphens because Azure requires globally unique lowercase names.
Blob coordinates use Azure terms throughout the code and database:
`artifact_backend`, `storage_account_name`, `storage_container`, `blob_name`,
and `blob_version_id`. The storage-account identity is retained because a
container name is scoped to one Azure Storage account, not globally unique.

Azure access uses `DefaultAzureCredential`:

- on the VM, the system-assigned managed identity is used;
- on a developer workstation, an authenticated Azure CLI session can be used;
- no storage account key, connection string, or service-principal secret is
  required by the application.

The VM identity needs `Storage Blob Data Contributor` on each required Blob
container and, when Key Vault is enabled, `Key Vault Secrets User` on the vault.
Contributor alone generally cannot create those role assignments; an Owner,
User Access Administrator, or delegated access administrator must review them.

## Prerequisites

- Python 3.12 through 3.14 (production and CI target 3.12)
- PostgreSQL 16
- a PDF.co API key
- for Azure Blob mode, a GPv2 storage account plus private `raw-pdfs` and
  `pdfco-json` containers
- Azure CLI and Bicep for provisioning or validating Azure resources

## Local setup

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Linux:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
```

Fill `.env` without committing it. Production installs the fully resolved
`requirements.lock`; `requirements.txt` is the short direct-dependency list used
only when intentionally refreshing that lock.

For a local artifact directory:

```dotenv
ARTIFACT_STORE=local
LOCAL_ARTIFACT_DIR=var/artifacts
```

For Azure Blob Storage:

```dotenv
ARTIFACT_STORE=azure_blob
AZURE_STORAGE_ACCOUNT_URL=https://stexample.blob.core.windows.net
AZURE_RAW_CONTAINER=raw-pdfs
AZURE_PARSER_CONTAINER=pdfco-json
AZURE_STORAGE_PREFIX=
```

The backend choice is explicit. Merely setting an Azure account URL does not
switch away from local storage, and an incomplete Azure Blob configuration fails
startup.

For the first private pilot, `DATABASE_URL` and `PDFCO_API_KEY` can be direct
environment values in a root-owned mode-`0600` VM file. With Key Vault, leave
those direct values blank and configure:

```dotenv
AZURE_KEY_VAULT_URL=https://kv-example.vault.azure.net
DATABASE_SECRET_NAME=database-url
PDFCO_API_KEY_SECRET_NAME=pdfco-api-key
```

Never set both forms of one secret.

## Create or migrate the database

For a disposable local database whose login can create a schema, leave
`DATABASE_MIGRATION_ROLE` blank and run:

```bash
python -m coi_backend.cli migrate
python -m coi_backend.cli check
```

For a shared or managed database, keep deployment and runtime privileges
separate:

1. As the database administrator, run `sql/grants/bootstrap_roles.sql`.
2. Grant `coi_migrator` to the real deployment login.
3. Set `DATABASE_MIGRATION_ROLE=coi_owner` only in the deployment environment
   and run `python -m coi_backend.cli migrate`.
4. As administrator, run `sql/grants/least_privilege.sql`.
5. Grant `coi_runtime` to the application login. Never grant that login
   `coi_owner` or `coi_migrator`.
6. Remove `DATABASE_MIGRATION_ROLE` from the service environment and run the
   check using the runtime login.

Both grant scripts are safe to re-run. The runtime can ingest and read the
migration ledger but cannot alter the ledger, delete evidence, or create schema
objects. Read [database-migration.md](docs/database-migration.md) before pointing
this release at a database made by the legacy one-shot script.

## Run ingestion

```bash
python -m coi_backend.cli ingest --type invoice
python -m coi_backend.cli ingest --type oa
```

Override the configured input directory with `--input-dir`. Compatibility
wrappers remain available:

```bash
python scripts/ingest_invoices.py
python scripts/ingest_oa.py
```

The command exits nonzero if a document fails, needs review, or is skipped.
`--force-retry` can reclaim an old failed/review document or a processing claim
older than `PROCESSING_STALE_AFTER_SECONDS`. It first replays retained JSON or
resumes a known PDF.co job. If neither recovery source exists, a new potentially
billable parse requires both `--force-retry` and `--allow-new-paid-parse`.

`SOURCE_MIN_AGE_SECONDS` defaults to 60, `MAX_PDF_BYTES` to 25 MiB, and
`MAX_PARSER_JSON_BYTES` to 10 MiB. New or changing sources are deferred and left
in the inbox. Permanent policy, mapping, business-key, and terminal parser
failures move to quarantine when configured. Successes and content duplicates
move to archive. Transient storage or parser failures stay in the inbox for
retry and still alert with a nonzero exit.

Useful queries:

```sql
SELECT * FROM coi.po_document_overview
WHERE vendor = 'ARTOPEX' AND po = 'replace-at-runtime';

SELECT * FROM coi.po_product_reconciliation
WHERE vendor = 'ARTOPEX' AND reconciliation_status <> 'matched';
```

## Test and validate

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
```

Database integration is opt-in so tests never touch an accidental database.
Supply an empty disposable PostgreSQL database through `TEST_DATABASE_URL`, then
run:

```bash
python -m pytest -m integration -q
```

CI runs formatting, lint, unit/integration tests, Bicep validation, and a
full-history secret scan.

## Deploy to Azure

Follow [azure-deployment.md](docs/azure-deployment.md). It distinguishes:

- confirmed state: the existing Ubuntu VM and `/opt/coi` work directory;
- pilot work: Blob Storage, managed identity/RBAC, optional Key Vault, local
  PostgreSQL, a versioned application release, and systemd timers;
- later production gate: private Azure Database for PostgreSQL Flexible Server.

The Bicep files create durable platform resources but do not recreate or replace
the existing VM. Review `what-if` output and Azure costs before every deployment.
The first application remains private: no public PostgreSQL, port 8000, HTTP, or
HTTPS rule is part of this repository.

## Security and operational boundaries

- Azure Blob writes use content-addressed names, SHA-256 metadata, private
  containers, account encryption, versioning, and soft-delete protection.
- PDF.co polling is bounded. The billable parse-start request is not retried
  automatically after an ambiguous timeout.
- Parser result downloads use a credential-free session and an exact hostname
  allowlist; redirects and alternate ports are rejected.
- Raw PDFs and parser JSON are evidence. Normalized rows alone are not treated
  as sufficient provenance.
- Backups, secret rotation, alert routing, retention approval, and restore drills
  remain explicit deployment-owner responsibilities.

Before publishing or deploying, read [SECURITY.md](SECURITY.md). A known
credential was removed from the live tree, but deletion does not remove it from
Git history; rotate it if it was ever valid.
