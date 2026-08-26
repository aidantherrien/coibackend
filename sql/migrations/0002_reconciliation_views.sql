-- Parameter-free read models for portal search and safe PO reconciliation.
-- The migration runner wraps this whole file in one transaction and records its
-- checksum in coi.schema_migrations.

CREATE VIEW coi.po_document_overview AS
SELECT
    'invoice'::TEXT                AS document_type,
    inv.document_id,
    inv.invoice_id                 AS summary_id,
    inv.vendor,
    inv.invoice_no                 AS document_number,
    inv.order_no,
    inv.po,
    inv.invoice_date               AS document_date,
    inv.total,
    inv.currency,
    doc.status                     AS document_status,
    doc.review_status
FROM coi.invoice_summary AS inv
JOIN coi.documents AS doc ON doc.document_id = inv.document_id

UNION ALL

SELECT
    'order_acknowledgement'::TEXT  AS document_type,
    oa.document_id,
    oa.oa_id                       AS summary_id,
    oa.vendor,
    oa.order_no                    AS document_number,
    oa.order_no,
    oa.po,
    oa.order_date                  AS document_date,
    oa.total,
    oa.currency,
    doc.status                     AS document_status,
    doc.review_status
FROM coi.oa_summary AS oa
JOIN coi.documents AS doc ON doc.document_id = oa.document_id;

COMMENT ON VIEW coi.po_document_overview IS
    'All invoices and OAs in one searchable shape; callers filter by PO, vendor, or document number.';

-- Aggregate each side before joining. A direct line-to-line join on PO and
-- product_code multiplies rows when a PO is split across documents or a product
-- appears more than once, producing incorrect financial totals.
CREATE VIEW coi.po_product_reconciliation AS
WITH acknowledged AS (
    SELECT
        oa.vendor,
        oa.po,
        line.product_code,
        oa.currency,
        count(DISTINCT oa.oa_id)       AS acknowledgement_count,
        count(*)                       AS acknowledgement_line_count,
        count(line.qty)                AS acknowledged_qty_value_count,
        count(line.net_price)          AS acknowledged_price_value_count,
        count(line.extension)          AS acknowledged_extension_value_count,
        sum(line.qty)                  AS acknowledged_qty,
        min(line.net_price)            AS acknowledged_min_net_price,
        max(line.net_price)            AS acknowledged_max_net_price,
        sum(line.extension)            AS acknowledged_extension
    FROM coi.oa_summary AS oa
    JOIN coi.oa_line_items AS line ON line.oa_id = oa.oa_id
    WHERE line.product_code IS NOT NULL
    GROUP BY oa.vendor, oa.po, line.product_code, oa.currency
),
invoiced AS (
    SELECT
        inv.vendor,
        inv.po,
        line.product_code,
        inv.currency,
        count(DISTINCT inv.invoice_id) AS invoice_count,
        count(*)                       AS invoice_line_count,
        count(line.ship_qty)           AS invoiced_qty_value_count,
        count(line.net_price)           AS invoiced_price_value_count,
        count(line.extension)           AS invoiced_extension_value_count,
        sum(line.ship_qty)             AS invoiced_qty,
        min(line.net_price)            AS invoiced_min_net_price,
        max(line.net_price)            AS invoiced_max_net_price,
        sum(line.extension)            AS invoiced_extension
    FROM coi.invoice_summary AS inv
    JOIN coi.invoice_line_items AS line ON line.invoice_id = inv.invoice_id
    WHERE line.product_code IS NOT NULL
    GROUP BY inv.vendor, inv.po, line.product_code, inv.currency
)
SELECT
    COALESCE(ack.vendor, inv.vendor)                 AS vendor,
    COALESCE(ack.po, inv.po)                         AS po,
    COALESCE(ack.product_code, inv.product_code)     AS product_code,
    COALESCE(ack.currency, inv.currency)             AS currency,
    ack.acknowledgement_count,
    ack.acknowledgement_line_count,
    ack.acknowledged_qty_value_count,
    ack.acknowledged_price_value_count,
    ack.acknowledged_extension_value_count,
    inv.invoice_count,
    inv.invoice_line_count,
    inv.invoiced_qty_value_count,
    inv.invoiced_price_value_count,
    inv.invoiced_extension_value_count,
    ack.acknowledged_qty,
    inv.invoiced_qty,
    inv.invoiced_qty - ack.acknowledged_qty          AS quantity_variance,
    ack.acknowledged_min_net_price,
    ack.acknowledged_max_net_price,
    inv.invoiced_min_net_price,
    inv.invoiced_max_net_price,
    ack.acknowledged_extension,
    inv.invoiced_extension,
    inv.invoiced_extension - ack.acknowledged_extension
                                                     AS extension_variance,
    CASE
        WHEN ack.po IS NULL THEN 'missing_acknowledgement'
        WHEN inv.po IS NULL THEN 'missing_invoice'
        WHEN ack.acknowledged_qty_value_count <> ack.acknowledgement_line_count
          OR ack.acknowledged_price_value_count <> ack.acknowledgement_line_count
          OR ack.acknowledged_extension_value_count <> ack.acknowledgement_line_count
          OR inv.invoiced_qty_value_count <> inv.invoice_line_count
          OR inv.invoiced_price_value_count <> inv.invoice_line_count
          OR inv.invoiced_extension_value_count <> inv.invoice_line_count
            THEN 'incomplete_data'
        WHEN ack.acknowledged_qty IS DISTINCT FROM inv.invoiced_qty
            THEN 'quantity_mismatch'
        WHEN ack.acknowledged_min_net_price
             IS DISTINCT FROM inv.invoiced_min_net_price
          OR ack.acknowledged_max_net_price
             IS DISTINCT FROM inv.invoiced_max_net_price
            THEN 'price_mismatch'
        WHEN ack.acknowledged_extension
             IS DISTINCT FROM inv.invoiced_extension
            THEN 'extension_mismatch'
        ELSE 'matched'
    END                                              AS reconciliation_status
FROM acknowledged AS ack
FULL OUTER JOIN invoiced AS inv
    ON inv.vendor = ack.vendor
   AND inv.po = ack.po
   AND inv.product_code = ack.product_code
   AND inv.currency = ack.currency;

COMMENT ON VIEW coi.po_product_reconciliation IS
    'Vendor/PO/product rollup that avoids many-to-many line multiplication; NULL product codes are intentionally excluded.';
