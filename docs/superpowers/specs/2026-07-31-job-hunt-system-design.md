# Job Discovery & Application Prep System — Design

**Date:** 2026-07-31
**Status:** Approved design, pending implementation plan
**Source:** `IDEA.md` (research groundwork), revised through brainstorming

## 1. Purpose

A single-user system that discovers relevant software/cloud/embedded engineering roles daily, filters them against hard criteria (salary, location, role fit, Sharia business-activity screen), generates a tailored resume and cover letter for the best matches, and presents them in a private web UI where Jarra reviews and submits each application by hand.

**It is a discovery-and-preparation engine, not an auto-submit bot.** That is a deliberate constraint, not a limitation — see §3.

## 2. Goals and non-goals

**Goals**
- Surface 5–10 genuinely good, fresh roles per day with a match rationale.
- Eliminate the labor of tailoring documents and re-typing the same form answers.
- Track outcomes so the system's value can be measured.
- Run for under ~$30/month all-in.
- Carry zero risk of any job-platform account being suspended or banned.

**Non-goals**
- Automated form submission of any kind.
- Volume applying. The evidence in `IDEA.md` is that volume tracks a ~0.4% interview rate; specificity is what converts.
- Multi-user support. Single user, single process, forever.

## 3. The ban-risk boundary

**Rule: no automated process ever authenticates as Jarra on any job platform.**

This is the load-bearing constraint of the whole design, and it is enforced structurally rather than by policy:

- Discovery reads **public, unauthenticated JSON endpoints only**. There is no account attached to those requests, so there is nothing to suspend.
- **No browser is in the dependency tree.** Not Playwright, not Puppeteer, not headless Chrome. The dependency is absent, so it cannot be reintroduced by accident later.
- **Function instances have no writable persistent disk**, so no session cookie or credential store can survive an invocation even if one were somehow created. Moving to serverless made this guarantee stronger: it is now enforced by the platform, not only by our discipline.
- The UI's apply action is `<a href="{apply_url}" target="_blank">`. Jarra's own browser, own session, own click.
- `IDEA.md` Stage 3 ("cautious, narrow auto-submit") is **cut from the design entirely**.

The residual risk is not to accounts but to *reachability*: a cloud IP that hammers public endpoints can get throttled or IP-blocked, which silently degrades the system. On shared Vercel egress this is somewhat worse than on a dedicated IP, because the reputation is not ours alone. Mitigations in §7; the accepted consequences in §13.4.

## 4. Architecture

One Vercel project (Pro plan) running a single Python function, backed by Neon Postgres. There is no server to administer and no persistent local disk anywhere in the system.

```
 Vercel Cron ──► POST /api/cron/discover   */10 * * * *   enqueue + drain
        │        POST /api/cron/prepare    */2  * * * *   drain doc queue
        │        POST /api/cron/digest     5 13 * * *     06:05 PT digest
        ▼
   Neon Postgres (pooled endpoint) ◄──── FastAPI + Jinja2 + HTMX
        ▲                                       │
        │                                 Vercel Authentication
   Vercel Blob (generated PDFs)                 │
                                          laptop / phone, any network
```

Every route — UI and cron alike — is served by the same `FastAPI` instance. Vercel bundles a FastAPI app into exactly one function, so "the pipeline" and "the web app" are the same deployment artifact, sharing models and database code by direct import rather than over a wire.

### 4.1 Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.14 throughout | One runtime, one dependency manager, one mental model. Vercel supports 3.12–3.14; 3.14 is what the dev Mac runs, so local and deployed interpreters match exactly. Pinned in `pyproject.toml` — Vercel silently defaults to 3.12 if unpinned. |
| Web | FastAPI + Jinja2 + HTMX | Server-rendered; no client state. First-class Vercel target — Vercel resolves a `FastAPI` instance named `app` at `api/index.py`. |
| CSS | Tailwind v4 **standalone binary** | No Node.js, no npm anywhere. CSS built on the Mac and committed to `public/`. |
| DB | **Neon Postgres**, pooled endpoint | Serverless functions require pooled connections. Free tier, scales to zero, and branching gives staging a real forked dataset. |
| HTTP client | `httpx.AsyncClient` | Async fan-out across hosts is what makes the crawl fit a bounded invocation. |
| DB driver | `psycopg[binary]` v3, async | Binary wheels, no build step. Protocol-level prepared statements work through PgBouncer; SQL-level `PREPARE` does not. |
| Docs | `python-docx` + **`fpdf2`** | Pure Python — no Cairo/Pango, which cannot be installed in the Vercel runtime. Deterministic page count via `len(pdf.pages)`. See §10.2. |
| Blob storage | Vercel Blob | Generated PDFs need a durable home; the function filesystem is ephemeral. |
| Embeddings | `fastembed` (ONNX, `bge-small-en-v1.5`) | Local, $0, no API key, no PyTorch. Model vendored into the bundle at build time — see §13.3. Phase 2. |
| LLM | Anthropic only | Haiku 4.5 + Sonnet 5. One vendor, one key, one bill. |
| Access | Vercel Authentication, scope **All Deployments** | Pro unlocks production-domain protection. No auth code of our own to get wrong. |
| Email | **Gmail SMTP** via `aiosmtplib` | Jarra already holds an App Password. No vendor, no domain verification, no third party holding a sending key. Port 587/STARTTLS — Lambda blocks outbound 25 but permits 587. |

### 4.2 Why the `work_queue` table is the coordination mechanism

This table was originally justified by SQLite's single-writer constraint. That reason is gone; three stronger ones replace it, and all three are forced by the platform:

1. **Vercel never retries a failed cron, and delivery is best-effort in both directions** — a scheduled run can be silently skipped *or* delivered twice. Durable queue rows are what make a missed invocation recoverable and a duplicate invocation harmless.
2. **Invocations are time-bounded.** A run drains work until a wall-clock budget expires, then returns cleanly; the next tick resumes. Nothing has to fit in 800 seconds, so the duration ceiling stops being a design constraint.
3. **Concurrency control must be row-level.** Neon's pooled endpoint runs PgBouncer in transaction mode, which prohibits session-level advisory locks. Claiming work with `FOR UPDATE SKIP LOCKED` inside a transaction is the pattern that survives that constraint — and it is also how overlapping cron invocations avoid double-processing.

The claim is a single statement:

```sql
UPDATE work_queue SET status = 'running', started_at = now(), attempts = attempts + 1
WHERE id IN (
  SELECT id FROM work_queue
  WHERE status = 'pending' AND kind = %s
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT %s
)
RETURNING id, kind, payload;
```

The UI still never executes work directly — clicking "Prep" inserts a row and polls it via HTMX (`hx-trigger="every 2s"`), swapping in the preview when `status='done'`.

**Idempotency is a hard requirement, not an aspiration.** Because duplicate delivery is expected, every pipeline operation must be reconciliation-shaped. `upsert_job` matching on a content fingerprint already satisfies this: re-running a day converges to the same rows instead of duplicating them. Any operation added later that increments or appends rather than converges is a bug.

### 4.3 Schedules (`vercel.json` crons)

| Path | Schedule (UTC) | Work |
|---|---|---|
| `/api/cron/discover` | `*/10 * * * *` | Enqueue due sources, drain the fetch queue, normalize, prefilter, score |
| `/api/cron/prepare` | `*/2 * * * *` | Drain `prep_documents` — generate résumé + cover letter |
| `/api/cron/digest` | `5 13 * * *` | 06:05 America/Los_Angeles — build xlsx, send via Gmail SMTP |

Vercel cron schedules are **UTC only** and have no DST awareness. `5 13 * * *` is 06:05 PDT; during PST it lands at 05:05 local. The digest job therefore re-checks local time itself and no-ops if it fires outside a 06:00–07:00 America/Los_Angeles window, so the schedule stays correct across both halves of the year without a twice-yearly edit.

Each source carries its own interval in `targets.yaml`; the 10-minute tick is a clock, not a crawl frequency. Cron runs on production deployments only — staging is exercised with `vercel crons run <path>`.

## 5. Data model

PostgreSQL (Neon provisions **18.4** as of 2026-08-01; the local dev cluster is 16 and CI should be pinned to 18 to match). Nothing in the schema is version-specific — verified by applying both migrations to Neon. Timestamps are `TIMESTAMPTZ` throughout — a distributed system with UTC cron and Pacific-local business rules cannot afford naive strings.

```sql
-- Employers. Sharia verdict is cached here forever.
CREATE TABLE companies (
  company_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  domain          TEXT,
  ats_type        TEXT,           -- greenhouse|lever|ashby|smartrecruiters|...
  board_token     TEXT,
  sharia_verdict  TEXT NOT NULL DEFAULT 'unknown',  -- allowed|excluded|flagged|unknown
  sharia_sector   TEXT,
  sharia_reason   TEXT,
  sharia_source   TEXT,           -- blocklist|llm|user   (user always wins)
  sharia_decided_at TIMESTAMPTZ
);

CREATE TABLE jobs (
  job_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fingerprint   TEXT NOT NULL UNIQUE,   -- sha256(norm_company|norm_title|norm_city)
  company_id    BIGINT NOT NULL REFERENCES companies(company_id),
  source        TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  title         TEXT NOT NULL,
  location      TEXT,
  remote_type   TEXT,             -- remote|hybrid|onsite
  salary_min    INTEGER,
  salary_max    INTEGER,
  salary_source TEXT,             -- structured|parsed|none
  description   TEXT NOT NULL,
  apply_url     TEXT NOT NULL,
  posted_at     TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL,
  closed_at     TIMESTAMPTZ,
  filtered_out  BOOLEAN NOT NULL DEFAULT FALSE,
  filter_reason TEXT,             -- null iff filtered_out = false
  UNIQUE(source, source_job_id)
);
CREATE INDEX idx_jobs_live ON jobs(last_seen_at DESC) WHERE filtered_out = FALSE;

CREATE TABLE scores (
  job_id            BIGINT PRIMARY KEY REFERENCES jobs(job_id),
  embed_similarity  DOUBLE PRECISION,
  rule_score        DOUBLE PRECISION,
  freshness_score   DOUBLE PRECISION,
  total_score       DOUBLE PRECISION NOT NULL,
  relevance_verdict TEXT,
  rationale         TEXT,          -- the 2 lines shown in the UI
  is_stretch        BOOLEAN NOT NULL DEFAULT FALSE,
  model             TEXT,
  scored_at         TIMESTAMPTZ NOT NULL
);

-- The outcome feedback loop. Absent from IDEA.md; this is how we learn whether it works.
CREATE TABLE applications (
  app_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id            BIGINT NOT NULL UNIQUE REFERENCES jobs(job_id),
  status            TEXT NOT NULL,  -- queued|prepped|applied|responded|interview|offer|rejected|skipped
  applied_at        TIMESTAMPTZ,
  resume_url        TEXT,           -- Vercel Blob URL, not a filesystem path
  cover_letter_url  TEXT,
  notes             TEXT,
  updated_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE work_queue (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        TEXT NOT NULL,      -- fetch_source|prep_documents|classify_company
  payload     JSONB NOT NULL,
  status      TEXT NOT NULL,      -- pending|running|done|failed
  attempts    INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
-- Supports the FOR UPDATE SKIP LOCKED claim in §4.2.
CREATE INDEX idx_work_queue_claim ON work_queue(kind, created_at) WHERE status = 'pending';
-- At most one un-finished row per (kind, payload): makes enqueue idempotent under duplicate cron delivery.
CREATE UNIQUE INDEX idx_work_queue_dedupe ON work_queue(kind, payload)
  WHERE status IN ('pending', 'running');

CREATE TABLE answer_bank (
  key      TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  value    TEXT NOT NULL,
  category TEXT NOT NULL
);

CREATE TABLE unmapped_questions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id        BIGINT REFERENCES jobs(job_id),
  label         TEXT NOT NULL,
  field_type    TEXT,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMPTZ NOT NULL
);

-- Hard spend ceiling. Refuses calls past the daily cap.
CREATE TABLE llm_spend (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  day                 DATE NOT NULL,
  model               TEXT NOT NULL,
  purpose             TEXT NOT NULL,
  input_tokens        INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL,
  cost_usd            NUMERIC(10,6) NOT NULL,   -- exact; never float for money
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_spend_day ON llm_spend(day);

CREATE TABLE run_log (
  run_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at   TIMESTAMPTZ NOT NULL,
  finished_at  TIMESTAMPTZ,
  jobs_seen    INTEGER, jobs_new INTEGER, jobs_filtered INTEGER,
  jobs_scored  INTEGER, docs_generated INTEGER, errors INTEGER,
  duration_ms  INTEGER,  -- headroom against the invocation ceiling, measured not guessed
  budget_hit   BOOLEAN NOT NULL DEFAULT FALSE  -- true if the run returned on time budget with work left
);
```

Schema lives in `migrations/NNN_*.sql`, tracked in a `schema_version` table.

**Migrations do not run at request time.** A serverless function can be cold-started concurrently, so migrating on startup means racing migrations. They run once per deploy, from the GitHub Actions workflow, against the environment being deployed — before the Vercel deploy is promoted. Application code assumes the schema is already correct and never creates it.

**Repost handling.** Jobs get reposted with fresh IDs. `fingerprint` is content-derived, so a repost collapses onto the existing row and updates `last_seen_at` rather than appearing as new. `UNIQUE(source, source_job_id)` catches the exact-duplicate case.

## 6. Discovery layer

Each source is one module exporting a uniform interface:

```python
class Source(Protocol):
    name: str

    def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]: ...
```

`fetch` is an async generator so the orchestrator can stop mid-source when its
wall-clock budget expires, without the source having fetched everything first.

Adding a source is one file plus one registry entry. Normalization to the canonical `Job` shape happens in `normalize.py`, not in the source modules.

**No key required:** Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Workday CXS, HN Algolia, Remotive, RemoteOK, Arbeitnow, Himalayas, Jobicy, WeWorkRemotely, The Muse.
**Free, key required:** Adzuna, USAJobs.
**Explicitly excluded:** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound — all require authentication or violate ToS to automate. Per §3, they are out.

Target employer board tokens live in `profile/targets.yaml` (~150 to start).

**Measured against live boards, 2026-08-01.** Three assumptions in this section
turned out to need qualifying:

| Claim | What the APIs actually do |
|---|---|
| Ashby has reliable structured salary | Reliable **per employer, not per source** — publishing compensation is an opt-in Ashby setting. Ramp 95%, OpenAI 80%, Perplexity 82%, Vanta 62%; Linear, Notion, Cursor, ClickHouse and PostHog all 0%. Adapters must degrade to text parsing, not assume the field. |
| Greenhouse rarely publishes salary | Salary was parseable from prose for **79%** of Anthropic's postings. The regex parser carries more weight than expected. |
| A dead board token 404s | Lever and Ashby both answer **200 with an empty list** for retired boards, which is indistinguishable from "nothing open". Both adapters log a warning, because otherwise a dead token contributes nothing forever and nobody notices. |
| Aggregators can be filtered server-side | **Remotive's filter parameters do not work.** `?category=software-development`, `?category=devops` and `?search=engineer` all return results identical to the unfiltered endpoint. Passing one implies a filter that never ran, so the adapter sends none and lets the pre-filter do the work. |
| HN comments are job posts | Only **top-level** ones are — half the comments on a live thread were replies. And `whoishiring` posts "Who is hiring?" alongside "Who wants to be hired?" on the same day; the second is job *seekers*. |

Greenhouse and Lever return an entire board in one response — verified, no
pagination. Lever's `createdAt` is epoch **milliseconds**; Greenhouse
distinguishes `first_published` (posting date) from `updated_at` (any edit), and
freshness scoring needs the former.

## 7. Politeness and rate-limit safety

All HTTP goes through one `PoliteSession` wrapper (httpx):

- Per-host token bucket, default 1 req/sec with ±30% jitter.
- **Remotive publishes a rate expectation in its own API response** and it is stricter than our per-request delay: *"there is absolutely no need to request Remotive job data too frequently... we advise max. 4 times a day... excessive requests will be blocked."* The adapter declares `MIN_INTERVAL_SECONDS = 6h` for the orchestrator to honour. Their notice also requires attribution and a link back — we store Remotive's own `url` as the apply link and tag every row `source="remotive"`, and nothing is ever republished.

**Workday gets its own slower lane (1 req/2s)** — it rate-limits by source IP *across all tenants*, so 150 tenants from one egress IP is the realistic failure mode. On shared Vercel IPs we may also inherit a budget someone else already spent, which is why Workday stays deferred (§13.4).
- ETag / `If-Modified-Since` stored per URL; 304 costs nothing.
- Exponential backoff with jitter on 429/503.
- **Hard stop** on a 403 for a host: mark the source degraded, log it, surface it in the digest. Never retry into a block.
- Descriptive `User-Agent` with a contact address.

## 8. Scoring pipeline

Four gates, cheapest first, so the expensive stage sees very few jobs.

1. **Deterministic (free).** Salary floor $125k (Ashby's structured field when present, regex over description text otherwise); location rules (Remote > Hybrid > onsite ranked by proximity to San Leandro, CA); title/keyword families (Full-Stack/AI, Cloud/DevOps, Python/React/AWS/Docker, Computer Engineering, Biomedical/Embedded); freshness. Expected to kill ~70%.
2. **Local embeddings ($0).** `fastembed` cosine of JD against the resume vector.
3. **Sharia screen.** See §9.
4. **Haiku relevance + rationale.** Structured output via `client.messages.parse()` with a Pydantic model returning `{verdict, score, rationale}`. `thinking` disabled — this is classification, not reasoning.

**Ranking** weights `total_score` and `posted_at` together, with a strong boost for jobs posted under 48h. A ~5–10% random "stretch" allowance lets through above-level roles and adjacent pivots that pass every other filter.

## 9. Sharia screen

**Only the DJIM/AAOIFI *business activity* screen applies.** The financial-ratio screens (debt/market cap thresholds) exist for equity investing and are irrelevant to employment — applying them would wrongly exclude most leveraged companies. `IDEA.md` conflated the two.

Three tiers:

1. **Static blocklist** (domains + sector keywords) → `excluded`, hard drop, $0.
   Core exclusions: interest-based finance (banks, lenders, credit, mortgage, insurance), alcohol, gambling, adult content, weapons/defense, pork.
2. **Haiku classification**, cached per company forever, structured output `{verdict, sector, reason}`. Only companies not resolved by tier 1 and not already cached reach this.
3. **Gray zone → `flagged`, not dropped.** Surfaced in the UI with the reason visible; Jarra decides.

**`companies.sharia_source='user'` always wins and is never re-billed or re-evaluated.** An LLM must never silently make a religious ruling that cannot be corrected. Overrides are editable at `/settings`.

## 10. Document generation

Resume lives as `profile/resume.json` — structured, factual, hand-maintained.

**Per role:**
- **Haiku 4.5** rewrites the professional summary and reorders/re-emphasizes the skills section against the JD's keywords.
  **Fabrication guardrail:** employment history, dates, titles, employers, and metrics are copied through by code. The model receives them as read-only context and its output schema has no field that can alter them. There is no code path by which a model edit reaches a factual field.
- **Sonnet 5** fills Jarra's existing cover-letter template rather than writing prose from scratch. See §10.1.
- Both render `python-docx` → `.docx` and a declarative layout spec → `fpdf2` → PDF (§10.2).
- **One-page loop:** `len(pdf.pages)`; if > 1, regenerate with a reduced word budget (feeding back the current count), **max 2 retries**, then flag in the UI rather than loop forever.

**PDF is the primary artifact.** Its pagination is what gets verified; the `.docx` is an unverified fallback for the rare form that demands Word. Greenhouse, Lever, Ashby, and Workday all parse PDF correctly in 2026, and every submission is human-reviewed anyway.

ATS-safe formatting: single column, standard section headings, no tables/text boxes/headers-footers for content, standard fonts, no images.

### 10.1 Cover letter: slot-filling, not free composition

`Personalized Cover Letter.docx` is a structured template, not a sample letter. Its shape:

| Block | Handling |
|---|---|
| Header (name, title, phone, email, location) | **Fixed.** Rendered from `profile/identity.yaml`. |
| Date, hiring manager, company, address, `RE: [JOB TITLE], [JOB ID]` | **Mechanical.** Filled from the `jobs` row. Recipient lines are omitted when unknown rather than guessed — never invent a hiring manager's name. |
| ¶1 intro | **Boilerplate + 2 slots** (`[JOB TITLE]`, `[COMPANY NAME]`). Prose is fixed. |
| ¶2 CloudBase background | **Fixed.** Factual history — never model-touched. |
| "My experience aligns with this position in several areas:" | **Fixed.** |
| 4 labeled competency bullets | **The only generated content.** See below. |
| Closing (`[COMPANY NAME]`, `[TEAM OR DEPARTMENT NAME]`) | Boilerplate + 2 slots. |
| `Sincerely,` + name | **Fixed.** |

**The model's job is selection and re-emphasis, not invention.** `profile/competency_bullets.yaml` holds a pool of labeled bullets — the four in the template (Full-Stack Development, AI and Automation, Measurable Product Impact, Cloud and DevOps) are the seed, and more can be added over time (Embedded/Computer Engineering, Search & Ranking, Constraint Optimization — all backed by real resume content). Per job, Sonnet 5:

1. **Selects** the 4 most relevant bullets from the pool.
2. **Reorders** them by relevance to the JD.
3. **Rewords** each toward the JD's vocabulary, subject to a hard constraint: every technology, metric, and claim must already appear in the source bullet. Output is validated against the pool — a bullet mentioning a technology absent from `profile/` fails the render and is flagged in the UI.

This is a strictly better design than the "3 freeform paragraphs" in the original spec:

- **Voice is preserved structurally.** It is already Jarra's writing; the model edits rather than imitates. `profile/voice_samples.md` becomes unnecessary — the template *is* the voice sample.
- **Fabrication surface shrinks to near zero.** The model can only recombine pre-approved claims.
- **Cost drops sharply.** Output is ~4 short bullets (~250 tokens) instead of ~600 tokens of prose, and the fixed blocks sit in the cached prefix.
- **One-page fit is near-deterministic.** Only the bullets vary in length, so the retry loop in §10 rarely fires.

The `[TEAM OR DEPARTMENT NAME]` slot is filled only when the JD names a team; otherwise the closing sentence drops that clause rather than guessing.

**Existing layout is one-page-tuned and must be preserved:** margins 0.70″ left / 0.67″ right / 0.30″ top / 0.40″ bottom. The `fpdf2` renderer sets these exactly so the PDF matches the `.docx`.

### 10.2 PDF rendering: `fpdf2`, not WeasyPrint

WeasyPrint requires Cairo and Pango as native shared libraries. The Vercel Python runtime has no package manager, so using it would mean vendoring `.so` files into the bundle and keeping them working across runtime updates. That is a standing maintenance liability for a personal tool.

`fpdf2` is pure Python (its only binary dependency, Pillow, ships manylinux wheels), needs no system libraries, and gives the same thing the design actually depends on: an exact page count before the bytes are written.

The cost is real and worth stating: we lose CSS layout. But §10.1 changed what we need. Both documents are now **fixed-layout, slot-filled forms** — known margins, known section order, only the text inside slots varies. That is a case where imperative placement is *more* deterministic than a CSS engine, not less. Layout lives in `pipeline/docs/layout.py` as a declarative spec (margins, fonts, spacing, slot order) consumed by both the `fpdf2` and `python-docx` renderers, so the two artifacts cannot drift apart.

`profile/cover_letter.html.j2` is therefore replaced by `profile/cover_letter.yaml` — the same slots and fixed prose, without the HTML.

## 11. LLM cost controls

| Lever | Effect |
|---|---|
| Model routing: Haiku for tailoring + classification, Sonnet only for cover-letter prose | ~60% off doc-gen |
| Prompt caching on the static block (resume JSON + answer bank + voice samples + style rules) | 90% off cached input reads |
| Batch API on the 06:00 eager generation | additional 50% |
| Sharia verdicts cached per company forever | near-zero after ~2 weeks |
| `llm_spend` daily cap, enforced in the client wrapper | hard ceiling against a retry loop |

**Caching caveat that must be verified at build time:** Haiku 4.5's minimum cacheable prefix is **4096 tokens** (Sonnet 5's is 1024). If the static block is shorter than that, caching silently does nothing — `cache_creation_input_tokens` will be 0 with no error. Assert on that field in a startup check rather than assuming caching is working.

**Development phase:** eager generation **off**. On-demand prep only, for jobs Jarra would actually apply to. Flip eager on once ranking is trusted.

### Cost estimate

Per prepped job: Haiku tailoring (~3k in / 800 out) ≈ $0.007 + Sonnet 5 cover letter (~2k in / 600 out) ≈ $0.015, plus ~1.3× for page retries ≈ **$0.029**.
Haiku classification (~1.5k in / 200 out) ≈ **$0.0025**.

| | Monthly (approximate — verify at build) |
|---|---|
| **Vercel Pro** | **$20** — fixed, and the largest single line item |
| Vercel Functions (cron + UI, well inside Pro's included usage) | $0 |
| Neon Postgres (free tier: 0.5 GB, scales to zero) | $0 |
| Vercel Blob (a few hundred small PDFs) | $0, inside the included allowance |
| Gmail SMTP (existing account, 1 message/day against a ~500 limit) | $0 |
| Anthropic — build phase (lazy only) | ~$3–5 |
| Anthropic — steady state (eager + caching + batch) | ~$5–7 |
| **Total** | **~$23/month best case, ~$27 worst case** |

**This is more expensive than the EC2 design**, which came to ~$5–19/month. The $20 Pro subscription buys production-domain protection (a genuine security requirement, since the UI displays personal data), an 800s duration ceiling, per-minute cron, and the elimination of all server administration. That trade was chosen deliberately; it is not an oversight. Everything else in the system got cheaper or free.

The variable cost is still Anthropic, and every control in §11 still applies. If monthly spend needs to come down, the lever is eager generation volume — not infrastructure.

## 12. Web UI

FastAPI + Jinja2 + HTMX + Tailwind v4. Four routes.

| Route | Contents |
|---|---|
| `/` | Ranked queue. Cards: title, company, salary, location, score, "posted 6h ago", Sharia badge if flagged. |
| `/job/{id}` | JD, match rationale, inline PDF preview of both documents, downloads, **Open apply page** (new tab), copy-to-clipboard answer-bank panel, status buttons. |
| `/tracker` | Funnel: queued → applied → responded → interview → rejected. Weekly conversion stats. |
| `/settings` | Answer bank, Sharia overrides, target employer list, unmapped questions, run log with duration and budget-hit flags. |

Server-rendered; HTMX handles the prep-poll and status mutations. No client-side framework and no JavaScript build step, which is also why the deployment is a single Python function rather than two runtimes.

## 13. Infrastructure

No servers. One Vercel project, one Neon project, one private GitHub repository.

### 13.1 Environments

Two long-lived environments, mapped to branches:

| Environment | Branch | Vercel target | Neon branch | Cron |
|---|---|---|---|---|
| Production | `main` | Production | `production` (Neon's default) | Active |
| Staging | `staging` | Preview (aliased) | `staging` (forked from `production`) | Inactive — trigger manually |

Neon's branching is what makes staging honest: `staging` forks from production data copy-on-write, so the UI is exercised against realistically-shaped rows instead of an empty database. Refork it whenever it drifts; it costs nothing.

Cron runs only on production deployments. Staging is exercised with `vercel crons run /api/cron/discover`. This is a feature — you do not want two environments independently crawling the same hosts and doubling your request rate against a shared IP reputation.

### 13.2 CI/CD (GitHub Actions)

Vercel's native Git integration is **disabled**; deploys go through Actions so that tests and migrations gate them. Without this, Vercel would deploy on push regardless of whether the test suite passed.

| Workflow | Trigger | Steps |
|---|---|---|
| `ci.yml` | PR, push to `main`/`staging`, and `workflow_call` | ruff check + format, pytest against a `postgres:18` service container |
| `deploy-staging.yml` | push to `staging` | ci → `vercel pull --environment=preview` → migrate Neon `staging` → `vercel build` → `vercel deploy --prebuilt` |
| `deploy-production.yml` | push to `main` | ci → `vercel pull --environment=production` → migrate Neon `production` → `vercel build --prod` → `vercel deploy --prebuilt --prod` → smoke test |

`ci.yml` is a reusable workflow (`workflow_call`) that both deploy workflows
invoke as a job, so tests gate every deployment without the steps existing in
three places. Both deploys carry a `concurrency` group: two overlapping runs
would otherwise migrate the same Neon branch simultaneously.

**The CI service container is `postgres:18`, not 16.** Neon provisions 18; the
local dev cluster is 16. CI matching production is what would surface a
version-specific regression, and the local skew is acceptable because nothing in
the schema is version-dependent.

**`vercel build` requires `uv` on the runner.** Vercel's Python builder shells
out to it. It is present on Vercel's own build machines and absent on GitHub
runners, and the failure — *"uv is required but was not found in PATH"* —
appears only at build time, after the tests have already passed. Both deploy
workflows install it with `astral-sh/setup-uv`.

**No `.vercelignore`.** It breaks `vercel deploy --prebuilt` with an `ENOENT`
naming a file that exists, because it excludes from the upload files the build
manifest still references. `excludeFiles` under `functions` in `vercel.json`
does the equivalent job without interfering.

Migrations run **before** the deploy is promoted and must be backward-compatible with the currently-live code, because there is a window where old code is serving against the new schema. In practice: add columns nullable, never rename or drop in the same deploy that stops using them.

### 13.3 Function configuration

- `maxDuration: 300` on the entrypoint — **currently on Hobby, which caps there**. Pro allows 800. `pipeline.config.INVOCATION_CEILING_SECONDS` mirrors this value and `DEFAULT_RUN_BUDGET_SECONDS` sits at 80% of it; a test asserts the mirror matches `vercel.json`, because drift is silent — the code would plan for headroom it does not have. Raise both together or neither. The queue-drain design means we should rarely approach it; `run_log.duration_ms` says whether that holds.
- `CRON_SECRET` set as a project env var. Vercel sends it as `Authorization: Bearer …` on every cron invocation; the handler compares and returns 401 otherwise. Without this, a public URL guess triggers your pipeline.
- **Verify on first deploy:** confirm cron invocations still reach the function once Deployment Protection is scoped to "All Deployments". If they are blocked, the documented remedy is Protection Bypass for Automation (`x-vercel-protection-bypass`), which is available on Pro. Test with `vercel crons run` immediately after enabling protection — do not wait for the schedule to discover this.
- Embeddings (Phase 2): the `bge-small-en-v1.5` ONNX model is fetched into the bundle by the build script (`[tool.vercel.scripts] build`) rather than downloaded at runtime, so cold starts do not pay for a 130 MB download. If the bundle exceeds 500 MB, set `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` for the 5 GB limit.

### 13.4 Egress and politeness

Vercel functions egress from shared AWS IPs in `iad1`. Unlike a dedicated elastic IP, that reputation is not ours alone and our backoff cannot repair it.

This does not weaken the §3 guarantee — it strengthens it. Function instances are ephemeral and have no writable persistent disk, so no session cookie can survive an invocation even by accident. The zero-authentication property is now enforced by the platform rather than by our discipline.

It does mean **reachability** is more fragile. Concretely: the §7 hard-stop-on-403 becomes more likely to fire for reasons we did not cause, and Workday — whose rate limiting is per-IP and cross-tenant — is the most exposed. Workday stays deferred (§6), and if it proves unreachable from Vercel that is an acceptable loss, not a reason to add infrastructure back.

### 13.5 Backup and observability

**Backup:** Neon's point-in-time restore covers the database (7 days on the free tier) — no backup job to write or monitor. Generated documents live in Vercel Blob, which is already durable. The genuinely irreplaceable artifact is `profile/`, which lives only on the Mac and must be backed up there.

**Observability:** structured JSON to stdout lands in Vercel Runtime Logs; a `run_log` row per run; the digest email carries the run summary, so a silent failure is visible the next morning. Note that Vercel produces **no log at all** for a cron invocation that was never delivered — a missing `run_log` row is the only signal, which is why the digest reports run counts rather than assuming they happened.

**Secrets:** Vercel Environment Variables, scoped per environment (Production / Preview / Development). GitHub Actions holds only `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, and the Neon migration URLs. **Configuration is read from the real environment — no dotenv file is loaded anywhere.** `docs/environment.md` is the runbook, and provisions both stores through `vercel env add` and `gh secret set` rather than a dashboard.

## 14. Repository layout

```
job_hunt/
├── IDEA.md
├── vercel.json                 # crons, maxDuration
├── pyproject.toml              # deps + [tool.vercel] entrypoint/build
├── docs/superpowers/specs/
├── migrations/                 # NNN_*.sql, applied by CI before deploy
├── api/
│   └── index.py                # the FastAPI `app` Vercel resolves — routes only
├── pipeline/
│   ├── sources/                # one module per source, uniform interface
│   ├── db.py                   # psycopg async pool, pooled Neon endpoint
│   ├── http.py                 # PoliteSession: rate limit, ETag, backoff
│   ├── normalize.py            # RawJob -> Job + fingerprint
│   ├── store.py                # upserts, run_log
│   ├── queue.py                # enqueue / claim (FOR UPDATE SKIP LOCKED) / finish
│   ├── filters/                # deterministic prefilter, sharia screen
│   ├── score.py                # embeddings + rules + freshness
│   ├── llm.py                  # Anthropic client wrapper + spend ceiling
│   ├── docs/                   # layout.py + fpdf2 + docx + one-page loop
│   ├── digest.py               # xlsx + resend
│   └── run_discover.py         # callable by both cron route and CLI
├── profile/                    # GITIGNORED — personal data
│   ├── identity.yaml           # name, title, phone, email, location, URLs
│   ├── resume.json             # seeded from Resume.pdf
│   ├── competency_bullets.yaml # labeled bullet pool (§10.1)
│   ├── cover_letter.yaml       # fixed prose + slot definitions (§10.2)
│   ├── answer_bank.yaml
│   └── targets.yaml
├── profile.example/            # committed templates, no personal data
├── seed/                       # Resume.pdf + Personalized Cover Letter.docx (gitignored)
├── web/
│   ├── routes.py               # APIRouter mounted by api/index.py
│   └── templates/              # Jinja2
├── public/                     # served from the CDN — tailwind.css (built, committed)
├── scripts/
│   ├── capture_fixture.py
│   └── migrate.py              # applied by CI, never at request time
└── .github/workflows/
    ├── ci.yml
    ├── deploy-staging.yml
    └── deploy-production.yml
```

`profile/` is gitignored. It holds Jarra's address, phone, work-authorization status, and salary expectations — none of that belongs in a repo, even a private one.

**`profile/` is a deployment problem now, not just a privacy one.** A gitignored directory is not present in a Vercel build. The personal data it holds is small and structured, so it is stored as a single `PROFILE_JSON` environment variable (Vercel's limit is 64 KB total per deployment, which this fits comfortably) and loaded at startup. `profile/` remains the editable source of truth on the Mac; `scripts/sync_profile.py` serializes it and pushes it with `vercel env add`. It is never committed and never enters a build artifact from the repo.

## 15. Credentials runbook

Every account, in dependency order, with where the value goes.

Every value lands in **Vercel Environment Variables**, scoped to an environment. Set them under **Project → Settings → Environment Variables**, or from the CLI:

```bash
vercel env add ANTHROPIC_API_KEY production     # prompts for the value, never echoes it
vercel env add ANTHROPIC_API_KEY preview
```

### 15.1 Vercel — first, everything else depends on the project existing

1. Sign in at <https://vercel.com> with the GitHub account that owns the repo.
2. **Upgrade the team to Pro** ($20/mo). This is what unlocks production-domain protection, 800s functions, and per-minute cron. On Hobby the production URL is public and the design does not hold.
3. **Add New → Project → Import** the private `job_hunt` repo. Framework preset: **Other**. Do not deploy yet.
4. **Settings → Git → disable automatic deployments** for both production and preview. GitHub Actions owns deploys so tests gate them (§13.2).
5. **Settings → Deployment Protection → Vercel Authentication**, scope **All Deployments**. Verify by opening the production URL in a private window — it must challenge for login.
6. **Settings → Environment Variables** → add `CRON_SECRET`, a random string of ≥32 characters. Vercel sends it as `Authorization: Bearer …` on every cron invocation.
7. **Account Settings → Tokens → Create** a token scoped to the team, name it `github-actions`. Copy it.
8. Locally: `npm i -g vercel && vercel link`, then read `.vercel/project.json` for `orgId` and `projectId`.

→ GitHub repo **Settings → Secrets and variables → Actions**: `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`

### 15.2 Neon Postgres — required

1. Sign up at <https://neon.com> (free tier is sufficient) and create a project named `jobhunt`, region **AWS us-east-1** — same region as Vercel's `iad1`, so queries do not cross a continent.
2. The default branch is `main`. **Branches → New Branch** → name it `staging`, parent `main`.
3. For each branch, **Connect** → copy the **Pooled connection string** (the host contains `-pooler`).

**Use the pooled string everywhere at runtime.** Serverless functions open a connection per instance; the direct endpoint will exhaust `max_connections`. Two consequences, both already reflected in the design: session-level advisory locks are unavailable (§4.2 uses `FOR UPDATE SKIP LOCKED` instead) and SQL-level `PREPARE` is unavailable (psycopg's protocol-level prepared statements work fine).

Migrations are the one exception — they run from CI against the **direct** (non-pooled) string, because DDL wants a stable session.

→ Vercel env `DATABASE_URL` = pooled `main` string, **Production** scope
→ Vercel env `DATABASE_URL` = pooled `staging` string, **Preview** scope
→ GitHub Actions secrets `NEON_DIRECT_URL_PRODUCTION`, `NEON_DIRECT_URL_STAGING`

### 15.3 Anthropic API key — required

1. Go to <https://console.anthropic.com> and sign in.
2. **Settings → API keys → Create key.** Create **two**, named `job-hunt-production` and `job-hunt-staging`, so a staging misfire cannot exhaust the production budget or muddy its spend data.
3. Copy each immediately — shown once.
4. **Settings → Billing → Limits** → set a monthly spend limit (suggest **$25**). This is a second ceiling behind the in-code `llm_spend` cap.

→ Vercel env `ANTHROPIC_API_KEY` (different value per environment scope)

### 15.4 Vercel Blob — required (generated documents)

The function filesystem is ephemeral, so PDFs need a durable home.

1. **Project → Storage → Create Database → Blob**, name it `jobhunt-docs`.
2. Connecting it to the project injects `BLOB_READ_WRITE_TOKEN` automatically — you do not set this by hand.
3. Upload with `access='public'`: Blob URLs are unguessable but not authenticated. These documents contain Jarra's résumé, so treat the URL itself as the secret and never render one outside the authenticated UI.

→ Vercel env `BLOB_READ_WRITE_TOKEN` (auto-injected)

### 15.5 Gmail SMTP — required (daily digest)

Jarra already holds a Gmail App Password, so the digest sends over standard SMTP with no email vendor in the path.

**`nodemailer` was requested and is not usable here.** It is a Node.js library, and spec §4.1 keeps Node out of the dependency tree entirely — adding a second runtime to a Python Vercel function to send one message a day is a poor trade. `aiosmtplib` gives the identical outcome: same SMTP server, same App Password, async, pure Python.

1. App Password already configured. If it needs replacing: <https://myaccount.google.com/apppasswords>.
2. Strip the spaces from the 16-character value — Google accepts either form, but a spaced value looks truncated in `vercel env ls`.
3. Send **from and to `jarraomar4@gmail.com`**. A self-addressed digest needs no SPF/DKIM work of any kind.

→ Vercel env `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_TO_EMAIL`, `DIGEST_FROM_EMAIL`

**Port 587 with STARTTLS, not 465 or 25.** Vercel functions run on Lambda, which blocks outbound port 25 but permits 587. Confirm the digest sends on the first deploy rather than waiting for a 06:05 schedule to tell you.

**The App Password is a full-mailbox credential**, not a scoped sending key like Resend's. That is the real cost of dropping the vendor: a leak grants SMTP access to the account rather than to a send endpoint. It lives only in Vercel env vars and is revocable at the link above.

**Confirmed 2026-08-01.** `jarraomar4@gmail.com` is the single address for the digest and every application — it is what `Resume.pdf` already carries, so recruiters reply where Jarra is reading.

`developer@cloudbaseservices.com` is deliberately kept for a different purpose: the crawler's `User-Agent` contact string (`pipeline/config.py`). That address exists so a site operator who wants us to slow down can reach a human without reaching for an IP block. Keeping ops mail separate from recruiter mail means a politeness complaint cannot get lost in an inbox full of job replies.

### 15.6 Adzuna — free, key required

1. <https://developer.adzuna.com> → **Sign up** → register an application.
2. You get an **`app_id`** and an **`app_key`**.
3. Verify the current free-tier call limit on the dashboard (`IDEA.md` cites ~1,000/month; treat that as unverified). If it is that low, Adzuna is a low-priority breadth source, polled once daily, not every 6 hours.

→ Vercel env `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`

### 15.7 USAJobs — free, key required

1. <https://developer.usajobs.gov/apirequest/> → request an API key. It arrives by email.
2. Requests require three headers: `Host: data.usajobs.gov`, `User-Agent: <the email you registered>`, `Authorization-Key: <key>`.

→ Vercel env `USAJOBS_API_KEY`, `USAJOBS_USER_AGENT`

### 15.8 Not needed

| Dropped | Because |
|---|---|
| AWS (EC2, SSM, S3, IAM) | no servers; Vercel env vars and Blob replace all of it |
| Tailscale | Vercel Authentication protects the production domain on Pro |
| OpenAI | embeddings run locally via fastembed |
| AWS SES | Gmail SMTP with an App Password Jarra already holds |
| Resend | same — dropped once SMTP replaced it, one fewer vendor holding a key |
| nodemailer | Node-only; `aiosmtplib` is the Python equivalent and keeps the runtime single |
| Browserbase / Steel / Skyvern / Playwright | no server-side browser exists in this design |
| Fantastic.jobs / JSearch / Coresignal / TheirStack | only if free sources yield <30 relevant jobs/day |

The existing t2.micro can be stopped once Phase 1 runs green on Vercel. Do not terminate it until then.

### 15.9 Full variable list

| Variable | Scope | Source |
|---|---|---|
| `DATABASE_URL` | Vercel prod + preview | Neon pooled string, per branch (§15.2) |
| `CRON_SECRET` | Vercel prod + preview | random ≥32 chars, self-generated |
| `ANTHROPIC_API_KEY` | Vercel prod + preview | §15.3, distinct key per environment |
| `BLOB_READ_WRITE_TOKEN` | Vercel, auto | injected by connecting the Blob store |
| `SMTP_HOST` / `SMTP_PORT` | Vercel prod | `smtp.gmail.com` / `587` (§15.5) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Vercel prod | Gmail address + App Password (§15.5) |
| `DIGEST_TO_EMAIL` / `DIGEST_FROM_EMAIL` | Vercel prod | §15.5 |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Vercel prod + preview | §15.6 |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | Vercel prod + preview | §15.7 |
| `DAILY_LLM_CAP_USD` | Vercel prod + preview | `1.50` |
| `PROFILE_JSON` | Vercel prod + preview | `scripts/sync_profile.py` (§14) |
| `VERCEL_TOKEN` / `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` | GitHub Actions | §15.1 |
| `NEON_DIRECT_URL_PRODUCTION` / `NEON_DIRECT_URL_STAGING` | GitHub Actions | §15.2, non-pooled |

**Local development:** `vercel env pull .env.local` writes the preview-scope values to a gitignored file, loaded by `python-dotenv`. Point `DATABASE_URL` at a local Postgres container instead of Neon `staging` when running the test suite, so tests never touch a shared database.

### 15.10 Personal data (not credentials, but required before first run)

Goes in `profile/answer_bank.yaml`, never in the repo:

- **Work authorization: US-born citizen** — confirmed 2026-08-01, resolving the item `IDEA.md` §C flagged as unknown. Therefore: authorized to work in the US **without restriction**, and **no sponsorship required now or in the future**. These are two separate questions on most forms and both have a fixed answer.
- Salary: **$150,000 target / $125,000 floor** — confirmed.
- Contact: name, `jarraomar4@gmail.com`, phone, San Leandro CA address, LinkedIn / GitHub / portfolio URLs.
- EEO / gender / veteran / disability self-ID selections. Encode OFCCP Form CC-305 (OMB 1250-0005) verbatim — **re-verify the current form revision at build time**; `IDEA.md` reports an extension to 2029-07-31, which should be confirmed rather than hardcoded on trust.

### 15.11 Two consequences of citizenship for the discovery layer

Citizenship is not just a form answer — it changes which sources are worth crawling and which filters are correct.

**USAJobs becomes a real source rather than a wasted one.** The majority of federal postings are restricted to US citizens. Under an unknown-status assumption, USAJobs would have been mostly noise; it is now a legitimate breadth source with no competition from non-citizen applicants. It stays low priority relative to the ATS sources (federal hiring is slow and the pay bands often sit below the $125k floor), but it earns its API key.

**Clearance-required roles must not be filtered out.** US citizenship is a prerequisite for a security clearance, so "US Citizenship required", "Must be able to obtain a security clearance", and "ITAR" in a job description are **not disqualifiers** — the pre-filter must never drop on those phrases. This matters specifically for the defense and aerospace embedded roles that suit Jarra's Computer Engineering background, and those roles face a structurally smaller applicant pool. An *active* clearance is a different matter and is a legitimate soft negative, since Jarra does not hold one; "clearance required" is a hard filter only when it demands one already in place.

This is recorded here so §8 scoring and the Phase-1 pre-filter treat it as a deliberate rule rather than rediscovering it.

## 16. Phasing

Each phase is independently verifiable and independently useful.

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Vercel Pro + project, Neon branches, env vars, GitHub Actions workflows, migration runner | A trivial route deploys to both environments through CI; production challenges for login; `vercel crons run` reaches the function |
| 1 | Discovery + dedup + deterministic prefilter + `run_log` (no LLM) | A full run ingests ≥300 jobs and prefilters to a plausible shortlist |
| 2 | Embeddings + Haiku relevance + Sharia screen | Scores correlate with Jarra's own judgment on a manual sample |
| 3 | Web UI: queue, detail, tracker, settings (read-only + status writes) | Jarra reviews a day's queue on the phone |
| 4 | `work_queue` + worker + document generation + one-page loop | "Prep" produces a verified one-page PDF pair in <30s |
| 5 | SMTP digest + .xlsx attachment | A digest lands at 06:00 with correct counts |
| 6 | Eager generation for top ~8 + Batch API | Morning queue is pre-prepped; monthly spend still under target |

Ship phase 1 before anything else. If free sources do not yield ~30 relevant jobs/day, the answer is a paid source (Fantastic.jobs, ~$1/1,000 jobs), not more engineering.

## 17. Testing

- **Source adapters:** recorded HTTP fixtures per source; assert normalization to the canonical `Job` shape. No live network in tests.
- **Fingerprinting:** property test that trivial title/location variations collapse to one fingerprint and genuinely different roles do not.
- **Filters:** table-driven over salary strings, locations, and titles, including the ambiguous cases that motivated regex parsing.
- **Sharia screen:** fixture companies per tier; assert `sharia_source='user'` beats both blocklist and LLM, and that a cached verdict never triggers a second LLM call.
- **One-page loop:** a deliberately overlong resume must converge or flag within 2 retries — never loop.
- **Spend ceiling:** the LLM wrapper must raise, not silently proceed, once the daily cap is hit.
- **Queue:** a crash mid-item leaves the row re-claimable, not stuck in `running`. Two concurrent claims of the same `kind` must return disjoint sets — the `SKIP LOCKED` guarantee is what protects against overlapping cron invocations, so it gets a test with two real connections rather than being assumed.
- **Idempotency:** running the full discovery pass twice over the same fixtures must produce identical row counts. This is the test that stands in for Vercel's duplicate cron delivery.
- **Time budget:** a run given an already-expired budget must return cleanly with `budget_hit=true` and leave unclaimed rows `pending` — never partially applied.
- LLM calls are stubbed everywhere except one opt-in live smoke test.

Tests run against a **real Postgres** (a `postgres:16` service container in CI, Docker locally) rather than a stub. Migrations are the schema under test, and half these guarantees — `SKIP LOCKED`, partial unique indexes, `ON CONFLICT` — do not exist outside a real engine.

## 18. Risks and open items

| Item | Handling |
|---|---|
| Shared Vercel egress IPs degrade crawl reachability | Accepted (§13.4). Public ATS JSON APIs are unaffected; Workday is the exposed case and is already deferred. `run_log.errors` plus a 403 counter per host make it measurable rather than theoretical. |
| Vercel silently skips a cron invocation | Best-effort delivery is documented behavior. Work is queued and resumable, so the next tick catches up; the digest reports actual run counts so a persistent gap is visible. |
| 800s invocation ceiling | Queue-drain with a wall-clock budget means no single run must finish the work. `run_log.duration_ms` and `budget_hit` tell us whether the budget is set sanely. |
| Cron may be blocked by Deployment Protection | Unverified until first deploy. Test with `vercel crons run` immediately after enabling protection; documented fallback is Protection Bypass for Automation (§13.3). |
| Workday IP-level rate limiting | Dedicated slow lane, ETags, hard stop on 403. If it still blocks, drop Workday — the other three of the "big four" cover most tech roles. |
| Haiku 4.5's 4096-token cache minimum | Startup assertion on `cache_creation_input_tokens`. Caching failing silently is the risk, not caching being unavailable. |
| Adzuna free-tier limit unverified | Confirm at signup; demote to daily polling if ~1,000/month. |
| OFCCP CC-305 revision date | Re-verify before hardcoding; do not trust the date in `IDEA.md`. |
| `fpdf2` pagination ≠ Word pagination | Accepted: PDF is primary and verified; the `.docx` is an unverified fallback. Shared layout constants (§10.2) keep the two from drifting structurally. |
| `fpdf2` gives no CSS layout | Accepted, and cheaper than it looks: both documents are fixed-layout slot-filled forms (§10.1), where imperative placement is more deterministic than a CSS engine. Revisit only if a future document genuinely needs flow layout. |
| Model pricing and IDs move fast | Re-verify at build time. Anthropic model IDs used here: `claude-haiku-4-5`, `claude-sonnet-5`. |
| Review queue goes unread | This is the real failure mode. The digest email is the habit trigger; `/tracker` makes neglect visible. If the queue is ignored for two weeks, the honest conclusion is that the value was in discovery and phases 4–6 should be reconsidered. |
