# Biashara Mall — Project Context

> Read automatically at the start of every Claude Code session. Keep it under ~200 lines.

## What we're building

Biashara Mall turns a Kenyan seller's WhatsApp catalog into a real shop with
M-Pesa checkout — without asking them to leave WhatsApp.

**The direction changed on 2026-08-22: we no longer scrape social media for a
catalog. Sellers forward it to us.** Full reasoning in
[`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md) §1.

```
Seller forwards a post ──▶ bot parses it ──▶ product created
                                                   │
                                    "Added! Your store: /shop/zuma"
```

Sellers already run catalogs inside WhatsApp groups. The photo and the caption
already exist, written by them, where they already work. We receive them instead
of paying a scraper to reconstruct them badly.

## The two journeys

```
SELLER   "START" to the bot → shop name → /shop/<slug> provisioned
         → magic link to the web dashboard
         → forward a post to add a product

BUYER    taps the link in a chat, channel or status
         → WhatsApp in-app browser opens the storefront
         → browse → size/colour → cart → checkout
         → pays the seller in M-Pesa → receipt on page (+ WhatsApp, opt-in)
```

The buyer surface is the ordinary **in-app browser**, not WhatsApp Flows — no
Flows approval dependency, full control of the UI, and it debugs in a normal
browser.

**The webview does not know who the buyer is.** A link opened from a status is
just a browser tab: no Meta user id, no phone number. Checkout *asks* for the
phone — it is both the STK target and the receipt opt-in. Never assume buyer
identity.

## Current state

P0 (Foundation) and P1 (Catalog) are complete: FastAPI spine, typed config, DB
layer, auth and sessions, the price cascade, verification, and a public
storefront. 308 tests.

**Now on the W-line** (WhatsApp-first): **W1 — rebuild the storefront**, then W2
payments, W3 the bot. Phases in [`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md) §6.

> **There is no publish flow.** Nothing outside `scripts/seed_demo_shop.py` ever
> sets `PUBLISHED`. A real seller today gets a 404 storefront. Lands in W4.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Framework | FastAPI |
| Validation | Pydantic v2 — API schemas **and** LLM output schemas |
| Database | PostgreSQL + SQLAlchemy 2.0 + Alembic |
| Templating | Jinja2 (server-rendered storefront) |
| AI | Gemini via `google-genai`, called directly |
| Messaging | WhatsApp Cloud API |
| Payments | M-Pesa, paid **direct to the seller** — manual confirm, or the seller's own Daraja STK |
| Scraping | Apify — **parked**, behind our adapter if it returns |
| Testing | pytest, transactional fixtures against real Postgres |
| Hosting | Railway |

## Architecture rules — do not silently change

- **The agent proposes, code disposes.** The LLM never decides price, stock,
  payment status or consent. It drafts; deterministic code transacts.
- **Publish is a human gate**, and still requires a price.
- **We are never in the money path.** The buyer pays the seller directly. We
  record that it happened; we never hold, split or forward funds. This rules out
  a per-transaction commission by design — revenue is the subscription tier.
- **The payment callback is the only automatic payment truth**, processed
  idempotently — Daraja retries, and so does Meta.
- **A buyer-entered M-Pesa code is a claim, not a payment.** Only the seller
  moves an order from `awaiting_confirmation` to `paid`.
- **Order lines copy the price.** Never join to a live product for history: a
  seller editing a price must not rewrite what a buyer already paid.
- **Inbound WhatsApp messages are deduped by message id.** Meta redelivers; a
  forwarded post must not create two products.
- **Rails before agent.** Payment paths are built and tested before an agent may
  call them.
- **No RAG.** Every lookup is by known key. Don't bring a vector database to a
  `WHERE id = ?` problem.
- **No AI framework.** Provider SDKs directly, Pydantic as the guardrail.
- **One database per application** — and tests get their own too. `TEST_DATABASE_URL`
  must never equal `DATABASE_URL`; the suite refuses to run if it does.
- **Spike before schema.** Let a real payload drive the data model.
- **Money is integer KES.** No floats near a price.
- **The workspace is one design, not six pages.** Icons come from
  `partials/icons.html`; shared parts live in `workspace.css` and earn their
  place there once a second page needs them. Status wears one vocabulary
  everywhere: green done, amber needs you, grey not yet, red over.
- **Never show a seller invented data**, and never ship a control that cannot
  work. A design mockup's sample rows are for judging layout, not for shipping.
- **Cache anything that costs money.** Each forwarded image parsed once ever,
  keyed by WhatsApp media id.

## Prices are not in captions

Verified against 24 real captions in `fixtures/poc-captions.json`: **zero**
mention KSh, only three contain any price-like number. Sellers withhold the
price deliberately — that is the buyer bottleneck the product removes.

Extraction is a **cost-ordered cascade**, escalating only when the cheaper tier
yields no confident price:

1. **Caption** (near-free) — category hints, sometimes sizes.
2. **Image** — price burned in as a text overlay.

The video tier is parked with the scrapers. **The image tier is now the primary
parser**, and it got an easier job: a forwarded catalog post is composed to be
read, unlike a video cover frame.

## Payments — two paths, and why

The buyer pays the seller. **We are never in the middle**, which is a deliberate
refusal to become a payment intermediary holding other people's money.

| Seller's method | Checkout | Confirmation |
|---|---|---|
| **Pochi la Biashara** | shows the number | manual — buyer enters the code, seller confirms |
| Till / Paybill, no credentials | shows the number | manual — same |
| Till / Paybill **+ their own Daraja creds** | STK push | automatic, via their callback |

> **Pochi la Biashara can never do STK push** — Daraja's STK and C2B APIs work
> with Paybill and Buy Goods shortcodes only. A large share of micro-sellers run
> on Pochi, so **the manual path is permanent and first-class, not a fallback.**
> Build it first: it needs no credentials, so nothing can block it.

Order status carries the difference: `pending → awaiting_confirmation → paid`,
and only the seller makes the last move.

## Variants and stock

One flat `stock` integer per product. The buyer's size/colour choice is captured
as a string on the order line (`"Size 40 / Black"`). Kenyan micro-sellers hold a
pile and sell until it's gone — they do not track per-variant stock, and
modelling it would impose a discipline they don't want.

## Layout

```
backend/
  app/
    main.py          thin — wires routers, owns nothing else
    config.py        typed settings; NOTHING else reads os.environ
    db.py            engine + session; import get_db, never build an engine
    models/          SQLAlchemy models — DB rails live here
    schemas/         Pydantic wire + LLM schemas
    services/        business logic — providers behind our own adapters
    agent/           LLM-facing code — the only place a provider SDK is imported
    api/             HTTP routes — thin, delegate to services
    templates/       Jinja2 — storefront, workspace, auth
      app_base.html    the shell for every page behind the login wall
      partials/icons.html  ONE icon set — never hand-write an SVG in a page
    static/css/
      workspace.css    the seller workspace skin, loaded by the shell
  alembic/           migrations, reviewed line by line
  tests/             pytest, mirrors app/
docs/                plan, business + technical PDFs, Notion handoff
fixtures/            real caption data for AI accuracy tests
```

`api/` handles HTTP shapes, `services/` holds logic, `models/` owns persistence.
A route containing business logic is in the wrong layer.

## Commands

```bash
cd backend
.venv\Scripts\activate                  # Windows
uvicorn app.main:app --reload           # dev server
python -m app.worker                    # background worker

ruff check . && ruff format --check .   # lint
mypy app tests                          # types
pytest                                  # tests

alembic revision --autogenerate -m "…"  # create a migration — then REVIEW it
alembic upgrade head                    # apply
```

## Documentation standard — non-negotiable

**Every file opens with a header docstring** covering three things: what it does,
the pipeline it sits in (ASCII diagram when data flows through), and **why it
exists and is shaped that way**. Docstrings on every public function. Comments
explain **why**, not what.

In six weeks nobody remembers why the cascade escalates in that order or why a
callback is idempotent. The code has to say so itself.

**Tests ship in the same PR as the logic.** External services are always mocked —
tests never hit a paid API. No silent `except`. CI green or it doesn't merge.

Full agreement: [`WAYS_OF_WORKING.md`](./WAYS_OF_WORKING.md).

## Reference implementation

`Project TIKTOK` (`../Project TICKTOCK`, repo `Data-Amigo/PROJECT_TIKTOK`) — a
working Python predecessor, live on Railway. **Its idempotent Daraja callback
and STK client are the most relevant thing in it right now**; read them before
writing W2. Adapt it; never copy blind, never reinvent either.

## Workflow

- **One branch per milestone**: `w1-storefront`, `w2-payments`, …
- **Remote is HTTPS** — no SSH keys on this machine.
- Notion is the source of truth for status; the repo for code.
- New ideas go to the Notion **Backlog**, not into the current milestone.
