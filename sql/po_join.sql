-- One row per normalized invoice or order acknowledgement. Add a PO predicate
-- for an individual job; this example intentionally contains no embedded data.
SELECT
    document_type,
    document_id,
    document_number,
    order_no,
    vendor,
    po,
    document_date,
    total,
    currency,
    document_status,
    review_status
FROM coi.po_document_overview
ORDER BY vendor, po, document_date, document_type, document_number;
