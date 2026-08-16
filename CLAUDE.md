# Biashara Mall — Project Context

> Read automatically at the start of every Claude Code session. Keep it under ~200 lines.

## What we're building

Biashara Mall turns a Kenyan seller's TikTok feed into a shop that lives inside WhatsApp.

```
Biashara Mall
├── Soko Commerce    Catalog · Orders · Payments · WhatsApp Storefront
├── Soko Intel       Scripts · Captions · Market Insights · Competitor Intelligence
└── Soko AI          Customer Support · Sales Agent · Follow-ups · Order Assistance
```

**SokoLedger** (on-device M-Pesa reconciliation) is parked as Phase 2. Do not build it.

## The buyer journey

```
WhatsApp chat → browse → WhatsApp in-app browser opens → Biashara Mall storefront
    → product selection → M-Pesa checkout → return to WhatsApp
```

This is the ordinary **in-app browser**, not WhatsApp Flows — no Flows approval
dependency, full control of the UI, and it debugs in a normal browser.

## Current state

**P0 (Foundation) is complete.** FastAPI spine, typed config, database layer,
health endpoints, Alembic, pytest, CI. Next is **P1 — Catalog**.

Full plan: [`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md) (P0–P9, four phases).

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Framework | FastAPI |
| Validation | Pydantic v2 — API schemas **and** LLM output schemas |
| Database | PostgreSQL + SQLAlchemy 2.0 + Alembic |
| Templating | Jinja2 (server-rendered storefront) |
| AI | Gemini via `google-genai`, called directly |
| Scraping | Apify, behind our own adapter |
| Messaging | WhatsApp Cloud API |
| Payments | M-Pesa Daraja (STK push) |
| Testing | pytest, transactional fixtures against real Postgres |
| Hosting | Railway |

## Architecture rules — do not silently change

- **The agent proposes, code disposes.** The LLM never decides price, stock,
  payment status or consent. It drafts; deterministic code transacts.
- **Publish is a human gate**, and still requires a price.
- **The payment callback is the only payment truth**, processed idempotently —
  Daraja retries, and so does Meta.
- **Rails before agent.** Payment paths are built and tested before an agent may call them.
- **No RAG.** Every lookup is by known key. Don't bring a vector database to a
  `WHERE id = ?` problem.
- **No AI framework.** Provider SDKs directly, Pydantic as the guardrail.
- **One database per application** — and tests get their own too. `TEST_DATABASE_URL`
  must never equal `DATABASE_URL`; the suite refuses to run if it does.
- **Spike before schema.** Let a real payload drive the data model.
- **Money is integer KES.** No floats near a price.
- **Cache anything that costs money.** Apify once per day per seller; each video
  processed once ever, keyed by video id.

## Prices are not in captions

Verified against 24 real captions in `fixtures/poc-captions.json`: **zero**
mention KSh, only three contain any price-like number. Sellers withhold the
price deliberately — that is the buyer bottleneck the product removes.

Extraction is a **cost-ordered cascade**, escalating only when the cheaper tier
yields no confident price:

1. **Caption** (near-free) — category hints, sometimes sizes.
2. **Cover image** — price burned in as a text overlay.
3. **Video, audio + visual** — the seller says the price aloud in Sheng.

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
    templates/       Jinja2 — the storefront
  alembic/           migrations, reviewed line by line
  tests/             pytest, mirrors app/
  spikes/            throwaway probes against real APIs
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

`Project TIKTOK` (`../Project TICKTOCK`, repo `Data-Amigo/PROJECT_TIKTOK`) is a
working Python predecessor — 63 commits, 110 tests, live on Railway. It proved
the price cascade, the Apify adapter, the sales agent, and idempotent Daraja
callbacks.

**Read and adapt it; never copy blind, never reinvent either.** Its
`docs/CONCEPTS.md` is worth reading in full. It is a reference, not a foundation:
production Biashara Mall puts WhatsApp at the centre and adds Soko Intel, which
TIKTOK never had.

## Workflow

- **One branch per milestone**: `p0-foundation`, `p1-catalog`, …
- **Remote is HTTPS** — no SSH keys on this machine.
- Notion is the source of truth for status; the repo for code. The connector has
  been unreliable — [`docs/NOTION_WORKSPACE.md`](docs/NOTION_WORKSPACE.md) holds
  the full spec to rebuild it from.
- New ideas go to the Notion **Backlog**, not into the current milestone.
