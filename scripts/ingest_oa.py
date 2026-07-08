"""
Mass-parse Artopex ORDER ACKNOWLEDGEMENTS via PDF.co AI Invoice Parser
-> oa_summary + oa_line_items.
"""
import os
import re
import time
import json
import requests
import psycopg

# ─── CONFIG ──────────────────────────────────────────────────────────────
OA_FOLDER     = r"C:\Users\aidan\VSCode Projects\coibackend\coibackend\data\artopex\oas"
DATABASE_URL  = "postgresql://coiuser:joe@localhost:5432/coibackend"
PDFCO_API_KEY = "INSERT"   

PDFCO_BASE = "https://api.pdf.co/v1"


# ─── SMALL HELPERS ───────────────────────────────────────────────────────
def to_num(v):
    """'2,094.00' -> 2094.0 ; blank -> None"""
    s = re.sub(r"[^\d.\-]", "", str(v if v is not None else ""))
    return float(s) if s not in ("", "-", ".") else None

def to_date(v):
    """'2026/05/07' -> '2026-05-07' ; unrecognized -> None"""
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


# ─── STORE ONE ACKNOWLEDGEMENT ───────────────────────────────────────────
def store_acknowledgement(parsed, source_file, conn):
    order_no = g(parsed, "invoice.invoiceNo")     # AI parser puts OA order no here
    po       = g(parsed, "invoice.poNo")
    total    = to_num(g(parsed, "paymentDetails.total"))

    # Enforce the three NOT NULL columns before touching the DB.
    if not (order_no and po and total is not None):
        print(f"   SKIP (missing order_no / po / total): {source_file}")
        return

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO oa_summary
                (vendor, order_no, po, account_no, salesman,
                 order_date, ship_date, terms, reference, freight_terms, fob,
                 subtotal, freight, total, retail_extension_total, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vendor, order_no) DO NOTHING
            RETURNING oa_id;
        """, ("ARTOPEX", order_no, po,
              g(parsed, "customField.accountNo"),
              g(parsed, "customField.salesman"),
              to_date(g(parsed, "invoice.invoiceDate")),
              to_date(g(parsed, "invoice.deliveryDate")),
              g(parsed, "paymentDetails.paymentTerms"),
              g(parsed, "customField.referenceNo"),
              g(parsed, "customField.freight"),
              g(parsed, "customField.fob"),
              to_num(g(parsed, "paymentDetails.subtotal")),
              None,                                  # freight amount: not a field in this output
              total,
              None,                                  # retail_extension_total: not present
              source_file))

        row = cur.fetchone()
        if row is None:
            print(f"   already in DB, skipping: {source_file}")
            return
        oa_id = row[0]

        for li in parsed.get("lineItems", []):
            cur.execute("""
                INSERT INTO oa_line_items
                    (oa_id, po, line_no, qty, product_code, description,
                     retail_price, retail_extension, discount_pct, net_price, extension)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (oa_id, line_no) DO NOTHING;
            """, (oa_id, po,
                  to_num(li.get("line")),
                  to_num(li.get("qty")),
                  li.get("productCode"), li.get("description"),
                  to_num(li.get("retailPrice")), to_num(li.get("retailExtension")),
                  to_num(li.get("discount")),
                  to_num(li.get("netPrice")), to_num(li.get("extension"))))

    conn.commit()
    print(f"   stored acknowledgement {order_no} (id={oa_id}, {len(parsed.get('lineItems', []))} lines)")


# ─── MAIN ────────────────────────────────────────────────────────────────
def main():
    if not PDFCO_API_KEY:
        print("PDFCO_API_KEY is not set (put it in your .env).")
        return

    pdfs = [f for f in os.listdir(OA_FOLDER) if f.lower().endswith(".pdf")]
    if not pdfs:
        print("No PDFs found in", OA_FOLDER)
        return

    session = requests.Session()
    session.headers.update({"x-api-key": PDFCO_API_KEY})

    with psycopg.connect(DATABASE_URL) as conn:
        for name in sorted(pdfs):
            path = os.path.join(OA_FOLDER, name)
            print(f"Processing {name} ...")
            try:
                file_url = pdfco_upload(path, session)
                parsed   = pdfco_parse(file_url, session)
                store_acknowledgement(parsed, name, conn)
            except Exception as e:
                conn.rollback()
                print(f"   ERROR on {name}: {e}")

    print("Done.")


if __name__ == "__main__":
    main()