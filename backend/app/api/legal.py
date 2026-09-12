"""
Public legal pages — the privacy policy and data-deletion notice.

    GET /privacy         the privacy policy (also the app's Privacy Policy URL)
    GET /data-deletion   how a person asks for their data to be deleted

WHY THESE LIVE IN THE APP, NOT A STATIC HOST. Meta requires a live Privacy
Policy URL before a WhatsApp app can be published, and the honest place for it
is the same domain the service runs on — a buyer who taps through can see it is
really ours. They are plain, self-contained HTML (no template, no JS, no
external fetch) so they render the same everywhere and cannot break on a missing
asset. Edit the copy here; the contact address is the one thing to keep current.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

#: The business's contact address for privacy questions and data requests.
#: KEEP THIS CURRENT — it is the address people are told to write to.
PRIVACY_CONTACT = "hello@biasharamall.com"

#: Last time the policy text below was changed. Update it when you edit the copy.
LAST_UPDATED = "8 September 2026"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — BiasharaMall</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #1a1a1a; background: #fff; margin: 0; padding: 2rem 1.25rem 4rem;
  }}
  main {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 1.7rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: 1.15rem; margin: 2rem 0 .5rem; }}
  .meta {{ color: #666; font-size: .9rem; margin-bottom: 2rem; }}
  a {{ color: #0b7d3e; }}
  ul {{ padding-left: 1.25rem; }}
  li {{ margin: .3rem 0; }}
  footer {{ margin-top: 3rem; color: #888; font-size: .85rem; }}
</style>
</head>
<body>
<main>
{body}
<footer>BiasharaMall — commerce on WhatsApp for Kenyan businesses.</footer>
</main>
</body>
</html>"""


_PRIVACY_BODY = """
<h1>Privacy Policy</h1>
<p class="meta">Last updated: {updated}</p>

<p>BiasharaMall ("we", "us") provides a service that lets Kenyan businesses sell
to their customers over WhatsApp. This policy explains what information we
handle when you use BiasharaMall as a buyer or as a seller, why we handle it,
and the choices you have. By using the service you agree to this policy.</p>

<h2>Information we handle</h2>
<ul>
  <li><strong>Your WhatsApp phone number and name</strong>, as WhatsApp provides
      them when you message a shop.</li>
  <li><strong>Your messages to the shop</strong> — what you ask for, what you
      order, and the choices you make while ordering.</li>
  <li><strong>Order details</strong> — the items, quantities, and any delivery
      address or note you give so an order can be fulfilled.</li>
  <li><strong>Payment references</strong> — the M-Pesa confirmation code or
      transaction reference you share so a seller can confirm payment. We do
      <em>not</em> ask for or store PINs, card numbers, or bank passwords.</li>
  <li><strong>Seller account details</strong> — for sellers, your shop name,
      product listings, and the payment number (till, Pochi, or paybill) money
      should be sent to.</li>
</ul>

<h2>Why we handle it</h2>
<ul>
  <li>To show you a shop's products and take your order.</li>
  <li>To pass your order and delivery details to the seller so they can fulfil
      it, and to confirm payment.</li>
  <li>To send you order and payment updates on WhatsApp.</li>
  <li>To keep the service working, prevent abuse, and meet our legal
      obligations.</li>
</ul>

<h2>Who we share it with</h2>
<ul>
  <li><strong>The seller</strong> you are ordering from receives your name,
      contact number, order, and delivery details so they can fulfil and
      confirm it.</li>
  <li><strong>WhatsApp / Meta</strong> carries the messages between you and the
      shop, under their own terms.</li>
  <li><strong>Payment providers</strong> (for example Safaricom M-Pesa) process
      payments directly between you and the seller; we handle only the reference
      you share, not the payment itself.</li>
  <li>We do <strong>not</strong> sell your personal information to anyone.</li>
</ul>

<h2>How long we keep it</h2>
<p>We keep order and account information for as long as needed to run the
service, resolve disputes, and meet legal and accounting requirements, and then
delete or anonymise it.</p>

<h2>Your choices and rights</h2>
<p>You can ask us what information we hold about you, to correct it, or to
delete it (see our <a href="/data-deletion">data deletion</a> page). You can
stop using the service at any time by no longer messaging the shop.</p>

<h2>Children</h2>
<p>BiasharaMall is intended for use by adults running or buying from
businesses, and is not directed at children.</p>

<h2>Changes</h2>
<p>We may update this policy; the "last updated" date above shows when. Material
changes will be reflected here.</p>

<h2>Contact</h2>
<p>Questions about this policy or your information? Write to us at
<a href="mailto:{contact}">{contact}</a>.</p>
"""


_DATA_DELETION_BODY = """
<h1>Data Deletion</h1>
<p class="meta">Last updated: {updated}</p>

<p>You can ask us to delete the personal information BiasharaMall holds about
you at any time.</p>

<h2>How to request deletion</h2>
<p>Send an email to <a href="mailto:{contact}">{contact}</a> from the address or
with the WhatsApp number linked to your use of BiasharaMall, with the subject
line <strong>"Delete my data"</strong>. Tell us whether you used BiasharaMall as
a buyer, a seller, or both, so we can find your records.</p>

<h2>What happens next</h2>
<ul>
  <li>We confirm the request and verify it came from you.</li>
  <li>We delete or anonymise the personal information we hold about you,
      normally within 30 days.</li>
  <li>Some information may be kept where the law requires it (for example
      records needed for tax or to resolve a dispute); we delete it once that
      requirement ends.</li>
</ul>

<h2>Note on payments and WhatsApp</h2>
<p>Payments are handled by your payment provider (for example Safaricom M-Pesa)
and messages are carried by WhatsApp / Meta; requests to delete data held by
those companies must be made to them directly.</p>

<h2>Contact</h2>
<p><a href="mailto:{contact}">{contact}</a></p>
"""


@router.get("/privacy", response_class=HTMLResponse)
def privacy_policy() -> HTMLResponse:
    """The public privacy policy — also the app's registered Privacy Policy URL."""
    body = _PRIVACY_BODY.format(updated=LAST_UPDATED, contact=PRIVACY_CONTACT)
    return HTMLResponse(_PAGE.format(title="Privacy Policy", body=body))


@router.get("/data-deletion", response_class=HTMLResponse)
def data_deletion() -> HTMLResponse:
    """How a person asks for their BiasharaMall data to be deleted."""
    body = _DATA_DELETION_BODY.format(updated=LAST_UPDATED, contact=PRIVACY_CONTACT)
    return HTMLResponse(_PAGE.format(title="Data Deletion", body=body))
