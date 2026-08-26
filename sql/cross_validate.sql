-- Reconciliation is pre-aggregated in the view so repeated products or split
-- invoices cannot multiply rows. Add `WHERE vendor = ... AND po = ...` when
-- investigating one purchase order.
SELECT
    vendor,
    po,
    product_code,
    currency,
    acknowledged_qty,
    invoiced_qty,
    quantity_variance,
    acknowledged_extension,
    invoiced_extension,
    extension_variance,
    reconciliation_status
FROM coi.po_product_reconciliation
ORDER BY vendor, po, product_code, currency;
