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
