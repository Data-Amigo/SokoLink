"""Read-only Daraja/STK diagnostic. Run with the venv active:
    python _selftest\daraja_diag.py
Prints booleans and Daraja's own error text — never your secrets. SELECT-only.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

here = Path(__file__).resolve()
env = next((p / ".env" for p in here.parents if (p / ".env").exists()), None)
raw = env.read_text(encoding="utf-8-sig")
def val(key):
    m = re.search(rf'^{key}=(.+)$', raw, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else "(not set)"
dsn = val("DATABASE_URL").replace("postgresql+psycopg://","postgresql://").replace("postgresql+psycopg2://","postgresql://")

import psycopg
out=[]
def p(s=""):
    print(s); out.append(str(s))

p("=== LOCAL .env payment config (NOTE: Railway's deployed values are what actually run) ===")
for k in ("APP_BASE_URL","NEXT_PUBLIC_APP_URL","MPESA_ENVIRONMENT","DARAJA_ENVIRONMENT","MPESA_CALLBACK_URL"):
    p(f"  {k} = {val(k)}")
p("  -> STK callback is built as <APP_BASE_URL>/payments/mpesa/callback and MUST be public (your Railway https URL), not localhost.")

with psycopg.connect(dsn) as con, con.cursor() as cur:
    p("\n=== payment_methods (can_stk = has real Daraja creds) ===")
    cur.execute("""
        select s.display_name, s.slug, pm.kind, pm.number,
               pm.stk_shortcode is not null as has_shortcode,
               pm.consumer_key_enc is not null as has_key,
               pm.consumer_secret_enc is not null as has_secret,
               pm.passkey_enc is not null as has_passkey
        from payment_methods pm join sellers s on s.id = pm.seller_id
        order by pm.id
    """)
    rows = cur.fetchall()
    if not rows:
        p("  (none) -> no seller has set up any payment method yet")
    for r in rows:
        can_stk = r[2] in ("till","paybill") and r[7]
        p(f"  {r[0]!r} /{r[1]}: kind={r[2]} number={r[3]} | shortcode={r[4]} key={r[5]} secret={r[6]} passkey={r[7]} => CAN_STK={can_stk}")
    p("  -> If CAN_STK is False for your test shop, checkout ALWAYS shows the manual 'send code' path; STK is never attempted.")
    p("  -> Daraja API creds are entered in the WEB workspace payment settings, not in the chat.")

    p("\n=== recent Payment attempts (STK) ===")
    try:
        cur.execute("""
            select p.id, o.reference,
                   p.checkout_request_id is not null as pushed,
                   p.result_code, p.result_desc,
                   p.confirmed_at is not null as confirmed,
                   to_char(p.created_at at time zone 'Africa/Nairobi','MM-DD HH24:MI')
            from payments p left join orders o on o.id = p.order_id
            order by p.created_at desc limit 15
        """)
        prows = cur.fetchall()
        if not prows:
            p("  (none) -> no STK push has ever been recorded. Either CAN_STK is False, or start_stk_payment never ran.")
        for r in prows:
            p(f"  #{r[0]} order={r[1]} pushed={r[2]} result_code={r[3]} confirmed={r[5]} @ {r[6]}")
            if r[4]: p(f"        Daraja said: {r[4]}")
    except Exception as e:
        con.rollback(); p(f"  payments query ERR {str(e)[:80]}")

    p("\n=== orders by status (last 3 days) ===")
    cur.execute("""select status, count(*) from orders
        where created_at >= now() - interval '3 days' group by status order by 2 desc""")
    for r in cur.fetchall():
        p(f"  {r[0]:22} {r[1]}")

dest = here.parent / "daraja_out.txt"
dest.write_text("\n".join(out), encoding="utf-8")
print(f"\n--- wrote {dest} ---")
