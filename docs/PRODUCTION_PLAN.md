# Biashara Mall — Production Plan

> **Rewritten 2026-08-22.** The direction changed: we no longer pull a catalog
> from social media. Sellers push it to us over WhatsApp.
>
> The reasoning behind the change is in
> [`BUILD_LOG.md`](BUILD_LOG.md). The previous plan — analytics-first, with
> WhatsApp as a late channel — is superseded in full. So is
> [`BUILD_ORDER.md`](BUILD_ORDER.md).

---

## 1. The direction

**The old bet:** a seller connects a TikTok account, we scrape their feed, an AI
reads a price off each video, they review the drafts, a storefront appears.

**Why it was the wrong bet.** Every word of that is work the seller has to be
talked into before they see anything. Connect an account. Prove you own it.
Wait for a scrape. Review what the AI guessed. Five steps of friction, paid for
by us per scrape, before the seller has a single thing to sell.

**What we actually observed:** sellers already run catalogs inside WhatsApp
groups. The photo and the caption already exist, already written by them, in the
place they already work. We were paying a scraper to reconstruct — badly — a
thing the seller would hand us directly if we simply asked.

So we stopped pulling and started receiving.

```
   OLD                                  NEW
   ───                                  ───
   Connect account                      Forward a post to the bot
   Prove ownership (bio code)                    │
   Wait for Apify scrape                         ▼
   AI guesses which posts are products    "Added! Your store: …"
   Seller reviews drafts
   Storefront appears                     One step. Zero AI guessing
                                          about intent — a forwarded
   5 steps · we pay per scrape            post IS the intent.
```

**The intent problem disappears.** Scraping forced us to guess whether a video
was a product at all. A forwarded post carries that answer: the seller chose to
send it. The AI's job shrinks from *"is this a product, and what is it?"* to
*"read this product card"* — a far easier task, on a far better image, because a
catalog post is composed to be read.

---

## 2. The MVP, in three steps

### Step 1 — Forward-to-catalog

```
Seller forwards photo + caption ──▶ WhatsApp webhook
                                          │
                                    download media
                                          │
                                    vision parse (Gemini)
                                          │
                                    Product (draft)
                                          │
              "Added! View your store: biasharamall.com/shop/zuma"
```

The seller never opens a browser to add stock. They forward; we reply with a
link. That is the entire onboarding.

### Step 2 — The in-WhatsApp storefront

The buyer taps the link in a chat, channel or status. The **WhatsApp in-app
browser** opens our server-rendered pages — not WhatsApp Flows, so there is no
Meta approval dependency and it debugs in an ordinary browser.

Browse → pick size/colour → add to cart → checkout.

### Step 3 — The M-Pesa handshake

Two paths, chosen by the seller's registered payment method. The money always
goes buyer → seller; we are never in the middle.

```
Checkout form ──▶ phone + delivery + WhatsApp opt-in ──▶ Order (pending)
                          │
        ┌─────────────────┴──────────────────┐
        │                                    │
  Till/Paybill WITH                    Pochi la Biashara,
  Daraja credentials                   or a number with no credentials
        │                                    │
   STK push ──▶ handset               "Pay to Pochi 07XX XXX XXX"
        │                                    │
   seller's callback                  buyer pays in M-Pesa,
   (the ONLY truth)                   enters the code
        │                                    │
        │                            Order (awaiting_confirmation)
        │                                    │
        │                            seller confirms  ← only the seller
        │                                    │
        └──────────────┬─────────────────────┘
                       │
                 Order (paid) ──▶ receipt on page (always)
                              ──▶ receipt on WhatsApp (opt-in)
                              ──▶ order alert to seller (always)
```

---

## 3. Decisions taken, and what they close off

Four questions were answered on 2026-08-22. Each closes a design space, so each
is recorded here with its reasoning.

### Buyer identity: collected, never assumed

**The webview has no idea who the buyer is.** A link opened from a status or a
channel is just a browser tab — no Meta user id, no phone number, browser
sandbox boundary. Any design that assumed *"we know the buyer because they came
from WhatsApp"* was wrong.

So checkout **asks**. One phone field does double duty: it is the STK push
target, and — with an explicit checkbox — the opt-in for a WhatsApp receipt.

> **The on-page receipt is not the fallback. It is the primary.** It is the only
> receipt that works for every buyer regardless of opt-in, network, or Meta's
> template rules. The WhatsApp receipt is the bonus.

### Payments: the buyer pays the seller directly. We never touch the money.

**A central paybill was proposed and rejected on 2026-08-22.** Aggregating
buyers' payments into our own shortcode and settling out to sellers would make
us a payment intermediary holding other people's money — in Kenya, a CBK / PSP
licensing question. That is not a risk worth carrying to remove onboarding
friction, and it is far cheaper to decline before there is float in an account
than after.

So money goes buyer → seller, and we are never in the path.

**The seller's registered method decides how a payment is confirmed**, and there
are two paths because Safaricom gives us no choice:

| Seller's method | Checkout | Confirmation |
|---|---|---|
| **Pochi la Biashara** | shows the number, buyer pays in M-Pesa | manual: buyer enters the code, seller confirms |
| Till / Paybill, no credentials | shows the number, buyer pays in M-Pesa | manual: buyer enters the code, seller confirms |
| Till / Paybill **+ Daraja credentials** | STK push to the buyer's handset | automatic, via the seller's callback |

> **Pochi la Biashara can never do STK push.** Daraja's STK Push and C2B APIs
> work with Paybill and Buy Goods shortcodes only — Pochi is not one. Since a
> large share of micro-sellers run on Pochi, **the manual confirmation path is
> permanent and first-class, not a shim we delete later.** Any design that
> treats STK as the real path and manual as a fallback is wrong for the
> majority of our sellers.

**What this costs, recorded so nobody is surprised:**

1. **No per-transaction commission is possible.** A fee requires being in the
   money path, and we have deliberately left it. **Monetisation is the
   subscription tier**, not a cut of sales. `platform_fee`, `seller_payout_amount`
   and `payout_status` are therefore *not* in the schema — they described an
   aggregator we are not building.
2. **Daraja credentials are custody.** A seller who wants automatic STK hands us
   their shortcode passkey and consumer secret. Encrypted at rest, never logged,
   and a breach exposes a seller's payment credentials. The manual path exists
   partly so no seller is *forced* into this.
3. **A manual confirmation is a claim, not proof.** The buyer's M-Pesa code is
   unverified text until the seller checks it. Order status must say so:
   `awaiting_confirmation` is not `paid`, and only the seller moves it.

### Seller surface: WhatsApp captures, the web reports

| WhatsApp bot | Web dashboard |
|---|---|
| Forward-to-catalog | Order history, tabular |
| Instant order alerts | Inventory adjustment |
| Simple status toggles | Receipts and exports |
| Magic link delivery | Deep analytics |

A financial summary table rendered into a chat thread is a bad experience for
everyone. Capture belongs where the seller already is; reporting belongs on a
screen that can hold a table.

### Variants: one stock pool, choice recorded as text

A Nairobi micro-seller holds a pile of ten shoes and sells until the pile is
gone. They do not track "Black 38" separately from "Black 40". Modelling
per-variant stock would impose an inventory discipline the seller does not have
and does not want.

So: `stock` stays a flat integer on `Product`. The buyer's choice is captured as
a descriptive string on the order line — `selected_variant: "Size 40 / Black"`.

> This is **spike before schema** applied honestly. If a real pilot seller turns
> out to need per-variant stock, we will have their actual data to design from.
> Guessing now costs a migration either way; guessing simple costs less.

### Seller auth: WhatsApp-first, magic link, social optional

```
Seller sends "START" to the bot
          │
   Bot asks shop name + location
          │
   Shop provisioned: /shop/<slug>, bound to seller_phone
          │
   Magic link ──▶ web dashboard, one-time token, no password
          │
   (optional, later) connect TikTok / Instagram
```

**TikTok is not an authentication wall.** It is an optional connection a seller
may add from the dashboard. The bio-code verification we already built stays,
and becomes opt-in rather than mandatory.

---

## 4. What survives, what is parked

Nothing is deleted. Parked code stays on `p1-catalog` and can be revived.

### Survives, and gets more valuable

| Asset | Why it matters more now |
|---|---|
| `agent/draft.py` + `schemas/draft.py` | The cover-image vision parse **is** the forward-to-catalog parser. Now aimed at a composed catalog post rather than a video frame — an easier read on a better image. |
| `services/media.py` | Storing our own copy of an image is exactly what WhatsApp media download needs. |
| `models/job.py`, `services/jobs.py`, `worker.py` | Media download and vision parse must not run inside a webhook. Meta retries anything slow. |
| `Product` money rails | `published_requires_price`, `stock_non_negative`, `price_positive`, `price_plausible` — untouched, and now guarding real payments. |
| Storefront service + slug rules | The buyer-visibility rules were right; only the URL shape and the UI change. |
| Auth, sessions, dashboard shell | Magic link is additive — the cookie and session layer is reused as-is. |

### Parked

| Asset | Status |
|---|---|
| `services/scraper.py`, `schemas/tiktok.py` | Apify profile scraping. Parked; the single-post path may return for "paste a TikTok link". |
| `services/sync.py`, `services/analytics.py` | Profile sync and metric recording. Parked whole. |
| `models/post.py`, `models/snapshot.py` | The content corpus. Parked — but see the note below. |
| `services/verification.py`, `models/account_claim.py` | **Not parked — demoted.** Still works, now optional. |

> **On the snapshot tables:** they were built because metric history cannot be
> backfilled. That argument was correct and is now moot — we are not collecting
> metrics. Parking them costs nothing; the tables stay in the schema, empty.

### Deliberately reversed

The previous plan stated: *"WhatsApp is not a dependency. The web platform
stands alone… WhatsApp is added later as a channel, not as the foundation."*

**That is now false.** WhatsApp is the foundation: it is how a seller signs up,
how they add stock, how they are alerted, and where the buyer starts. The web is
the reporting surface, and the storefront that WhatsApp opens.

---

## 5. Schema additions

Five new tables. Everything else is already in place.

Six new tables. Everything else is already in place.

```
Seller ──┬──▶ Product ──────────┐
         │                      │
         ├──▶ PaymentMethod     │   pochi | till | paybill (+ optional creds)
         │                      │
         └──▶ Order ──▶ OrderItem
                 │
                 └──▶ Payment  (one attempt; STK callback OR a buyer's claim)

WaContact ──▶ WaMessage        (inbound dedupe — Meta redelivers)
MagicToken                     (one-time dashboard login)
```

| Table | Holds | The one hard rule |
|---|---|---|
| `PaymentMethod` | seller's kind + number, encrypted Daraja creds if any | **Pochi can never STK.** The kind decides the checkout path. |
| `Order` | total, buyer phone, delivery, status | Money is integer KES. `total_amount = sum(items) + delivery_fee`. **No platform fee — we are not in the money path.** |
| `OrderItem` | product, qty, **price at time of order**, `selected_variant` | Price is **copied, never joined**. A seller editing a price must not rewrite history. |
| `Payment` | checkout id, M-Pesa code, raw callback, `confirmed_by` | An STK callback is truth. **A buyer-entered code is a claim** until the seller confirms. |
| `WaMessage` | inbound message id | Meta redelivers. A forwarded post must not create two products. |
| `MagicToken` | hash, expiry, used_at | Single use. Hashed at rest, like a password. |

**Order status is not a boolean.** The manual path needs a state that means
*"someone says they paid and nobody has checked"*:

```
pending ──▶ awaiting_confirmation ──▶ paid
   │              (manual path)         ▲
   │                                    │
   └──────── STK callback ──────────────┘
                                    (automatic path)
   └──▶ cancelled / failed
```

---

## 6. The phases

Each ends in something demonstrable. `W` for the WhatsApp-first line, to keep
them distinct from the superseded P-phases in older documents.

### W0 — Documents `w0-docs`

This file, `CLAUDE.md`, the build log entry, `BUILD_ORDER.md` superseded.

### W1 — The storefront, rebuilt `w1-storefront`

*Goal: the shop in the mockup exists and works, minus payment.*

- Move to `/shop/{slug}` — retires the "register the router last" rule and `RESERVED_SLUGS`
- Drop the Tailwind CDN for a compiled stylesheet; the buyer pays for that payload in mobile data
- Product cards, category pills, search, filters
- Product detail with size/colour choice
- **Cart** — server-side, cookie-keyed, survives a page reload
- Fix `og:image` to an absolute URL — a link pasted into WhatsApp with no preview image is a wasted first impression
- The first real tests the storefront has ever had

**Done when:** a buyer browses, filters, picks a size, fills a cart and reaches a checkout page.

### W2 — Orders and M-Pesa `w2-payments`

*Goal: money moves from a buyer to a seller, both paths, us never in between.*

- `PaymentMethod`, `Order`, `OrderItem`, `Payment` + migrations
- Checkout: phone, delivery, opt-in → `Order(pending)`
- **Path A — manual (Pochi, or any number with no credentials).** Show the
  seller's number, take the buyer's M-Pesa code, `awaiting_confirmation`, seller
  confirms. **Build this first: it works for every seller and needs no
  credentials, so it is the one path that cannot be blocked.**
- **Path B — STK (Till/Paybill + the seller's own Daraja credentials).** Client
  behind our adapter; credentials encrypted at rest and never logged
- **Idempotent callback** — the only automatic payment truth, and it is retried
- Polling page: the STK prompt interrupts the browser, so the page must survive being backgrounded
- On-page receipt, on both paths

**Done when:** a Pochi seller takes an order end to end with no credentials at
all — and a Till seller's sandbox STK completes automatically, with the same
callback replayed five times changing nothing.

### W3 — The WhatsApp bot `w3-whatsapp` — *blocked on sandbox credentials*

- Webhook: verify token, **signature verification**, idempotency via `WaMessage`
- `START` → shop name → slug provisioned → magic link
- **Forward-to-catalog**: media download → job → vision parse → draft product → reply
- Order alerts to the seller; receipts to opted-in buyers

**Done when:** forwarding a real catalog post to the bot creates a product and replies with a working link.

### W4 — The seller dashboard `w4-dashboard`

- Order table, receipts, exports
- Inventory adjustment, publish gate
- **The publish flow — which does not exist today in any form** (see §7)

**Done when:** a seller takes a forwarded post to published and watches an order arrive.

### W5 — Social as lookup `w5-social`

- "Saw it on TikTok?" — the buyer pastes a link and we match it to a product in this shop
- Optional TikTok/Instagram connection from the dashboard

### W6 — Pilot `w6-pilot`

Rate limits, AI cost caps, secrets audit. Real sellers. One real order end to end.

---

## 7. The gap nobody had noticed

**There is no publish flow anywhere in the application.** Nothing in `api/` or
`services/` ever sets `is_published = True` or `ProductStatus.PUBLISHED`. The
only two places in the repo that do are in `scripts/seed_demo_shop.py`.

Ingestion writes `DRAFT` and nothing ever promotes it. A real seller today would
sign up, add stock, and get a 404 storefront. The rule *"publish is a human
gate"* was written down, and the gate itself was never built.

It lands in W4, and W1 is built assuming it will exist.

---

## 8. Rules that do not change

- **The agent proposes, code disposes.** The LLM never decides price, stock,
  payment status or consent.
- **Publish is a human gate**, and still requires a price.
- **We are never in the money path.** The buyer pays the seller. We record that
  it happened; we do not hold, split or forward funds.
- **The payment callback is the only automatic payment truth**, processed
  idempotently — Daraja retries, and so does Meta.
- **A buyer-entered M-Pesa code is a claim, not a payment.** Only the seller
  moves an order from `awaiting_confirmation` to `paid`.
- **Rails before agent.** Payment paths are built and tested before an agent may
  call them.
- **Money is integer KES**, and the price is of one unit *as sold*.
- **Order lines copy the price.** Never join to a live product for history.
- **Cache anything that costs money.** Each forwarded image is parsed once,
  keyed by WhatsApp media id.
- **No AI framework, no RAG.** Provider SDKs directly, Pydantic as the guardrail.
- **One database per application**, and tests get their own.
- **Spike before schema.**

---

## 9. Open risks

| Risk | Standing |
|---|---|
| **No per-transaction revenue** | Accepted, and the direct consequence of not holding funds. Monetisation is the subscription tier. Revisit only via a *licensed* PSP — never by routing funds through us. |
| **Manual confirmation is unverified** | A buyer's M-Pesa code is a claim. The seller checks it. Fraud surface is the seller's own, as it is today in their WhatsApp group. |
| **Custody of seller Daraja credentials** | Encrypted at rest, never logged. The manual path exists so no seller is forced to hand them over. |
| Pochi sellers can never have automatic confirmation | Safaricom's limitation, not ours. Manual path is permanent and first-class. |
| Meta business verification | Blocks W3 only. W1 and W2 proceed regardless. |
| WhatsApp receipt requires opt-in | Mitigated: the on-page receipt is primary. |
| Wordmark: "Biasharamall" vs "Biashara Mall" | **Open.** The mockup says one word; every document says two. |

---

## Appendix — parked evidence

The spikes below remain valid and their evidence is saved in
`backend/spikes/out/`. They supported the analytics and content engine, which is
deferred rather than abandoned.

- **Spike 05** — own-account analytics via Apify: fully confirmed
- **Spike 06** — competitor discovery by keyword/hashtag: confirmed
- **Canva MCP** and **capcut-cli**: both evaluated and viable for the content
  engine when it returns

The pricing intent also stands: **Starter** free (WhatsApp store, M-Pesa
checkout), **Growth** paid (content and intel). The pivot does not change what
is free — it changes what is built first.
