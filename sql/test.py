import psycopg

DATABASE_URL = "postgresql://coiuser:joe@localhost:5432/coibackend"

with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_schema();")
        print("Connected to db / schema:", cur.fetchone())

        cur.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_name IN
                ('invoice_summary','invoice_line_items','oa_summary','oa_line_items')
            ORDER BY table_schema, table_name;
        """)
        rows = cur.fetchall()
        print("Found:", rows or "nowhere in this database")