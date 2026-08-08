# Build Log

> A technical record of what was built, what broke, and why each decision was
> made. Written as we go, newest entry last.
>
> **Why this file exists:** in six weeks nobody remembers why the config
> normalises a database driver, or why the test suite refuses to run without a
> second database. The code says *what*; this says *why it had to be that way*,
> including the dead ends — a fix whose reasoning is lost gets undone by the
> next person who finds it inconvenient.

---

## 2026-08-05 → 08 · Re-baseline and P0 Foundation

### Context: the project changed shape twice in one day

The session opened intending to build a 14-day TypeScript plan. Two discoveries
changed that.

**1. A working Python implementation already existed.** `Project TIKTOK`
(`Data-Amigo/PROJECT_TIKTOK`) — 63 commits, 110 passing tests, live on Railway.
It already covered most of what the plan called Days 1–6: Apify ingestion, a
Gemini vision draft agent, an M-Pesa Daraja client with an idempotent callback,
auth, and a public storefront.

**2. WhatsApp became the interface, not the exit.** Meta business verification
is in progress, which reverses TIKTOK's 2026-07-25 pivot — that pivot had moved
the agentic close onto the web *specifically because* Meta was blocking it.

**Decisions taken:**

- **Python, not TypeScript.** Fredrick is materially stronger in Python and can
  debug it unaided. On a solo build that outweighs framework preference. Pydantic
  replaces Zod and does double duty as the LLM structured-output schema.
- **Build fresh; TIKTOK is a reference, not a foundation.** Its proven modules
  are read and adapted, never copied blind and never reinvented either.
- **Three pillars**: Soko Commerce, Soko Intel, Soko AI.
- **The mini app is the WhatsApp in-app browser, not WhatsApp Flows.** Removes
  the Flows approval dependency, gives full UI control, and debugs in an
  ordinary browser. It also collapses two surfaces into one — the same
  server-rendered pages are the in-app storefront *and* the SEO presence.

### A finding that was already known

Mid-session, analysis of the 24 exported POC captions showed **zero mention KSh**
and only three containing any price-like number — so prices cannot be parsed from
captions.

This was presented as a discovery. It was not: TIKTOK's `spike_00` established
it on 2026-07-22, and `agent/draft.py` already implements the full cascade
(cover image, then Gemini watching *and listening* to the clip), proven live at
KES 500/550/550/600.

**Lesson recorded deliberately:** read the reference project's docs *before*
analysing its data. Several hours went into re-deriving a conclusion that was
already written down in `docs/CONCEPTS.md`.

### Documents produced

Generated rather than hand-written, from `docs/src/`, so a plan change is an edit
and a re-run instead of four documents silently drifting apart:

| Output | Source | How |
|---|---|---|
| `SokoLink_Business_Document.pdf` | `src/business.html` | headless Chrome `--print-to-pdf` |
| `SokoLink_Technical_Document.pdf` | `src/technical.html` | headless Chrome `--print-to-pdf` |
| `SokoLink_Project_Flow.pptx` (13 slides) | `src/build_deck.py` | python-pptx |

The deck was exported to PNG via PowerPoint COM and inspected slide by slide —
which caught two real layout collisions (callout boxes overlapping the footer on
the tech-stack and hard-parts slides). Generating a deck without looking at it is
not verification.

---

### P0 — Foundation

**Retired the TypeScript scaffold.** `apps/`, `packages/`, `node_modules`
(704 MB), and all the JS tooling config. Uncommitted, so nothing was lost from
history.

**Built the FastAPI spine:**

```
backend/
  app/
    main.py       thin — wires routers, owns nothing else
    config.py     typed settings; NOTHING else reads os.environ
    db.py         engine + session; pool_pre_ping for cloud Postgres
    api/health.py liveness + readiness
    models/ schemas/ services/   layered, documented, empty until P1
  alembic/        migrations, wired to app config
  tests/          pytest with transactional fixtures
  scripts/        setup utilities
```

**Design decisions worth keeping:**

- **`/health` and `/health/ready` are separate endpoints.** They answer different
  questions with different consequences. Liveness failing means "restart me";
  readiness failing means "stop routing traffic, but do *not* restart — the
  database is down and restarting will not fix that." Collapsing them makes a
  slow database into a platform restart-loop. A test asserts liveness never
  opens a database connection.
- **`pool_pre_ping=True` and `pool_recycle=300`.** Cloud Postgres silently drops
  idle connections. Without pre-ping the app hands a request a dead connection
  and the user sees a 500 that vanishes on retry — the worst kind of bug to chase.
- **Settings are lazy (`@lru_cache`), not evaluated at import.** An eager
  `settings = Settings()` throws the moment anything imports the module, which
  breaks tests and tooling that only need to resolve it.
- **`extra="ignore"` on Settings.** Railway injects `PORT` and `RAILWAY_*`.
  Without this, deploys fail on someone else's config.
- **Alembic reads the URL from `app.config`, not `alembic.ini`.** The URL is a
  secret and `alembic.ini` is committed. One source means the app, the tests and
  the migrations can never disagree about which database they mean.

### Five things that broke, and the fixes

**1. ruff reported 42,462 errors.** It was linting `.venv`. Added it to
`exclude`. Real error count: 1.

**2. `alembic/env.py` failed import sorting.** An explanatory comment sat *between*
imports, splitting the block. Moved the comment below the imports — where it now
also warns that the apparently-unused `Base` import is load-bearing: without it,
autogenerate sees an empty schema and writes a migration that drops every table.

**3. `ModuleNotFoundError: No module named 'psycopg2'`.** SQLAlchemy reads a bare
`postgresql://` URL as "use psycopg2", which is not installed — we use psycopg 3.
Railway hands out exactly that bare URL.

Fixed in `config.py` with `_with_psycopg_driver()`, which rewrites the scheme to
`postgresql+psycopg://`. **Chosen over telling the developer to hand-edit the URL**,
because that is a step to forget on every new environment, and the failure
surfaces far from its cause. Also handles the legacy `postgres://` scheme.

**4. Four tests failed because they were reading the real `.env`.** `Settings(**values)`
still loads the env file, so `apify_token` was populated from the developer's
machine and the "later-milestone keys are optional" test failed. A test that
passes for one person and fails for another is worse than no test.

Fixed with a `build()` helper passing `_env_file=None` — every config test is now
hermetic.

**5. mypy rejected a `# type: ignore[arg-type]`.** The correct code was
`call-arg`. Narrowed to `[call-arg,arg-type]` with a comment explaining that
`_env_file` is a pydantic-settings init hook rather than a declared field.

### The test database problem

`.env` still pointed at the **POC database** holding the 24 real captions. Today
that was harmless — no models exist, so `create_all` created nothing. At P1 it
would have started creating tables in there.

**Safety rail added** (`TEST_DATABASE_URL`):

- Unset → database-backed tests **skip**. A skipped test is a visible gap; a test
  that quietly rewrites real data is a disaster.
- Equal to `DATABASE_URL` → the suite **refuses to run**. That is never intentional.

**How the databases were provisioned.** Docker was available but heavy, and the
daemon was not running. Rather than provisioning a second Railway service:

> A Postgres **server** hosts many **databases**, and Postgres databases cannot
> see each other's tables. Two extra databases on the existing instance give the
> same isolation at zero cost, with nothing new to run.

The Railway role turned out to be superuser with `CREATEDB`, so
`scripts/create_databases.py` (idempotent, documented) created:

```
railway         ← POC data, 24 captions — untouched, referenced nowhere
sokolink        ← the application database
sokolink_test   ← the test database
```

**Known limitation, accepted deliberately:** this is *logical* isolation, not
*resource* isolation — CPU, storage and connection limits are shared with the POC
database. Fine for development. Before real sellers arrive, `sokolink` should
move to its own Railway service; that is a dump and restore.

### CI

`.github/workflows/ci.yml` runs ruff, ruff format, mypy, pytest, and
`alembic upgrade head` on every PR, with a **real Postgres 17 service container**
— because the DB-level constraints *are* the rails, and SQLite would silently
accept what Postgres rejects. No real secrets needed: external services are
always mocked.

The migration step matters on its own: it proves the history applies to an empty
database. A migration that cannot run from empty is a broken deploy.

### Verification

Not "it should work" — actually run:

```
ruff          All checks passed
ruff format   14 files already formatted
mypy          Success — 13 source files (strict)
pytest        18 passed              (was 14 passed / 4 skipped before the DBs existed)
alembic       f33154a36521 (head), applied to sokolink

live uvicorn process, real HTTP, real round trip:
  GET /health        {"status":"ok","app":"SokoLink","env":"dev"}
  GET /health/ready  {"status":"ready","database":"up","detail":null}
```

The init migration mirrors TIKTOK's M0 — prove the pipeline runs end to end
before there is a schema to migrate.

### Still open

- **Railway deploy** — the last P0 item; needs the Railway dashboard.
- **Notion tracker** — the MCP connector worked once, then dropped and would not
  return across several restarts. Full rebuild spec written to
  `docs/NOTION_WORKSPACE.md` for a session that does have access.
- **Meta business verification** — developer account approved; verification
  pending. P2 onward waits on it. **Soko Intel does not**, which is why it can
  run in parallel.

---

## 2026-08-08 · P1 scope change and the data model

### Scope: three ingestion paths, not one

The plan assumed one front door — sync a TikTok handle. That fails two-thirds of
real sellers, so P1 now carries three:

| Path | Seller does | System does |
|---|---|---|
| Profile sync | enters `@handle` | Apify pulls the feed → AI drafts each video |
| Single link | pastes one TikTok URL | same pipeline, one item |
| Manual upload | uploads a photo | no scrape; AI drafts from the uploaded image |

The third reuses the vision agent unchanged — it does not care whether an image
came from a TikTok cover or a camera roll. Same code, new door.

### Frontend decided: Jinja2 + HTMX + Tailwind

Not Next.js, despite TIKTOK having a working one. Keeping the whole application
in one language and one deploy is the reason Python was chosen; HTMX covers
inline editing, bulk actions, upload progress and live filtering without a JS
framework or a second debugging context.

Two surfaces from one stack: the buyer storefront (mobile-first, minimal JS, also
the SEO surface) and the seller dashboard (the working surface, draft review
queue first).

### The data model — rails in the database, not in services

`Seller`, `Product`, `ScrapeJob`, plus a `models/enums.py`.

**Why the constraints live in Postgres rather than service code:** a model, a
future agent, a migration script and a hand-written query all have to obey the
database. Only application code has to *remember* to call a validator.

Fifteen CHECK constraints landed. The ones that carry weight:

| Constraint | Prevents |
|---|---|
| `published_requires_price` | the AI pushing an unpriced item live |
| `stock_non_negative` | overselling, at the storage layer |
| `price_positive` | a KSh 0 product — always a parse failure, never a giveaway |
| `price_plausible` (≤ 10,000,000) | the phone-number misparse reaching a storefront |
| `manual_has_no_video_id` | a manual product looking re-syncable |
| `published_needs_whatsapp` | a live shop with no way to contact the seller |
| `slug_format` | a URL a buyer cannot type |
| `failed_needs_error` | a failed scrape nobody can debug |

**Design decisions worth keeping:**

- **Enums stored as strings with CHECK constraints**, not native PG enum types.
  Adding a value to a native enum needs a migration and an exclusive lock;
  adding one here is a code change. For categories, which will grow, that
  matters more than the bytes saved.
- **`tiktok_video_id` is nullable and unique.** Postgres permits multiple NULLs
  in a unique index, so manual products coexist with no partial index needed.
- **`price_source` records which cascade tier produced the price.** This is the
  feedback signal for the whole approach: if tier 3 rarely fires, the expensive
  path is not paying for itself. Without the column that is unanswerable.
- **`raw_payload` on ScrapeJob is kept deliberately** — it lets a changed parser
  be replayed against real data for free, and it is the evidence when a seller
  disputes an extraction.
- **`Seller.tiktok_handle` is nullable.** A manual-upload-only seller is
  legitimate; TikTok is an option, not a requirement.

### Testing the rails

`tests/test_models.py` — 24 tests, and most assert that Postgres **refuses**
something. A constraint nobody has watched fail is only a claim.

Autogenerate produced the migration; all 15 CHECK constraints carried through
and were verified by reading the migration before applying it.

### One fixture bug found by its own warnings

The rail tests emitted 13 × `SAWarning: transaction already deassociated from
connection`. Cause: tests deliberately trigger `IntegrityError`, Postgres aborts
the transaction, and the fixture then tried to roll back something already gone.

Fixed with an `is_active` guard. Isolation was never affected — each test gets a
fresh connection either way — but a warning on every rail test trains you to
ignore warnings, which is how the real one gets missed.

### Known cost: the test suite is slow

42 tests take ~49 seconds, almost entirely network round trips to Railway. Fine
now; it will hurt at 200 tests. The fix when it does is a local Postgres for
tests — the shared-server choice bought zero setup at the price of latency.

### Verification

```
ruff · format · mypy strict   clean
pytest                        42 passed, 0 warnings
alembic                       3bbf0cbc3f71 applied on top of f33154a36521
```

---

## 2026-08-08 · Spikes against a real seller — @zumamitumbabales

Test subject: a live Kenyan mitumba (second-hand clothing) seller. 22,000
followers, 1,453 videos, selling ladies' sandals in bulk.

Four spikes, each writing its raw output to `spikes/out/` so every later
question is answerable offline and for free.

### The cascade, measured on real data

| Tier | Source | Yield | Cost |
|---|---|---|---|
| 1 | Caption | **0/10** | ~free |
| 2 | Cover image | **0/4** | cheap |
| 3 | **Video, audio + visual** | **3/3** | expensive |

**The cascade is not a nice-to-have — for this seller it is the only thing that
works.** Tiers 1 and 2 returned nothing at all. Had we built only caption
parsing, this seller would have been uncatalogable.

Tier 2 is still worth keeping: it costs a fraction of tier 3 and other sellers
do print prices on covers. Ordering by cost is the entire point.

### Tier 2 failed *correctly*

Gemini identified the products confidently (0.90–0.95: "Mixed Ladies Sandals",
"Assorted ladies sandals available for wholesale purchase in bulk lots") and
returned `price_kes: null` on every one. It did **not** invent a plausible
price. The guardrail — *never guess, a wrong price is worse than a missing one*
— held under real conditions.

### The finding that changed the data model

Tier 3 read the price from all three clips, and the `price_heard_as` field —
added so a human could verify the model's reading — showed why that matters:

```
"@3000 30pairs"    "3000 for 30 pairs"    "3000 30pairs shown on screen"
```

**This seller prices in bulk lots, not units.** KES 3,000 buys *thirty pairs*.
The account is literally named ZUMA MITUMBA **BALES**.

A storefront rendering "Mixed Ladies Sandals — KES 3,000" would make a buyer
expect one pair. That is a trust-destroying error on the single field that must
never mislead — and the `Product` model designed two hours earlier had no way to
express it.

Added, driven entirely by the data:

- `unit_quantity` — 30
- `unit_label` — "pairs", in the seller's own vocabulary, not normalised
- `price_evidence` — the exact words the price was stated in, kept as the audit
  trail behind an AI-read number. It is what revealed bulk pricing at all.
- `price_display` — renders "KES 3,000 for 30 pairs", never the bare number
- Two constraints: a lot size must be positive, and quantity and label must be
  present together (*"KES 3,000 for 30" of what?*)

**This is the whole argument for spiking before schema, demonstrated on
ourselves.** The model was reasonable, well-documented, fully tested — and wrong
about a real seller in a way no amount of thinking would have surfaced.

### Other findings worth keeping

- **No TikTok transcriptions.** `transcriptionLink` and `subtitleLinks` were
  null on 10/10, matching TIKTOK's spike 00. Tier 3 must send actual video;
  there is no cheap text shortcut.
- **Cover URLs are signed and expire.** We must store our own copies, as
  designed.
- **The bio carries phone numbers.** `signature` held
  *"...contact us 0105515839/0754234636/0762508007"* — so `whatsapp_number` can
  be pre-filled at onboarding and merely confirmed. One less field to type.
- **Clips are short** — 9–24s, mean 13.8s. Tier 3 is affordable per item, but
  must stay cached once-ever per video id.
- **1,453 videos on this profile.** A full import is ~$0.44 in Apify alone, and
  most of it is stale stock. Initial sync must be capped at the recent N, not
  the whole history.
- **Hashtags carry category, never price** — `#sandalsforwomen`, `#zarasandals`,
  `#kidscrocs`. Useful as a hint to the vision model, worthless as a price source.
- **Engagement baseline is thin at 10 posts** (mean 563 views, no 3× outliers).
  Soko Intel's outlier detection needs a fuller feed to mean anything.

### Three failures along the way

**1. `gemini-2.5-flash` is retired.** 404 *"no longer available to new users"*.
Switched to `gemini-3.6-flash`, which is also what TIKTOK validated on for
Sheng/Swahili. Model ids in config are a maintenance surface, not a constant.

**2. Apify's downloaded videos are not public.** `mediaUrls` points into Apify's
key-value store and 403s without the API token. My spike fetched a 214-byte JSON
error and posted it to Gemini **as a video** — a paid call spent on garbage,
with no complaint. Fixed by adding the token, and by refusing to send anything
whose content-type is not video or that is implausibly small. Exactly the silent
failure our own standard bans, committed by me.

**3. `create_all` does not migrate.** The session fixture created missing tables
but never altered existing ones, so adding three columns left the test database
stale and produced 25 failures with a baffling *"column unit_quantity does not
exist"*. Now drops and recreates, which cannot drift. Safe because
`TEST_DATABASE_URL` is refused if it equals the app database.

### Verification

```
ruff · format · mypy strict   clean
pytest                        50 passed
alembic                       588f6c5577d0 (head)
```

---

## 2026-08-08 · Ingestion and the cascade, in production code

The spikes are now the specification. Three layers, each with one job.

### `schemas/tiktok.py` — the validation border

Apify's response is a third-party contract we do not control. Validating once,
at the edge, means a shape change fails loudly with a readable error instead of
surfacing three layers later as a `None`.

The models are **tolerant of extra fields and strict about the ones we depend
on**. Apify sends 28 top-level keys; we need nine. A field they add can never
break us; one they remove that we rely on does — which is the right way round.

Field names come from the real payload, not from documentation.

### `services/scraper.py` — the adapter

Callers depend on `ScraperEngine`, never on Apify. Swapping the engine is a
change to one file. TIKTOK proved the value of this seam by swapping vision
providers three times through an equivalent one.

`download_media()` refuses anything whose content type is not what was asked
for. That guard exists because a spike fetched a 214-byte JSON error from
Apify's key-value store and passed it to Gemini **as a video** — a paid call
spent on nothing, silently.

`DEFAULT_PROFILE_LIMIT = 30`, because the spike account had 1,453 videos. A full
import is ~$0.44 of Apify for one seller, mostly stale stock.

### `agent/draft.py` — the provider seam

The only file that knows which vision provider we use. Constrained decoding
against `ProductDraft`, so the output can only be a valid instance.

One guard worth naming: **constrained decoding guarantees the SHAPE, never the
SENSE.** A model can still return a phone number in a price field. The agent
rejects an implausible price itself, so the failure names the draft rather than
arriving as an IntegrityError from three layers down.

### `services/drafting.py` — where the economics live

The agent knows how to ask a model; this knows how much we are willing to pay to
find out. Conflating them is how a cost control ends up buried in a prompt.

- Tier 1 (caption) is **skipped as a price source on purpose** — 0/10 measured,
  and every caption already reaches the model as hashtag context. A separate
  text call would spend money re-reading what tier 2 sees anyway.
- A confident tier-2 price **stops** the cascade.
- A failed tier does **not** abort it — the next tier may still rescue the item —
  but the failure is recorded, never swallowed.
- `allow_video_tier=False` exists for bulk imports and over-quota sellers.
- `tiers_attempted` is recorded so we can ask later whether the expensive tier
  earns its cost. Unrecorded, that question is unanswerable.

### Testing what actually matters here

The cascade tests protect **cost**, not just correctness. A regression that
quietly escalated every item would pass a naive correctness test and arrive as a
bill — so the tests assert which tiers ran, not only what came out.

Scraper fixtures are built from the real captured payload. A test that passes
against a payload we imagined proves nothing about the one the provider sends.

### Verification

```
ruff · format · mypy strict   clean
pytest                        92 passed
```
