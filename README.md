# SokoLink

**Where your audience becomes your customers.**

A Kenyan seller's TikTok feed becomes a shop that lives inside WhatsApp. Buyers
browse, ask questions in Sheng, and pay by M-Pesa without leaving the
conversation.

```
SokoLink
├── Soko Commerce    Catalog · Orders · Payments · WhatsApp Storefront
├── Soko Intel       Scripts · Captions · Market Insights · Competitor Intelligence
└── Soko AI          Customer Support · Sales Agent · Follow-ups · Order Assistance
```

---

## The problem

A buyer sees a product in a TikTok video. To buy it they must comment
*"Price?"*, wait hours for a reply, hunt for a bio link, then compose a message
describing an item the seller cannot identify. Every step leaks intent.

The price is withheld on purpose — verified against 24 real captions, **zero**
mention KSh. That tactic is exactly what creates the bottleneck.

SokoLink reads the price off the video cover, or hears the seller say it in
Sheng, and turns the whole feed into something a buyer can tap.

---

## Quick start

**Prerequisites:** Python 3.11+, and two Postgres databases — one for the app,
one for tests.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate                    # Windows
# source .venv/bin/activate               # macOS / Linux

pip install -r requirements.txt

cp ../.env.example ../.env                # then fill in DATABASE_URL

alembic upgrade head                      # apply migrations
uvicorn app.main:app --reload             # http://localhost:8000
```

Check it's alive, and that it can actually reach Postgres:

```bash
curl http://localhost:8000/health         # liveness  — no dependencies
curl http://localhost:8000/health/ready   # readiness — checks the database
```

Interactive API docs (dev only): <http://localhost:8000/docs>

---

## Commands

| Command | What it does |
|---|---|
| `uvicorn app.main:app --reload` | Dev server |
| `pytest` | Run tests |
| `ruff check .` | Lint |
| `ruff format .` | Format |
| `mypy app tests` | Type check |
| `alembic revision --autogenerate -m "…"` | Create a migration — **then review it** |
| `alembic upgrade head` | Apply migrations |

---

## Two databases, on purpose

`DATABASE_URL` and `TEST_DATABASE_URL` must point at **different** databases.
The test suite creates tables and writes rows.

- If `TEST_DATABASE_URL` is unset, database-backed tests **skip** rather than
  running somewhere dangerous.
- If it equals `DATABASE_URL`, the suite **refuses to run**.

This is not paranoia. Sharing a database across projects has already caused a
near-miss here — an unrelated app's tables collided and a migration offered to
reset the whole schema.

---

## Layout

```
backend/
  app/
    main.py       thin — wires routers
    config.py     typed settings; nothing else reads os.environ
    db.py         engine + session
    models/       SQLAlchemy models — the DB rails
    schemas/      Pydantic wire + LLM schemas
    services/     business logic
    agent/        LLM-facing code
    api/          HTTP routes
    templates/    Jinja2 storefront
  alembic/        migrations
  tests/          pytest
docs/             plan, business + technical documents
fixtures/         real caption data for AI accuracy tests
```

---

## Conventions that will bite you if you miss them

- **Money is integer KES.** Never a float near a price.
- **Never read `os.environ`.** Import `settings` from `app.config`.
- **Never build an engine.** Import `get_db` from `app.db`.
- **Never trust LLM output shape.** Pass a Pydantic schema so generation is
  constrained; validate what comes back.
- **Every file opens with a header docstring** explaining *why* it exists.
- **Tests ship in the same PR as the logic**, and never call a paid API.

Full engineering standard: [`WAYS_OF_WORKING.md`](./WAYS_OF_WORKING.md)
Project context for Claude Code: [`CLAUDE.md`](./CLAUDE.md)

---

## Documentation

| Document | What it covers |
|---|---|
| [`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md) | The build plan — P0–P9 across four phases |
| [`docs/SokoLink_Business_Document.pdf`](docs/SokoLink_Business_Document.pdf) | Problem, market, product, moat, business model |
| [`docs/SokoLink_Technical_Document.pdf`](docs/SokoLink_Technical_Document.pdf) | Architecture, stack, flows, security |
| [`docs/SokoLink_Project_Flow.pptx`](docs/SokoLink_Project_Flow.pptx) | End-to-end flow deck |
| [`docs/NOTION_WORKSPACE.md`](docs/NOTION_WORKSPACE.md) | Spec to rebuild the Notion tracker |

Documents are generated from `docs/src/` — edit the source and re-run, so four
documents cannot drift apart from each other.

---

## Status

**P0 (Foundation) complete** — FastAPI spine, typed config, database layer,
health endpoints, Alembic, pytest, CI.

**Next: P1 — Catalog.** Apify ingestion and the price cascade.

**Not in scope:** SokoLedger (on-device M-Pesa reconciliation), marketplace
search, generative try-on. All Phase 2.
