import psycopg

# Same URL you'll put in .env as DATABASE_URL
DATABASE_URL = "postgresql://coiuser:joe@localhost:5432/coibackend"

try:
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            print("Connected:", cur.fetchone()[0])

            # Confirm the four tables exist yet (nice sanity check post-schema)
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN
                    ('invoice_summary','invoice_line_items','oa_summary','oa_line_items')
                ORDER BY table_name;
            """)
            found = [r[0] for r in cur.fetchall()]
            print("Tables found:", found or "none yet")
except Exception as e:
    print("Connection FAILED:", e)