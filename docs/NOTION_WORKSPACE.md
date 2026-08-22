# Notion Workspace — build/update specification

> **Purpose.** The Notion MCP connector keeps dropping, so this file carries
> everything Notion needs. Hand it to a Claude session that *does* have Notion
> access, or work through it by hand.
>
> **Revised 2026-08-16** for the growth-engine pivot.
>
> **Do not create new databases.** They exist — IDs below. The job is to update.

---

## Existing workspace

Workspace **Bonganabob's HQ** · account `hello@bonganabob.com`
Hub page: **Biashara Mall — Mission Control** — `3b3772fd-4e22-8153-8f7d-e305ab5ba18e`

| Database | Data source ID |
|---|---|
| Projects | `3b2772fd-4e22-808b-8932-000b69754912` |
| Tasks | `3b2772fd-4e22-8066-b77a-000b98667aa5` |
| Docs | `3b2772fd-4e22-803c-a78b-000b9b007caf` |
| Decisions | `ff8cebf7-0994-4bb5-818a-f4d2ffda97e8` |
| Backlog | `6bd6e3e1-7b3b-455c-9e75-ad51619234ca` |

**Tasks schema:** `Name` (title), `Status`, `Due date`, `Project` (relation),
`Milestone No.` (number), `Branch` (text), `PR` (url), `Milestone` (checkbox).

---

## What changed, and why

The plan built the store first. **A store nobody can find is worth nothing**, and
a new storefront has no audience to send to it.

So the order reverses: lead with an **AI content and growth engine** that solves
a daily, felt pain, and let the store be what that audience graduates into. The
live site (`biasharamall.com`) already positions it exactly this way — AI Content
Engine available now, Store as the vision.

**Nothing built is discarded.** The Apify adapter, the Gemini seam, ownership
verification, auth, the models and the working storefront all transfer. The
storefront moves from "the product" to "what Growth users graduate into".

---

## Step 1 — Projects

Rename the three pillars to match the live brand and the new order.

| Row ID | New name | Stage | Body |
|---|---|---|---|
| `c351e0cc-bd13-4ac5-8fdb-3bd8c40b1f7a` | **Biashara Commerce** | Planning | The store. Catalog · Orders · Payments · WhatsApp Storefront. Largely built — storefront and handoff work. Now what Growth users graduate into, not the entry point. |
| `05a6afb4-5c1b-4935-8b74-2eeade871584` | **BiasharaIntel** | In Progress | Analytics and competitor intelligence. **The paid tier.** Own-account trends, niche discovery, benchmarking, outliers. Feeds the hook corpus. |
| `3b7772fd-4e22-81e4-9261-ea0ecd5857fb` | **AI Content Engine** | In Progress | Hooks · Scripts · Captions · CTAs · Content ideas. The reason people sign up. Grounded in the hook corpus — the moat. |

---

## Step 2 — Tasks

**Retire** the P0–P9 rows (prefix `[Retired — commerce-first plan]`, set Done).
Keep P0/P1 marked Done rather than retired: that work shipped and still stands.

Create these. Statuses: G0 `In progress`, rest `Not started`.

| No. | Name | Project | Branch |
|---|---|---|---|
| 0 | G0 — Dashboard foundation | AI Content Engine | `g0-dashboard` |
| 1 | G1 — My Analytics | BiasharaIntel | `g1-analytics` |
| 2 | G2 — BiasharaIntel: competitors | BiasharaIntel | `g2-intel` |
| 3 | G3 — Hooks, scripts and captions | AI Content Engine | `g3-content` |
| 4 | G4 — Reports and the weekly nudge | AI Content Engine | `g4-reports` |
| 5 | C1 — Catalogue dashboard | Biashara Commerce | `c1-catalogue` |
| 6 | C2 — Storefront polish | Biashara Commerce | `c2-storefront` |
| 7 | C3 — WhatsApp channel ⏳ | Biashara Commerce | `c3-whatsapp` |
| 8 | C4 — Orders and payments 🏁 | Biashara Commerce | `c4-payments` |
| 9 | P — Hardening and pilot 🏁 | Biashara Commerce | `p-pilot` |

### Task bodies

**G0 — Dashboard foundation**
- Signup / login / logout routes and pages (Jinja2 + HTMX + Tailwind)
- Session cookie handling, `HttpOnly`, `current_account` dependency
- Dashboard shell: navigation, empty states, account menu
- Connect-account flow with bio-code verification wired to real screens
- Apply the naming decision (Biashara Mall → Biashara Mall, if taken)

*Done when:* a stranger signs up in a browser, verifies a TikTok account, and lands on a dashboard.

*Note:* the auth **service layer exists and is tested — there are no pages.** Today a seller can only be created from Python. This is the gap.

**G1 — My Analytics**
- Metric snapshots: views, likes, comments, shares, followers — captured per sync **as history**
- Account trend: followers and total views over time
- Post table: best and worst performers, sortable
- "Your average" baseline that everything else is measured against

*Done when:* a verified creator sees their own numbers, and a second sync a day later shows a visible trend.

⚠️ **START THE TIME SERIES IMMEDIATELY.** `Product.views` is a single number overwritten each sync. It answers "how many views?" but not **"is my traction growing?"** — which is the whole question a creator pays for. A `PostMetricSnapshot` table must start filling the day this ships: **history cannot be backfilled**, and every day without it is data we can never recover.

**G2 — BiasharaIntel: competitors**
- **Spike first, and it opens the milestone:** which Apify actor does keyword/hashtag creator search, what it returns, **what one search costs**
- Keyword/hashtag → creators in that niche
- Benchmark table against the creator's own G1 baseline
- Outlier detection — the posts worth studying, and G3's input
- **Persist results into the hook corpus from day one**, before generation exists

*Done when:* a Growth user searches a keyword and sees a ranked benchmarked list — and the same search again costs nothing.

⚠️ **Three cost guards, all required:** Growth tier only (in code, not the UI); per-seat monthly quota; result caching. Every guard is guesswork until the spike produces a real per-search cost.

**G3 — Hooks, scripts and captions**
- Hook corpus from G2's outliers, tagged by niche, language register, performance
- Hook generation grounded in the corpus + the creator's own niche
- Script generation with a shot list
- Caption + CTA generation in the register the niche actually uses
- Content ideas as a **queue**, not a one-shot — the question is "what do I post this week"
- Feedback loop: which hooks got filmed, and how they performed

*Done when:* a creator gets a hook, script and caption referencing real, recent, local performance — and can film from it without editing.

**This is the moat.** Hooks are culture- and language-bound; an English-market "viral formula" does not transfer to Nairobi. Searches feed the corpus, the corpus improves generation, better generation sells seats. A competitor can copy the feature but not the corpus.

**Grounding, not training** — real hooks retrieved into the prompt as examples. Fine-tuning is slower, costlier, needs data we will not have, and does not improve the instant a new hook lands.

**G4 — Reports and the weekly nudge**
- Weekly digest: your traction, one niche trend, one ready-to-film hook
- Client-facing performance report
- Email now; WhatsApp when Meta verification clears

**C1 — Catalogue dashboard**
The one genuinely missing piece of commerce. Ingestion, the price cascade, the storefront and the WhatsApp handoff all work.
- Draft review queue — **low-confidence parses first**, because that is where a wrong price hides
- Inline edit of price, title, sizes, stock without a reload
- Publish gate visible and explained — a disabled button that says why
- Manual upload path (photo → same vision agent)
- Single-link ingestion (the scraper supports it; nothing calls it)

**C2 — Storefront polish**
Built and browsable. Remaining: OG metadata for shared links, empty/error states, the shop page as an SEO surface.

**C3 — WhatsApp channel** ⏳ *Gated on Meta business verification — their clock.*
Webhook + signature verification + **idempotency** (Meta redelivers). Conversation state, signed storefront links. Twilio evaluated if Meta stalls.

**C4 — Orders and payments** 🏁
Order state machine, Daraja client, **idempotent callback** — callback is truth. Checkout survives the STK prompt interrupting the browser.

**P — Hardening and pilot** 🏁
Rate limits, AI cost caps, secrets audit, incident-grade logs. Real Kenyan creators. One real order end to end.

---

## Step 3 — New Decisions rows

**1. Lead with the growth engine, not the store** — *Product · 2026-08-16*
> A store nobody can find is worth nothing, and a new storefront has no audience to send to it.
>
> A creator who needs to know what to post **today** needs it today; a creator who needs a store needs it eventually. Content is a daily felt pain with an obvious did-it-work signal. Commerce is a decision made once you already have attention to convert.
>
> So the product becomes an AI content and growth engine that ends in a store, rather than a store that offers content tools. Nothing built is discarded — the storefront becomes what Growth users graduate into, which is how the live site already positions it.

**2. Intel is the paid tier, at $10/month** — *Product · 2026-08-16*
> Published on biasharamall.com: Starter free forever (WhatsApp store + M-Pesa), Growth $10/month (AI content engine + BiasharaIntel + priority support).
>
> This settles a question the old plan left open. It matters because Intel is also the only feature a user can trigger repeatedly at will, so **cost now tracks revenue by design rather than by hope**.
>
> The economics still need proving: at $10/month a user running unlimited niche searches and video-tier drafts can cost more than they pay. G2 opens with a spike that measures the real per-search cost, because every guard is guesswork until that number exists.

**3. Capture metric history from day one** — *Data model · 2026-08-16*
> `Product.views` is a single number overwritten on every sync. It answers "how many views does this have?" but not **"is my traction growing?"** — which is the entire question a creator is paying to answer.
>
> A `PostMetricSnapshot` table (post, metric, value, captured_at) must start filling the day G1 ships. **History cannot be backfilled.** Every day without it is a day of data we can never recover.
>
> Same argument as the hook corpus, and it applies sooner.

**4. Canva and CapCut are destinations, not integrations** — *Architecture · 2026-08-16*
> The flow diagram shows Canva and CapCut, and it would be easy to read those as things we build.
>
> We produce the words and the plan — hook, script, caption, shot list, CTA — and the creator takes them into whichever tool they already use. Canva's API is limited and CapCut has no public one; building editor integrations is a separate and much larger project.
>
> Stated explicitly so nobody assumes it is in scope.

---

## Step 4 — Backlog additions

| Item | Category | Priority |
|---|---|---|
| Canva / CapCut integrations | Idea | Low |
| Fine-tuning on Kenyan hooks | Idea | Low |
| Twilio as WhatsApp fallback | Feature | Medium |
| Free-tier Intel allowance | Feature | High |

**Free-tier Intel allowance** — a free tier with zero Intel may not convert; one with unlimited Intel cannot pay for itself. A small monthly allowance is the obvious middle, but the number needs the G2 cost spike first.

---

## Step 5 — Docs

Update **Production Plan** with the revised content from `docs/PRODUCTION_PLAN.md`.
Update **Codebase** — the storefront, ingestion, media and verification all shipped since it was last written.
Add a **Naming decision** doc if the rename is taken.
