"""Read-only probe: recent WhatsApp test data from the (Railway) DB.

Run from the backend folder with the venv active:
    python _selftest\read_test_data.py
Writes _selftest\out.txt and also prints to the screen. SELECT-only; changes nothing.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

# Find the repo-root .env (SOKOLINK/.env) by walking up from this file.
here = Path(__file__).resolve()
env = next((p / ".env" for p in here.parents if (p / ".env").exists()), None)
if not env:
    sys.exit("Could not find .env above this script.")
raw = env.read_text(encoding="utf-8-sig")
m = re.search(r'^DATABASE_URL=(.+)$', raw, re.M)
if not m:
    sys.exit("DATABASE_URL not found in .env")
dsn = m.group(1).strip().strip('"').strip("'").replace("\r", "")
dsn = dsn.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

try:
    import psycopg  # psycopg 3, per config.py
except ModuleNotFoundError:
    sys.exit("psycopg not importable — is the project venv active?")

lines: list[str] = []
def out(s=""):
    print(s); lines.append(str(s))

with psycopg.connect(dsn) as con, con.cursor() as cur:
    out("=== ROW COUNTS ===")
    for t in ("wa_messages", "wa_conversations", "sellers", "products", "orders",
              "carts", "parsed_media", "payment_methods"):
        try:
            cur.execute(f"select count(*) from {t}")
            out(f"{t:18} {cur.fetchone()[0]}")
        except Exception as e:
            con.rollback(); out(f"{t:18} ERR {str(e)[:70]}")

    out("\n=== wa_messages, last 3 days (Africa/Nairobi) ===")
    cur.execute("""
        select id, from_number, coalesce(body,'(no text)'), media_count,
               processed_at is not null,
               to_char(created_at at time zone 'Africa/Nairobi','YYYY-MM-DD HH24:MI')
        from wa_messages
        where created_at >= now() - interval '3 days'
        order by created_at
    """)
    rows = cur.fetchall()
    out(f"({len(rows)} rows)")
    for r in rows:
        out(f"[{r[5]}] #{r[0]} from={r[1]} media={r[3]} processed={'Y' if r[4] else 'N'}")
        out(f"        body: {str(r[2])[:300]}")

    out("\n=== conversation states (most recently updated 30) ===")
    cur.execute("""
        select phone, state, seller_id, order_reference,
               to_char(updated_at at time zone 'Africa/Nairobi','MM-DD HH24:MI')
        from wa_conversations order by updated_at desc limit 30
    """)
    for r in cur.fetchall():
        out(f"{r[0]:15} state={r[1]:16} seller={r[2]} order={r[3]} @ {r[4]}")

    out("\n=== sellers ===")
    try:
        cur.execute("""select id, coalesce(shop_name,'?'), coalesce(slug,'?'),
                       coalesce(whatsapp_number,'?') from sellers order by id""")
        for r in cur.fetchall():
            out(f"#{r[0]} {r[1]!r} /{r[2]} wa={r[3]}")
    except Exception as e:
        con.rollback(); out(f"sellers detail ERR {str(e)[:70]}")

dest = here.parent / "out.txt"
dest.write_text("\n".join(lines), encoding="utf-8")
print(f"\n--- wrote {dest} ---")
