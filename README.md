# COI Backend — Vendor Document Ingestion

Reads vendor documents (invoices and order acknowledgements) out of a folder,
parses them with **PDF.co's AI Invoice Parser**, and lands the data — cleaned,
typed, and de-duplicated — into a **PostgreSQL** database. Built to chew through
a backlog reliably, one document at a time, without hand-keying anything.

This is the Python replacement for our earlier Zapier prototype, which was fine
for a trickle of documents but not for reading years of backlog into a database.

Current scope: **Artopex** is the pilot vendor, and we handle two document types
(invoices, order acknowledgements). The design is meant to extend to more vendors
and document types without rework.

---

## Table of contents

1. [How it works, in one picture](#how-it-works-in-one-picture)
2. [Repo layout](#repo-layout)
3. [The PostgreSQL schema](#the-postgresql-schema)
4. [The ingestion scripts](#the-ingestion-scripts)
5. [The PDF.co JSON quirks (read this before editing a script)](#the-pdfco-json-quirks)
6. [Setup from scratch](#setup-from-scratch)
7. [Running it](#running-it)
8. [Security](#security)
9. [Roadmap / future direction](#roadmap--future-direction)

---

## How it works, in one picture

```
data/artopex/<type>/*.pdf
        │
        ▼
   PDF.co  ── upload → start AI-parse job → poll until done → fetch JSON
        │
        ▼
   Python  ── map the (messy, nested) JSON onto our columns; clean values
        │
        ▼
   PostgreSQL ── INSERT ... ON CONFLICT DO NOTHING  (dedup built in)
```

The single most important idea in the whole system: the **PO** (Artopex calls it
"Your Order No.", e.g. `JF823-09`) is the key that ties every document about a job
together. An invoice, its acknowledgement, and eventually its statement line all
carry the same PO, so we store it on every table and index it everywhere.

---

## Repo layout

```
coibackend/
├── README.md                 ← this file
├── .env                      ← secrets (NOT committed) — DB URL + PDF.co key
├── .env.example              ← template showing which vars to set
├── requirements.txt
│
├── sql/
│   ├── schema.sql            ← the four CREATE TABLE statements (below)
│   └── test_connection.py    ← quick "can I reach the DB / do my tables exist" check
│
├── scripts/
│   ├── ingest_oa.py          ← mass-parse order acknowledgements → oa_* tables
│   └── ingest_invoices.py    ← mass-parse invoices → invoice_* tables
│
└── data/
    └── artopex/
        ├── invoices/         ← drop invoice PDFs here
        └── oas/              ← drop acknowledgement PDFs here
```

### Why the folders are organized this way

Documents are sorted **by vendor, then by type**, and that structure is load-bearing,
not cosmetic. An invoice and an acknowledgement go to completely different tables
with different columns, so the script has to know a document's type *before* it can
parse or store it. Rather than sniff the PDF's contents (fragile), **the folder a PDF
sits in declares its type.** `ingest_invoices.py` only ever looks in `invoices/`;
`ingest_oa.py` only looks in `oas/`. Adding a vendor later means adding a
`data/<vendor>/` tree — the structure scales by copying, not by rewriting logic.

---

## The PostgreSQL schema

Four tables, in two pairs. Each document type gets a **summary** table (one row per
document) and a **line-items** table (one row per product line). See
[Why two tables per document](#why-a-summary--line-items-split) for the reasoning.

### Design principles that drive every column choice

- **Grain.** A summary row = one whole document. A line-item row = one product line
  on that document. Keeping each table at one consistent grain is what avoids
  duplicated/contradictory data.
- **Types are validation.** Giving a column a real type (`DATE`, `NUMERIC`) means the
  database *rejects* garbage at the door. A `DATE` column refuses "not a date"; a
  `NUMERIC` refuses "abc". This is the schema doing our data-cleaning for us, and it's
  why the goal of "clean enough for E-Manage" is enforced structurally rather than by
  hoping the parser behaves.
- **Money is `NUMERIC`, never float.** Floating point can't represent `0.10` exactly;
  for money that's unacceptable. `NUMERIC` is exact decimal.
- **Identifiers are `TEXT`, not numbers.** `invoice_no`, `po`, `salesman`, `account_no`
  are identifiers we never do arithmetic on. A PO has letters (`JF823-09`); a salesman
  code like `5053` could have leading zeros we'd lose as an integer. Store them as text.
- **Full fidelity in, round on the way out.** Artopex prints unit prices to **three
  decimals** (`927.850`, `20.404`, `0.770`), so unit-price columns are `NUMERIC(12,4)`
  to preserve them exactly. If E-Manage only wants two decimals, we round when we
  *export*, never when we store.

### `invoice_summary` — one row per invoice

| Column | Type | Null? | Why |
|---|---|---|---|
| `invoice_id` | `BIGINT IDENTITY` | PK | Internal handle only; what line items point at |
| `vendor` | `TEXT` | **NOT NULL** | Pins every row to a vendor; part of the dedup key |
| `invoice_no` | `TEXT` | **NOT NULL** | Artopex invoice number (`317045`); the dedup key |
| `order_no` | `TEXT` | null | Artopex's internal sales-order no (`2507177`) |
| `po` | `TEXT` | **NOT NULL** | `JF823-09` — the cross-document key |
| `account_no` | `TEXT` | null | Our customer number at the vendor (`810195`) |
| `salesman` | `TEXT` | null | Salesman code (`5053`) |
| `invoice_date` | `DATE` | null | |
| `order_date` | `DATE` | null | |
| `terms` | `TEXT` | null | `NET 30` |
| `freight_terms` | `TEXT` | null | `PREPAID` — a *term*, not a dollar amount (see quirks) |
| `subtotal` | `NUMERIC(12,2)` | null | |
| `freight` | `NUMERIC(12,2)` | null | Dollar amount; not broken out by the parser today (null) |
| `misc` | `NUMERIC(12,2)` | null | |
| `tax` | `NUMERIC(12,2)` | null | |
| `less_prepaid_deposit` | `NUMERIC(12,2)` | null | `0.00` on most Artopex invoices |
| `total` | `NUMERIC(12,2)` | **NOT NULL** | A document with no total is not a usable record |
| `currency` | `TEXT` | NOT NULL, default `'USD'` | Never left missing |
| `source_file` | `TEXT` | null | The filename it came from — audit trail |
| `ingested_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | When we loaded it |
| | | | **UNIQUE (vendor, invoice_no)** ← exactly-once |

### `invoice_line_items` — one row per product line

| Column | Type | Null? | Why |
|---|---|---|---|
| `invoice_line_id` | `BIGINT IDENTITY` | PK | |
| `invoice_id` | `BIGINT` | **NOT NULL**, FK → `invoice_summary` `ON DELETE CASCADE` | Ties the line to its document |
| `po` | `TEXT` | null | Copied down so you can query lines by job without a join |
| `line_no` | `INTEGER` | null | `1`, `2`, … |
| `ord_qty` | `NUMERIC(12,3)` | null | Ordered quantity |
| `ship_qty` | `NUMERIC(12,3)` | null | Shipped quantity |
| `bo_qty` | `NUMERIC(12,3)` | null | Backordered quantity (often blank → null) |
| `product_code` | `TEXT` | null | `TZ-CZ2U4D216035-L1` |
| `description` | `TEXT` | null | |
| `price_list` | `NUMERIC(12,4)` | null | List price before discount |
| `discount_pct` | `NUMERIC(6,3)` | null | `61.500` |
| `net_price` | `NUMERIC(12,4)` | null | Unit net (`927.850`) — 3 decimals, hence scale 4 |
| `extension` | `NUMERIC(12,2)` | null | Line total |
| | | | **UNIQUE (invoice_id, line_no)** ← a line can't load twice |

### `oa_summary` — one row per order acknowledgement

Same shape as `invoice_summary`, with the fields acknowledgements actually have:

| Column | Type | Null? | Why |
|---|---|---|---|
| `oa_id` | `BIGINT IDENTITY` | PK | |
| `vendor` | `TEXT` | **NOT NULL** | |
| `order_no` | `TEXT` | **NOT NULL** | Artopex order number (`2606287`); the dedup key |
| `po` | `TEXT` | **NOT NULL** | `JF1069-05` |
| `account_no` | `TEXT` | null | |
| `salesman` | `TEXT` | null | |
| `order_date` | `DATE` | null | |
| `ship_date` | `DATE` | null | **OAs have a ship/delivery date; invoices don't** |
| `terms` | `TEXT` | null | |
| `reference` | `TEXT` | null | `ROVE` |
| `freight_terms` | `TEXT` | null | `PP CHARGE` |
| `fob` | `TEXT` | null | `USA DEST.` |
| `subtotal` | `NUMERIC(12,2)` | null | |
| `freight` | `NUMERIC(12,2)` | null | Dollar amount; not captured today (null) |
| `total` | `NUMERIC(12,2)` | **NOT NULL** | On Artopex OAs this prints only on the **last page** |
| `retail_extension_total` | `NUMERIC(12,2)` | null | The `21,759.00` list rollup, unique to OAs |
| `source_file` | `TEXT` | null | |
| `ingested_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | |
| | | | **UNIQUE (vendor, order_no)** |

### `oa_line_items` — one row per acknowledgement line

The tell that OAs and invoices genuinely need *separate* line tables: an OA line has a
**single `qty`** and `retail_price`/`retail_extension` columns, where an invoice line
has three quantities (`ord`/`ship`/`bo`) and a `price_list`. Different columns → different tables.

| Column | Type | Null? | Why |
|---|---|---|---|
| `oa_line_id` | `BIGINT IDENTITY` | PK | |
| `oa_id` | `BIGINT` | **NOT NULL**, FK → `oa_summary` `ON DELETE CASCADE` | |
| `po` | `TEXT` | null | |
| `line_no` | `INTEGER` | null | Runs `1–19` then jumps to `900–904` for accessories — `INTEGER` holds it fine |
| `qty` | `NUMERIC(12,3)` | null | OAs have one quantity column |
| `product_code` | `TEXT` | null | |
| `description` | `TEXT` | null | |
| `retail_price` | `NUMERIC(12,4)` | null | |
| `retail_extension` | `NUMERIC(12,2)` | null | |
| `discount_pct` | `NUMERIC(6,3)` | null | |
| `net_price` | `NUMERIC(12,4)` | null | |
| `extension` | `NUMERIC(12,2)` | null | |
| | | | **UNIQUE (oa_id, line_no)** |

### Why a summary + line-items split?

A document has two natural grains: header facts that occur once (invoice number,
dates, total) and line facts that repeat (each product). If you jam both into one
table, every header value gets copied onto every line — the invoice total repeated on
all 24 lines of a 24-line document. That isn't just wasteful, it's a correctness
hazard: the "total" now lives in 24 places that can disagree. Splitting stores each
fact exactly once at its natural grain. That's the whole point of the two-table pattern.

### Why the NOT NULL choices are what they are

Only four things are `NOT NULL`: `vendor`, `po`, `total`, and the **document number**
(`invoice_no` / `order_no`). Everything else is nullable on purpose — a messy parse
should still *land* (and get reviewed later) rather than get rejected wholesale.

The document number being `NOT NULL` is not optional, and here's the subtle reason:
our exactly-once guarantee is the `UNIQUE (vendor, invoice_no)` constraint, and
**PostgreSQL treats NULLs as distinct from each other.** If `invoice_no` could be null,
two documents that both parsed as null would both insert and the dedup would silently
fail. `NOT NULL` is what gives the uniqueness teeth.

### How exactly-once actually works

The scripts insert with `ON CONFLICT (vendor, invoice_no) DO NOTHING RETURNING …`.
If the document is already in the database, the insert affects zero rows, the
`RETURNING` comes back empty, and the script skips it. **Re-running a script over the
same folder is therefore safe** — already-loaded documents are simply skipped, never
duplicated. The line tables have their own `UNIQUE (parent_id, line_no)` so individual
lines can't double-load either. And `ON DELETE CASCADE` means deleting a summary row
cleans up its lines automatically, so you never orphan line items.

---

## The ingestion scripts

`scripts/ingest_invoices.py` and `scripts/ingest_oa.py` are deliberately twins — same
plumbing, differing only in the table names, the columns mapped, and the dedup key.
Keeping them separate (rather than one clever script with type-detection) means each
file reads top-to-bottom with no branching, which is easier to reason about while the
system is young.

Each script does, per PDF:

1. **Upload** the file to PDF.co (`/v1/file/upload`) → get a temporary URL.
2. **Start** an AI-parse job (`/v1/ai-invoice-parser`) → get a `jobId`.
3. **Poll** the job (`/v1/job/check`) every 2 seconds until `status == "success"`.
4. **Fetch** the finished JSON result from the job's result URL.
5. **Map** the nested JSON onto our columns and **clean** values.
6. **Insert** the summary row, then the line rows, with `ON CONFLICT DO NOTHING`.
7. **Commit per document.** Errors on one document roll back only that document and
   the loop continues — one malformed PDF in a batch of 200 won't halt the run.

Shared helpers worth knowing:

- **`g(node, "invoice.poNo")`** — walks a dotted path through the nested JSON and
  returns `None` for missing/empty values. This replaced the old flat key-matching
  code once we learned the AI parser returns deeply nested camelCase.
- **`to_num("2,410.00") → 2410.0`** — strips commas/currency junk; blank → `None`.
- **`to_date("2025/07/04") → "2025-07-04"`** — normalizes Artopex's slash dates to ISO
  so the `DATE` columns accept them; anything unrecognized → `None`.
- **The guard clause** — before touching the DB, the script checks the three `NOT NULL`
  fields are present. If not, it prints `SKIP (missing …)` and moves on, rather than
  letting Postgres throw mid-batch.

> **Cost note:** the AI Invoice Parser is a paid, per-document API call, and it's
> asynchronous (a few seconds each). Fine for the backlog, but don't loop it needlessly
> — while developing a new mapping, dump one document and work from that, don't re-parse
> the whole folder.

---

## The PDF.co JSON quirks

This section exists because these cost us real debugging time. If you're adding a
vendor or a document type, read this first.

**1. Use the AI Invoice Parser, not the Document Parser.** They are different products.
`/v1/pdf/documentparser` with the built-in "Generic Invoice" template produced garbage
on our acknowledgements — it crammed the order number into a field literally named
`bankaccount` and found zero line items. `/v1/ai-invoice-parser` is the one that
returns clean, structured output. If you see fields like `bankaccount` or
`templatename`, you're on the wrong endpoint.

**2. It's asynchronous.** The first call returns a `jobId`, *not* the result. You must
poll `/v1/job/check` until the job reports `success`, then fetch the result URL. A
synchronous read returns nothing.

**3. Responses can carry a UTF-8 BOM.** PDF.co sometimes prepends invisible bytes that
make Python's `.json()` throw `Unexpected UTF-8 BOM`. Every response is therefore
parsed through `_json_bom_safe()`, which decodes with `utf-8-sig` (BOM-aware) before
`json.loads`.

**4. The output is nested camelCase, and the parser thinks everything is an invoice.**
Structure is `vendor` / `customer` / `invoice` / `paymentDetails` / `lineItems` /
`customField`. Because it always assumes "invoice," some values land in surprising
places — most notably, on an **acknowledgement** the OA order number comes back under
`invoice.invoiceNo`, and the PO under `invoice.poNo`.

**5. Field names drift between document types.** The same concept is labeled differently
depending on the document, so the mapping is not identical across the two scripts.
Known examples:

| Concept | On invoices | On acknowledgements |
|---|---|---|
| Salesman | `customField.slm` | `customField.salesman` |
| Order date | `customField.orderDate` | (derived from header) |
| Ship date | — (invoices have none) | `invoice.deliveryDate` |
| Internal order no | `invoice.orderNo` (populated) | `invoice.orderNo` (empty; real one in `invoiceNo`) |

**6. `freight` is a term, not a dollar amount.** `customField.freight` returns `PREPAID`
or `PP CHARGE` — shipping *terms*, which we store in `freight_terms`. The numeric
`freight` dollar column stays null for now.

**7. Some fields simply aren't captured yet.** The OA's `retail_extension_total`
(`21,759.00`) and the freight dollar amount don't appear in the standard output. They're
left null. **The right place to fix this is PDF.co** — add them as custom fields in the
parser config — not by hacking the scripts.

---

## Setup from scratch

Requires Python 3.10+ and a reachable PostgreSQL 16 instance.

```bash
pip install -r requirements.txt        # requests, psycopg[binary], python-dotenv
```

> If you hit `ImportError: no pq wrapper available`, you installed bare `psycopg`.
> Install the binary bundle instead: `pip install "psycopg[binary]"` (quotes matter on
> PowerShell). It ships libpq inside the wheel.

Create a `.env` in the project root (copy `.env.example`):

```
DATABASE_URL=postgresql://coiuser:PASSWORD@localhost:5432/coibackend
PDFCO_API_KEY=your_pdfco_api_key
```

Create the tables by running `sql/schema.sql` in pgAdmin (or `psql -f`). **Run it while
connected as the role that will own the tables** — see the permissions note below.

### The `coiuser` permissions gotcha

A fresh non-superuser can log in and connect but has *no rights on the tables inside a
database*, so from its point of view the tables don't exist ("connects fine, sees
nothing"). Two grants are needed, run as the table owner (e.g. `postgres`) in the
`coibackend` database:

```sql
GRANT USAGE ON SCHEMA public TO coiuser;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO coiuser;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO coiuser;   -- IDENTITY needs this

-- And for tables created LATER (the step everyone forgets):
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO coiuser;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO coiuser;
```

The cleaner long-term option is to have `coiuser` **own** the tables by running
`schema.sql` while connected as `coiuser` — then no grants are needed at all.

Verify the connection and that your tables are visible:

```bash
python sql/test_connection.py
```

---

## Running it

Drop PDFs into the right folders, then run the matching script:

```bash
python scripts/ingest_invoices.py     # reads data/artopex/invoices/
python scripts/ingest_oa.py           # reads data/artopex/oas/
```

Expected output per document: `stored invoice 317045 (id=1, 1 lines)`.
Already-loaded documents print `already in DB, skipping`. Both are safe to re-run.

Spot-check in pgAdmin: one summary row with the right `po` and `total`, and the
matching count of line rows all carrying that summary's id (including OA accessory
lines `900–904` at zero).

---

## Security

- **Never commit `.env`** or real financial PDFs. Both are gitignored; keep it that way.
- **Rotate the PDF.co key if it's ever pasted anywhere** (chat, a shared file, a commit).
  Keys live in `.env` and are read via `os.environ`, never hardcoded.
- **Vendor banking / remit-to details are never written by this system.** The AI parser
  will happily extract a `bankingInformation` block; we ignore it. Any bank-change
  document is handled by a person and verified out-of-band. An automated pipeline that
  both reads PDFs and writes payment info is a fraud vector — we don't build that.

---

## Roadmap / future direction

**Phase 1 — parse & store (where we are).** Get Artopex invoices and acknowledgements
reliably into PostgreSQL, cleaned and de-duplicated. Prove it on the real backlog.

**Broaden the backlog.** Point the scripts at the full historical document set and load
it. Because ingestion is idempotent, we can run incrementally and re-run safely as more
documents surface. Watch PDF.co credit usage on large runs.

**Move the database to AWS.** Today the DB is local (`localhost:5432`). The natural next
step is **Amazon RDS for PostgreSQL** so the data lives in one durable, backed-up,
team-accessible place rather than on one machine. The application code barely changes —
it's a new `DATABASE_URL` pointing at the RDS endpoint (with SSL and a security group
locking access to known IPs). Ingestion could then run on a small EC2 box or a scheduled
job rather than someone's laptop. Keep secrets in AWS Secrets Manager rather than a
committed `.env`.

**Add more vendors.** Each new vendor gets a `data/<vendor>/` folder tree and, if its
layout differs, its own field mapping. The `vendor` column and per-vendor dedup keys
already make the tables multi-vendor-ready; expect the real work to be per-vendor JSON
mapping (see the quirks section).

**Cross-validate (the payoff Joe asked for).** Once invoices, acknowledgements, and
statements for the same PO are all in the DB, we can automatically match them —
did the invoice bill what the acknowledgement confirmed? do line quantities and prices
agree? — and surface only the mismatches for a human. The `po` key on every table is
what makes this a straightforward join rather than a research project.

**Feed E-Manage.** The end goal: push validated data into E-Manage via its API and help
clear the Open Balances backlog. The schema is deliberately shaped so a row is "clean
enough for E-Manage" by construction; the remaining work is confirming which E-Manage
fields the API expects and mapping our columns to them.
