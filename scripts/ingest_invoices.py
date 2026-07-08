"""
Mass-parse Artopex INVOICES via PDF.co AI Invoice Parser
-> invoice_summary + invoice_line_items.
"""
import os
import re
import time
import json
import requests
import psycopg

# ─── CONFIG ──────────────────────────────────────────────────────────────
INVOICES_FOLDER = r"C:\Users\aidan\VSCode Projects\coibackend\coibackend\data\artopex\invoices"
DATABASE_URL    = "postgresql://coiuser:joe@localhost:5432/coibackend"
PDFCO_API_KEY = "INSERT" 

PDFCO_BASE = "https://api.pdf.co/v1"


# ─── SMALL HELPERS ───────────────────────────────────────────────────────
def to_num(v):
    """'2,410.00' -> 2410.0 ; blank -> None"""
    s = re.sub(r"[^\d.\-]", "", str(v if v is not None else ""))
    return float(s) if s not in ("", "-", ".") else None

def to_date(v):
    """'2025/07/04' -> '2025-07-04' ; unrecognized -> None"""
    s = str(v or "").strip().replace("/", "-")
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None

def g(node, path, default=None):
    """Walk a dotted path through nested dicts: g(parsed, 'invoice.poNo')."""
    cur = node
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur not in ("", None) else default


# ─── PDF.CO ──────────────────────────────────────────────────────────────
def _json_bom_safe(resp):
    resp.raise_for_status()
    return json.loads(resp.content.decode("utf-8-sig"))

def pdfco_upload(path, session):
    with open(path, "rb") as fh:
        r = session.post(f"{PDFCO_BASE}/file/upload",
                         files={"file": (os.path.basename(path), fh)}, timeout=120)
    data = _json_bom_safe(r)
    url = data.get("url")
    if not url:
        raise RuntimeError(f"upload failed: {data}")
    return url

def pdfco_parse(file_url, session):
    """AI Invoice Parser is async: start a job, poll /job/check, then fetch the result."""
    start = _json_bom_safe(session.post(f"{PDFCO_BASE}/ai-invoice-parser",
                                        json={"url": file_url}, timeout=60))
    if start.get("error"):
        raise RuntimeError(f"parse start failed: {start}")
    job_id = start.get("jobId")
    if not job_id:
        raise RuntimeError(f"no jobId returned: {start}")

    result_url = start.get("url")
    deadline = time.time() + 180
    while time.time() < deadline:
        check = _json_bom_safe(session.post(f"{PDFCO_BASE}/job/check",
                                            json={"jobId": job_id}, timeout=60))
        status = check.get("status")
        result_url = check.get("url") or result_url
        if status == "success":
            break
        if status in ("failed", "aborted"):
            raise RuntimeError(f"parse job {status}: {check}")
        time.sleep(2)
    else:
        raise RuntimeError(f"parse job timed out (job {job_id})")

    if not result_url:
        raise RuntimeError(f"job done but no result url: {check}")
    return _json_bom_safe(session.get(result_url, timeout=120))


# ─── STORE ONE INVOICE ───────────────────────────────────────────────────
def store_invoice(parsed, source_file, conn):
    invoice_no = g(parsed, "invoice.invoiceNo")
    po         = g(parsed, "invoice.poNo")
    total      = to_num(g(parsed, "paymentDetails.total"))

    # Enforce the three NOT NULL columns before touching the DB.
    if not (invoice_no and po and total is not None):
        print(f"   SKIP (missing invoice_no / po / total): {source_file}")
        return

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO invoice_summary
                (vendor, invoice_no, order_no, po, account_no, salesman,
                 invoice_date, order_date, terms, freight_terms,
                 subtotal, freight, less_prepaid_deposit, total, currency, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vendor, invoice_no) DO NOTHING
            RETURNING invoice_id;
        """, ("ARTOPEX", invoice_no, g(parsed, "invoice.orderNo"), po,
              g(parsed, "customField.accountNo"), g(parsed, "customField.slm", "salesman"),
              to_date(g(parsed, "invoice.invoiceDate")),
              to_date(g(parsed, "customField.orderDate")),
              g(parsed, "paymentDetails.paymentTerms"),
              g(parsed, "customField.freight"),
              to_num(g(parsed, "paymentDetails.subtotal")),
              None,                                  # freight amount: not broken out in this output
              to_num(g(parsed, "customField.lessPrepaidDeposit")),
              total,
              g(parsed, "customField.currency") or "USD",
              source_file))

        row = cur.fetchone()
        if row is None:
            print(f"   already in DB, skipping: {source_file}")
            return
        invoice_id = row[0]

        for li in parsed.get("lineItems", []):
            cur.execute("""
                INSERT INTO invoice_line_items
                    (invoice_id, po, line_no, ord_qty, ship_qty, bo_qty,
                     product_code, description, price_list, discount_pct,
                     net_price, extension)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (invoice_id, line_no) DO NOTHING;
            """, (invoice_id, po,
                  to_num(li.get("lineNo")),
                  to_num(li.get("ordQty")), to_num(li.get("shipQty")), to_num(li.get("boQty")),
                  li.get("productCode"), li.get("description"),
                  to_num(li.get("priceList")), to_num(li.get("discounts")),
                  to_num(li.get("netPrice")), to_num(li.get("extension"))))

    conn.commit()
    print(f"   stored invoice {invoice_no} (id={invoice_id}, {len(parsed.get('lineItems', []))} lines)")


# ─── MAIN ────────────────────────────────────────────────────────────────
def main():
    if not PDFCO_API_KEY:
        print("PDFCO_API_KEY is not set (put it in your .env).")
        return

    pdfs = [f for f in os.listdir(INVOICES_FOLDER) if f.lower().endswith(".pdf")]
    if not pdfs:
        print("No PDFs found in", INVOICES_FOLDER)
        return

    session = requests.Session()
    session.headers.update({"x-api-key": PDFCO_API_KEY})

    with psycopg.connect(DATABASE_URL) as conn:
        for name in sorted(pdfs):
            path = os.path.join(INVOICES_FOLDER, name)
            print(f"Processing {name} ...")
            try:
                file_url = pdfco_upload(path, session)
                parsed   = pdfco_parse(file_url, session)
                store_invoice(parsed, name, conn)
            except Exception as e:
                conn.rollback()
                print(f"   ERROR on {name}: {e}")

    print("Done.")


if __name__ == "__main__":
    main()