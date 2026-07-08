-- ============================================================
-- INVOICES
-- ============================================================
CREATE TABLE invoice_summary (
    invoice_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor                TEXT           NOT NULL,
    invoice_no            TEXT           NOT NULL,   -- 317045  (NOT NULL: dedup key)
    order_no              TEXT,                      -- Artopex sales order 2507177
    po                    TEXT           NOT NULL,   -- JF823-09 (cross-document key)
    account_no            TEXT,
    salesman              TEXT,
    invoice_date          DATE,
    order_date            DATE,
    terms                 TEXT,
    freight_terms         TEXT,                      -- PREPAID
    subtotal              NUMERIC(12,2),
    freight               NUMERIC(12,2),
    misc                  NUMERIC(12,2),
    tax                   NUMERIC(12,2),
    less_prepaid_deposit  NUMERIC(12,2),
    total                 NUMERIC(12,2)  NOT NULL,
    currency              TEXT           NOT NULL DEFAULT 'USD',
    source_file           TEXT,
    ingested_at           TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT uq_invoice UNIQUE (vendor, invoice_no)   -- one invoice, exactly once
);

CREATE INDEX ix_invoice_summary_po ON invoice_summary (po);


CREATE TABLE invoice_line_items (
    invoice_line_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id       BIGINT   NOT NULL
                     REFERENCES invoice_summary (invoice_id) ON DELETE CASCADE,
    po               TEXT,                          -- copied down for join-free queries
    line_no          INTEGER,
    ord_qty          NUMERIC(12,3),
    ship_qty         NUMERIC(12,3),
    bo_qty           NUMERIC(12,3),
    product_code     TEXT,
    description      TEXT,
    price_list       NUMERIC(12,4),
    discount_pct     NUMERIC(6,3),                  -- 61.500
    net_price        NUMERIC(12,4),                 -- 927.850  (3 decimals -> scale 4)
    extension        NUMERIC(12,2),
    CONSTRAINT uq_invoice_line UNIQUE (invoice_id, line_no)
);

CREATE INDEX ix_invoice_line_po ON invoice_line_items (po);


-- ============================================================
-- ORDER ACKNOWLEDGEMENTS
-- ============================================================
CREATE TABLE oa_summary (
    oa_id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor                  TEXT           NOT NULL,
    order_no                TEXT           NOT NULL,   -- 2606287 (NOT NULL: dedup key)
    po                      TEXT           NOT NULL,   -- JF1069-05
    account_no              TEXT,
    salesman                TEXT,
    order_date              DATE,
    ship_date               DATE,                      -- OA has this; invoices don't
    terms                   TEXT,
    reference               TEXT,                      -- ROVE
    freight_terms           TEXT,                      -- PP CHARGE
    fob                     TEXT,                      -- USA DEST.
    subtotal                NUMERIC(12,2),
    freight                 NUMERIC(12,2),
    total                   NUMERIC(12,2)  NOT NULL,
    retail_extension_total  NUMERIC(12,2),             -- 21,759.00 list rollup
    source_file             TEXT,
    ingested_at             TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT uq_oa UNIQUE (vendor, order_no)
);

CREATE INDEX ix_oa_summary_po ON oa_summary (po);


CREATE TABLE oa_line_items (
    oa_line_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    oa_id          BIGINT   NOT NULL
                   REFERENCES oa_summary (oa_id) ON DELETE CASCADE,
    po             TEXT,
    line_no        INTEGER,                          -- runs 1-19 then 900-904; INTEGER is fine
    qty            NUMERIC(12,3),                    -- OA has a single qty column
    product_code   TEXT,
    description    TEXT,
    retail_price   NUMERIC(12,4),
    retail_extension NUMERIC(12,2),
    discount_pct   NUMERIC(6,3),
    net_price      NUMERIC(12,4),
    extension      NUMERIC(12,2),
    CONSTRAINT uq_oa_line UNIQUE (oa_id, line_no)
);

CREATE INDEX ix_oa_line_po ON oa_line_items (po);