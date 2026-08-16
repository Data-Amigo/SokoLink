# Biashara Mall — Production Plan

> **Revised 2026-08-16.** Growth engine first, store second. Web platform first,
> WhatsApp when it is ready and not before.
>
> Mirrored into Notion via `NOTION_WORKSPACE.md`.

---

## 1. The direction

A store nobody can find is worth nothing. A creator who needs to know what to
post **today** needs it today; a creator who needs a store needs it eventually.

So we lead with the urgent problem and let the audience for the deferred one
build itself.

```
Idea ──▶ Intel ──▶ Hook ──▶ Script ──▶ Design ──▶ Video ──▶ Store
         │          │                    │          │
    what's working  what to say        Canva      CapCut
```

**Naming settled and applied:** the product is **Biashara Mall**
(`biasharamall.com`) — the sokolink domain was already taken. The rename landed
across the codebase, databases, docs and CI on 2026-08-16.

**WhatsApp is not a dependency.** The web platform stands alone: creators sign
up, connect an account, see analytics, get hooks and scripts, and run a
storefront — all in a browser. WhatsApp is added later as a *channel*, not as
the foundation.

---

## 2. What is already proven

Not assumed — measured, with saved evidence in `backend/spikes/out/`.

### Own-account analytics: **fully confirmed** (spike 05)

Every metric the product promises is already in the payload we pay for anyway:

| Metric | Status |
|---|---|
| Views per post | ✅ 10/10 posts |
| **Comments per post** | ✅ 10/10 posts |
| Likes, shares, saves per post | ✅ 10/10 posts |
| **Followers** | ✅ 22,000 |
| Total likes, post count, following | ✅ |

**"My Analytics" needs storage and a chart, not a new integration.**

### Competitor discovery: **confirmed** (spike 06)

A live search for `mitumba` returned **8 distinct creators from 10 results**,
with follower counts for every one — so benchmarking needs **no second call per
creator**, which is the cheapest possible outcome.

| Creator | Followers | Views on one post | Views ÷ followers |
|---|---:|---:|---:|
| @aliciasbales_gunias | 316,000 | 2,200,000 | 7× |
| @topgrademitumba | 10,100 | 179,100 | 18× |
| **@mitumba.business** | **92** | **4,247** | **46×** |

The actor also accepts `hashtags` and a `searchSection` that can target
**profiles** directly.

> ### The finding that changes the feature
>
> The 92-follower account with a 46× ratio is the most interesting row in that
> table, and a naive "top posts" list would bury it beneath the 316k account.
>
> **Outliers must be measured relative to the creator's own size, not in
> absolute views.** A 316k account getting 2.2M views mostly has reach. A
> 92-follower account getting 4,247 views *did something right*, and it is
> something a beginner can copy. That is the insight worth paying for — and it
> is what the hook corpus should harvest.

### Cost

10 results ≈ $0.003. At roughly $0.30 per 1,000 posts, a 50-result search is
about **$0.015** — so 100 searches a month is ~$1.50 against a $10 subscription.
Comfortable, but the real figure must be read off the Apify console after a
week of live use, not extrapolated from one run.

---

## 3. Canva and CapCut — verified, both in

Both were checked against the real thing, not from memory. An earlier draft of
this plan wrote Canva off; that was wrong, and the correction is recorded here
because the reasoning matters more than the conclusion.

### Canva: IN, via the MCP server

The mistake was evaluating **Autofill** and stopping. Autofill fills pre-made
brand templates and *does* require Enterprise — but that is not the feature we
want.

**Design generation from a text description is a different capability, and it is
available on all Canva plans.**

| Capability | Plan required |
|---|---|
| **Design generation from text** | **All plans** ✅ |
| Design editing by natural language | All plans |
| Export | All plans |
| Design resizing | Canva Pro+ |
| Autofill / brand templates | Enterprise — *not needed* |

So the vision works exactly as intended:

```
"Poster for my shoe business, KSh 2,500, delivery in Nairobi"
        │
   Biashara Mall ──▶ Canva MCP ──▶ user's own Canva account
        │
   editable design the creator can change however they like
```

**Each user authenticates individually** with their own Canva account — free is
fine. We never act on one shared account, which is also the right security
posture: their designs stay theirs.

**The one real constraint is timing.** Access is a **waitlist with a review**,
not a self-serve key — Canva assesses brand alignment, trust and safety,
compliance readiness, technical fit and strategic alignment.

> **Apply on day one of Phase 0.** It costs nothing and starts a clock we do not
> control. This is the same lesson WhatsApp taught: approvals run on someone
> else's schedule, so the application goes in long before the code needs it.

Phase 3 ships hooks, scripts and captions without Canva. Design generation
lands as soon as access does, and nothing before it is blocked.

### CapCut: IN, and more capable than expected

You were right that it deserved a try rather than a verdict. `capcut-cli` was
installed and run. What it actually does:

| Command | What it does |
|---|---|
| `init` | Create a draft from scratch |
| `add-video` · `add-audio` · `add-text` | Build the timeline |
| `caption --audio` | Auto-caption from audio |
| `import-srt` · `export-srt` | Subtitle interchange |
| `cut` · `detect-scenes` | Long-form → shorts, with scene detection |
| `render` | ffmpeg **proxy preview** |
| `export-timeline` | **OpenTimelineIO** |

Actively maintained — created April 2026, **published the same day this was
written**.

**The one thing to be precise about:** `render` is a low-res ffmpeg *proxy* so a
creator can watch the cut without opening CapCut. Its own help says it is "NOT
CapCut's final render (no multi-track compositing/effects)". The finished export
still happens in CapCut, on the creator's machine.

So the realistic flow is **assembly, not rendering** — and that is still most of
the value:

```
script ──▶ capcut-cli builds the draft ──▶ creator opens it in CapCut
           timeline, text, captions,          and hits export
           timing, scene cuts
```

The creator opens a **fully assembled timeline** instead of a blank project.
That is the difference between "here is a script" and "here is your video, check
it".

**Two honest risks:**

1. **Unofficial.** ByteDance publishes no video API; this tool reads and writes
   CapCut's private `draft_content.json`. That format can change without notice
   and break us. It says so itself: *"Not affiliated with ByteDance."*
2. **Drafts are local files.** For a web platform the creator downloads a draft
   and imports it, or runs a helper locally. That is friction worth designing
   around rather than pretending away.

> **The hedge is already in the tool: `export-timeline` emits OpenTimelineIO**, a
> vendor-neutral interchange format read by Premiere, DaVinci and others.
> Generating OTIO as the canonical output and treating CapCut as one target
> means a format change costs us one adapter, not the feature. Same adapter
> reasoning as `ScraperEngine`.

---

# The phases

Each phase ends with something a real creator can use. Nothing waits on Meta.

---

## Phase 0 — Rename and dashboard shell `p0-biashara`

*Goal: it is called the right thing, and a stranger can sign up in a browser.*

**Why first:** the auth service layer is built and tested, but **there are no
pages** — today a seller can only be created from Python. That is the single
thing blocking every other phase from being testable by a human.

- Rename Biashara Mall → Biashara Mall across code, docs, database, tests, repo
- Signup / login / logout routes and pages (Jinja2 + HTMX + Tailwind)
- Session cookies (`HttpOnly`), `current_account` dependency
- Dashboard shell: navigation, empty states, account menu
- Connect-account flow with bio-code verification, wired to real screens

**Done when:** you sign up at a URL, verify a TikTok account, and land on a dashboard. **No Python involved.**

---

## Phase 1 — My Analytics `p1-analytics`

*Goal: a creator sees their own numbers, and whether they are growing.*

Everything here is confirmed available by spike 05.

- **`PostMetricSnapshot` table** — post, metric, value, captured_at
- Daily sync writes a snapshot per post (the existing cooldown already caps cost)
- Account trend: followers and total views over time
- Post table: best and worst performers, sortable
- "Your average" baseline — the number everything else is measured against

> ⚠️ **Start the time series on day one of this phase.**
>
> `Product.views` today is a single number, overwritten every sync. It answers
> "how many views does this have?" but not **"is my traction growing?"** — the
> entire question a creator is paying to answer.
>
> **History cannot be backfilled.** Every day without snapshots is a day of data
> gone permanently. This is the one thing in the plan that is more expensive to
> delay than to do.

**Done when:** a creator opens Analytics, sees their real numbers, and a second sync the next day produces a visible trend line.

---

## Phase 2 — Competitor Intel `p2-intel`

*Goal: a creator sees who is winning in their category, and why.*

Confirmed working by spike 06.

- Keyword / hashtag → creators in that niche, with follower counts
- Benchmark table against the creator's own Phase 1 baseline
- **Outliers by ratio, not absolute views** — see §2. A small account punching
  above its weight is the actionable signal
- Save a niche as a watchlist, so the digest has something to report

**Cost guards, all three required:**

| Guard | Why |
|---|---|
| Growth tier only, enforced in code | Cost tracks revenue by design |
| Per-seat monthly quota | A paid seat is not an unlimited seat |
| Result caching by keyword | The same search must never be paid for twice |

**Done when:** a Growth user searches their category, sees ranked creators benchmarked against themselves, and the same search again costs nothing.

---

## Phase 3 — Hooks, scripts and captions `p3-content`

*Goal: the creator knows what to post, and can film it today.*

- **Hook corpus** — hooks harvested from Phase 2 outliers, tagged by niche,
  language register (English / Swahili / Sheng) and the ratio that earned the place
- Hook generation grounded in that corpus *and* the creator's own niche
- Script generation with a shot list
- Caption + CTA in the register the niche actually uses
- **Content ideas as a queue**, not a one-shot — the real question is "what do I
  post this week", not "give me a hook"
- Feedback loop: which hooks got filmed, and how those posts performed

> **This is the moat.** Hooks are culture- and language-bound; an English-market
> "viral formula" does not transfer to Nairobi. Searches feed the corpus, the
> corpus improves generation, better generation sells seats. A competitor can
> copy the feature but not the corpus.
>
> **Grounding, not training** — real hooks retrieved into the prompt as
> examples. Fine-tuning is slower, costlier, needs data we will not have, and
> does not improve the moment a new hook lands.

**Done when:** a creator gets a hook, script and caption referencing real, recent, local performance — and can film from it without editing.

---

## Phase 3.5 — Design and video assembly `p3-design`

*Goal: the creator leaves with a poster and an assembled timeline, not just words.*

Runs as soon as Canva access lands — the application goes in at Phase 0, so this
is usually waiting on nothing by the time Phase 3 finishes.

**Design, via Canva MCP:**
- Connect-Canva flow, per user, their own account
- Text description → generated design, opened in their Canva to edit
- Designs generated from the same hook and offer the script uses, so the poster
  and the video say the same thing

**Video assembly, via capcut-cli:**
- Script + shot list → an assembled CapCut draft: timeline, text, captions, timing
- `detect-scenes` for long-form → shorts
- ffmpeg proxy preview so the creator can watch the cut in the browser before
  downloading
- **OpenTimelineIO as the canonical output**, CapCut as one adapter — so a
  private-format change costs one adapter, not the feature

**Done when:** a creator goes from a hook to a downloadable, assembled draft and an editable Canva design, without writing anything themselves.

---

## Phase 4 — Reports and retention `p4-reports`

- Weekly digest: your traction, one niche trend, one ready-to-film hook
- Client-facing performance report, for creators who sell services
- Email now; WhatsApp when the channel exists

**Done when:** a creator receives a Monday digest naming a real trend and a hook grounded in it.

---

## Phase 5 — Catalogue dashboard `p5-catalogue`

*The one genuinely missing piece of commerce.*

Ingestion, the price cascade, the storefront and the WhatsApp handoff link all
work already. What is missing is the screen where a seller reviews AI drafts.

- Draft review queue — **low-confidence parses first**, because that is where a
  wrong price hides
- Inline edit of price, title, sizes, stock, without a reload
- Publish gate visible and explained — a disabled button that says *why*
- Manual upload (photo → same vision agent)
- Single-link ingestion (the scraper supports it; nothing calls it)

**Done when:** a seller takes a product from any path to published, in a browser.

---

## Phase 6 — Storefront polish `p6-storefront`

Built and browsable. Remaining: OG metadata for shared links, empty and error
states, the shop page as an SEO surface.

---

## Phase 7 — WhatsApp `p7-whatsapp` ⏳

*Added when ready. Nothing above depends on it.*

- Cloud API webhook, signature verification, **idempotency** (Meta redelivers)
- Conversation state, signed storefront links
- Twilio evaluated as the alternative route

---

## Phase 8 — Payments `p8-payments` 🏁

Order state machine, Daraja client, **idempotent callback** — the callback is
the only payment truth. Checkout survives the STK prompt interrupting the browser.

---

## Phase 9 — Hardening and pilot `p9-pilot` 🏁

Rate limits, AI cost caps, secrets audit, incident-grade logs. Real Kenyan
creators. One real order, end to end.

---

## Pricing, as published

| Tier | Price | Includes |
|---|---|---|
| **Starter** | Free forever | WhatsApp store, M-Pesa checkout |
| **Growth** | $10 / month | AI content engine, BiasharaIntel, priority support |

Intel is the paid tier, which is right: it is also the only feature a user can
trigger repeatedly at will, so cost tracks revenue by design.

---

## Rules that do not change

- **The agent proposes, code disposes.** The LLM never decides price, stock,
  payment status or consent.
- **Publish is a human gate**, and still requires a price.
- **Only verified accounts can be synced** — and an unverified account cannot
  exist at all.
- **Spike before schema.** Apify proved this twice; CapCut is next.
- **Cache anything that costs money.**
- **Money is integer KES**, and the price is of one unit *as sold*.
- **No AI framework.** Provider SDKs directly, Pydantic as the guardrail.

---

## Do this on day one, before any code

Three approvals, all on someone else's clock. Each costs nothing to start and
weeks to wait for, so all three applications go in before Phase 0 writes a line.

| Apply for | Unblocks | Cost to apply |
|---|---|---|
| **Canva MCP waitlist** | Phase 3.5 design generation | Free |
| **Meta business verification** | Phase 7 WhatsApp | Free |
| **TikTok Login Kit review** | OAuth instead of bio-code | Free |

The WhatsApp lesson, restated: the application is not the work. Waiting is the
work, and it starts when you file.

---

## Open questions

1. **Does Starter get any Intel?** Zero may not convert; unlimited cannot pay
   for itself. A small monthly allowance is the obvious middle — the number
   needs a week of real cost data first.
2. **Gemini image generation as well as Canva?** Canva gives an *editable*
   design the creator owns, which is better. Gemini gives an instant image with
   no third-party approval. They may both be worth having, for different moments.
3. **How does a web app hand over a CapCut draft?** Download-and-import is the
   obvious answer, and it is friction. Worth designing deliberately in Phase 3.5.
4. **Twilio or Meta direct**, if WhatsApp approval stalls.

---

## Deliberately not in scope

- **Canva Autofill / brand templates** — Enterprise-only, and not needed. Design
  generation is the feature we want, and it is on all plans.
- **Server-side final video render** — no official CapCut API exists. We assemble
  the timeline; CapCut exports it.
- **SokoLedger** — on-device M-Pesa reconciliation. Later.
- **Fine-tuning** — ground first.
- **Marketplace search across sellers** — needs density that does not exist yet.
