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

**Rule: no process on the EC2 instance ever authenticates as Jarra on any job platform.**

This is the load-bearing constraint of the whole design, and it is enforced structurally rather than by policy:

- Discovery reads **public, unauthenticated JSON endpoints only**. There is no account attached to those requests, so there is nothing to suspend.
- **No browser is installed on the box.** Not Playwright, not Puppeteer, not headless Chrome. The dependency is absent, so it cannot be reintroduced by accident later.
- The UI's apply action is `<a href="{apply_url}" target="_blank">`. Jarra's own browser, own session, own click.
- `IDEA.md` Stage 3 ("cautious, narrow auto-submit") is **cut from the design entirely**.

The residual risk is not to accounts but to *reachability*: an EC2 IP that hammers public endpoints can get throttled or IP-blocked, which silently degrades the system. Mitigations in §7.

## 4. Architecture

Three long-lived units on one t2.micro, coordinated through a single SQLite database. No inbound ports open.

```
 systemd timer ──► pipeline (Python)         every 6h: discover + score
        │                                    06:00 PT only: eager gen + email
        ▼
   SQLite (WAL) ◄──── worker (Python, always on)
        ▲                  polls work_queue → doc generation
        │
   FastAPI + Jinja2 + HTMX (uvicorn on tailscale0:8000)
        │
   laptop / phone over Tailscale
```

### 4.1 Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 throughout | One runtime, one dependency manager, one mental model. No language boundary. |
| Web | FastAPI + Jinja2 + HTMX | Server-rendered; no client state. Imports pipeline models directly — zero duplicated types. ~70 MB resident. |
| CSS | Tailwind v4 **standalone binary** | No Node.js, no npm anywhere. CSS built on the Mac and committed. |
| DB | SQLite, WAL mode, `busy_timeout=5000` | Two writers (pipeline, web) at trivial volume. Single file, trivially backed up. |
| Docs | `python-docx` + **WeasyPrint** | WeasyPrint gives page count from the render (`len(document.pages)`); ~100 MB instead of LibreOffice's ~500 MB. |
| Embeddings | `fastembed` (ONNX, `bge-small-en-v1.5`) | Local, $0, no API key, no PyTorch. ~200 MB resident. |
| LLM | Anthropic only | Haiku 4.5 + Sonnet 5. One vendor, one key, one bill. |
| Access | Tailscale | Zero inbound security-group rules. Works from phone. |
| Email | Resend | 5-minute setup vs SES domain verification + sandbox-exit ticket. |

### 4.2 Why the `work_queue` table is the coordination mechanism

The UI never executes work directly. Clicking "Prep" inserts a row into `work_queue`; the single always-on worker drains it. This buys four things at once:

1. **Serialization.** One worker means exactly one WeasyPrint render at a time — which is what keeps a 1 GB box alive.
2. **Retries and visible status** for free, as row state.
3. **Crash safety.** A restart resumes from the table.
4. **No HTTP tier** between web and work.

The UI polls the row via HTMX (`hx-trigger="every 2s"`) and swaps in the preview when `status='done'`.

### 4.3 Services (systemd)

| Unit | Type | Schedule |
|---|---|---|
| `jobhunt-pipeline.timer/.service` | oneshot | `OnCalendar=*-*-* 00,06,12,18:05:00 America/Los_Angeles` |
| `jobhunt-worker.service` | simple, `Restart=always` | continuous, polls every 2s |
| `jobhunt-web.service` | simple, `Restart=always` | continuous, uvicorn |

The 06:00 run additionally does eager document generation and sends the digest. The other three are discovery + scoring only. Rationale: application timing is one of the strongest predictors of a response, so a job found at noon should be visible at noon.

## 5. Data model

```sql
-- Employers. Sharia verdict is cached here forever.
CREATE TABLE companies (
  company_id      INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  domain          TEXT,
  ats_type        TEXT,           -- greenhouse|lever|ashby|smartrecruiters|...
  board_token     TEXT,
  sharia_verdict  TEXT NOT NULL DEFAULT 'unknown',  -- allowed|excluded|flagged|unknown
  sharia_sector   TEXT,
  sharia_reason   TEXT,
  sharia_source   TEXT,           -- blocklist|llm|user   (user always wins)
  sharia_decided_at TEXT
);

CREATE TABLE jobs (
  job_id        INTEGER PRIMARY KEY,
  fingerprint   TEXT NOT NULL UNIQUE,   -- sha256(norm_company|norm_title|norm_city)
  company_id    INTEGER NOT NULL REFERENCES companies(company_id),
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
  posted_at     TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  closed_at     TEXT,
  UNIQUE(source, source_job_id)
);

CREATE TABLE scores (
  job_id            INTEGER PRIMARY KEY REFERENCES jobs(job_id),
  embed_similarity  REAL,
  rule_score        REAL,
  freshness_score   REAL,
  total_score       REAL NOT NULL,
  relevance_verdict TEXT,
  rationale         TEXT,          -- the 2 lines shown in the UI
  is_stretch        INTEGER NOT NULL DEFAULT 0,
  model             TEXT,
  scored_at         TEXT NOT NULL
);

-- The outcome feedback loop. Absent from IDEA.md; this is how we learn whether it works.
CREATE TABLE applications (
  app_id            INTEGER PRIMARY KEY,
  job_id            INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
  status            TEXT NOT NULL,  -- queued|prepped|applied|responded|interview|offer|rejected|skipped
  applied_at        TEXT,
  resume_path       TEXT,
  cover_letter_path TEXT,
  notes             TEXT,
  updated_at        TEXT NOT NULL
);

CREATE TABLE work_queue (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,      -- prep_documents|classify_company
  payload     TEXT NOT NULL,      -- json
  status      TEXT NOT NULL,      -- pending|running|done|failed
  attempts    INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT
);

CREATE TABLE answer_bank (
  key      TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  value    TEXT NOT NULL,
  category TEXT NOT NULL
);

CREATE TABLE unmapped_questions (
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER REFERENCES jobs(job_id),
  label         TEXT NOT NULL,
  field_type    TEXT,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL
);

-- Hard spend ceiling. Refuses calls past the daily cap.
CREATE TABLE llm_spend (
  id                  INTEGER PRIMARY KEY,
  day                 TEXT NOT NULL,
  model               TEXT NOT NULL,
  purpose             TEXT NOT NULL,
  input_tokens        INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL,
  cost_usd            REAL NOT NULL,
  created_at          TEXT NOT NULL
);
CREATE INDEX idx_llm_spend_day ON llm_spend(day);

CREATE TABLE run_log (
  run_id       INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  jobs_seen    INTEGER, jobs_new INTEGER, jobs_filtered INTEGER,
  jobs_scored  INTEGER, docs_generated INTEGER, errors INTEGER,
  peak_rss_mb  INTEGER   -- so we find out empirically whether 1 GB holds
);
```

Schema lives in `migrations/NNN_*.sql`, applied by Python at pipeline/worker/web startup (idempotent, tracked in a `schema_version` table). Python is the only thing that applies migrations.

**Repost handling.** Jobs get reposted with fresh IDs. `fingerprint` is content-derived, so a repost collapses onto the existing row and updates `last_seen_at` rather than appearing as new. `UNIQUE(source, source_job_id)` catches the exact-duplicate case.

## 6. Discovery layer

Each source is one module exporting a uniform interface:

```python
class Source(Protocol):
    name: str
    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]: ...
```

Adding a source is one file plus one registry entry. Normalization to the canonical `Job` shape happens in `normalize.py`, not in the source modules.

**No key required:** Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Workday CXS, HN Algolia, Remotive, RemoteOK, Arbeitnow, Himalayas, Jobicy, WeWorkRemotely, The Muse.
**Free, key required:** Adzuna, USAJobs.
**Explicitly excluded:** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound — all require authentication or violate ToS to automate. Per §3, they are out.

Target employer board tokens live in `profile/targets.yaml` (~150 to start).

## 7. Politeness and rate-limit safety

All HTTP goes through one `PoliteSession` wrapper (httpx):

- Per-host token bucket, default 1 req/sec with ±30% jitter.
- **Workday gets its own slower lane (1 req/2s)** — it rate-limits by source IP *across all tenants*, so 150 tenants from one EC2 IP is the realistic failure mode.
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
- Both render `python-docx` → `.docx` and Jinja2+CSS → WeasyPrint → PDF.
- **One-page loop:** `len(HTML(...).render().pages)`; if > 1, regenerate with a reduced word budget (feeding back the current count), **max 2 retries**, then flag in the UI rather than loop forever.

**PDF is the primary artifact.** WeasyPrint's pagination is what gets verified; the `.docx` is an unverified fallback for the rare form that demands Word. Greenhouse, Lever, Ashby, and Workday all parse PDF correctly in 2026, and every submission is human-reviewed anyway.

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

**Existing layout is one-page-tuned and must be preserved:** margins 0.70″ left / 0.67″ right / 0.30″ top / 0.40″ bottom. The WeasyPrint CSS mirrors these exactly so the PDF matches the `.docx`.

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
| EC2 t2.micro (existing) | $0 if in free-tier window, else ~$8.50 |
| EBS gp3 20 GB | ~$2 |
| S3 nightly backup | <$1 |
| SSM Parameter Store / Resend / Tailscale | $0 |
| Anthropic — build phase (lazy only) | ~$3–5 |
| Anthropic — steady state (eager + caching + batch) | ~$5–7 |
| **Total** | **~$5/month best case, ~$19 worst case** |

## 12. Web UI

FastAPI + Jinja2 + HTMX + Tailwind v4. Four routes.

| Route | Contents |
|---|---|
| `/` | Ranked queue. Cards: title, company, salary, location, score, "posted 6h ago", Sharia badge if flagged. |
| `/job/{id}` | JD, match rationale, inline PDF preview of both documents, downloads, **Open apply page** (new tab), copy-to-clipboard answer-bank panel, status buttons. |
| `/tracker` | Funnel: queued → applied → responded → interview → rejected. Weekly conversion stats. |
| `/settings` | Answer bank, Sharia overrides, target employer list, unmapped questions, run log + peak RSS. |

Server-rendered; HTMX handles the prep-poll and status mutations. No client-side framework, no build step that can OOM.

## 13. Infrastructure

**Instance:** existing t2.micro (1 vCPU / 1 GB, x86), Ubuntu.

Estimated peak: OS ~180 MB + uvicorn ~70 MB + Python worker with fastembed ~350 MB + WeasyPrint render ~100 MB ≈ **~700 MB**. Add a **2 GB swapfile** as insurance. `run_log.peak_rss_mb` records actual peak each run so the estimate gets replaced by data.

**Tripwire:** if OOM kills appear or peak RSS exceeds ~850 MB, move to t3.small (2 GB, x86, ~$15/mo) — a stop / change-instance-type / start, about two minutes, same architecture, same EBS volume. Do not pre-emptively upsize.

**Network:** security group with **zero inbound rules** — not even SSH. Tailscale is outbound-only (UDP hole-punching, DERP relay fallback), and Tailscale SSH replaces port 22.

**Backup:** nightly `sqlite3 .backup` → gzip → S3, 30-day lifecycle. Generated documents also sync to S3. Without this, one instance failure loses the entire application history.

**Observability:** structured JSON logs to journald; `run_log` row per run; the digest email carries the run summary, so a silent failure is visible the next morning.

**Secrets:** SSM Parameter Store SecureString, pulled at boot by an IAM instance role into `/etc/jobhunt/env` (mode 0600, owned `root:jobhunt`), loaded by systemd `EnvironmentFile=`. No plaintext `.env` on the box, no long-lived AWS keys anywhere.

## 14. Repository layout

```
job_hunt/
├── IDEA.md
├── docs/superpowers/specs/
├── migrations/                 # NNN_*.sql, applied by Python
├── pipeline/
│   ├── sources/                # one module per source, uniform interface
│   ├── http.py                 # PoliteSession: rate limit, ETag, backoff
│   ├── normalize.py            # RawJob -> Job + fingerprint
│   ├── filters/                # deterministic prefilter, sharia screen
│   ├── score.py                # embeddings + rules + freshness
│   ├── llm.py                  # Anthropic client wrapper + spend ceiling
│   ├── docs/                   # docx + weasyprint + one-page loop
│   ├── digest.py               # xlsx + resend
│   ├── worker.py               # work_queue poller
│   └── run_daily.py
├── profile/                    # GITIGNORED — personal data
│   ├── identity.yaml           # name, title, phone, email, location, URLs
│   ├── resume.json             # seeded from Resume.pdf
│   ├── competency_bullets.yaml # labeled bullet pool (§10.1)
│   ├── cover_letter.html.j2    # template, margins mirrored from the .docx
│   ├── answer_bank.yaml
│   └── targets.yaml
├── profile.example/            # committed templates, no personal data
├── seed/                       # Resume.pdf + Personalized Cover Letter.docx (gitignored)
├── web/
│   ├── main.py                 # FastAPI app
│   ├── templates/              # Jinja2
│   └── static/                 # tailwind.css (built, committed)
└── infra/
    ├── systemd/                # 3 unit files + 1 timer
    ├── setup.sh
    └── fetch-secrets.sh
```

`profile/` is gitignored. It holds Jarra's address, phone, work-authorization status, and salary expectations — none of that belongs in a repo, even a private one.

## 15. Credentials runbook

Every account, in dependency order, with where the value goes.

### 15.1 Anthropic API key — required

1. Go to <https://console.anthropic.com> and sign in.
2. **Settings → API keys → Create key.** Name it `job-hunt-ec2`.
3. Copy immediately — it is shown once.
4. **Settings → Billing → Limits** → set a monthly spend limit (suggest **$25**). This is a second ceiling behind the in-code `llm_spend` cap.

→ `/jobhunt/ANTHROPIC_API_KEY` → env `ANTHROPIC_API_KEY`

### 15.2 Resend API key — required (daily digest)

1. Go to <https://resend.com> and sign up.
2. **API Keys → Create API Key**, permission **Sending access**. Copy it.
3. For zero DNS setup, send **from `onboarding@resend.dev` to the address you signed up with** — that works on the free tier immediately.
4. Optional, later: **Domains → Add Domain** for `cloudbaseservices.com`, add the SPF/DKIM records Resend shows, then send from `jobs@cloudbaseservices.com`.

→ `/jobhunt/RESEND_API_KEY` → env `RESEND_API_KEY`
→ `/jobhunt/DIGEST_TO_EMAIL`, `/jobhunt/DIGEST_FROM_EMAIL`

### 15.3 Tailscale — required (UI access)

**Order matters. Do not remove the SSH inbound rule until Tailscale SSH is verified working, or you will lock yourself out of the instance.**

1. Go to <https://login.tailscale.com/start>, sign in with Google or GitHub. Free **Personal** plan.
2. **Settings → Keys → Generate auth key.** Reusable: off. Ephemeral: off. Expiry: 90 days. Copy it.
3. On the EC2 instance (over your existing SSH):
   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --ssh --authkey=tskey-auth-XXXX
   tailscale ip -4          # note the 100.x.y.z address
   ```
4. Install Tailscale on the Mac and on the phone; sign in to the same account.
5. **Verify from the Mac:** `ssh ubuntu@100.x.y.z` must succeed over Tailscale.
6. **Only after step 5 succeeds:** in the EC2 console, edit the instance's security group and delete **all** inbound rules, including port 22.
7. Bind uvicorn to the Tailscale interface: `--host 100.x.y.z --port 8000`.

The auth key is consumed at join time and is not needed at runtime — it does not go into SSM.

### 15.4 AWS — required (you already have the account and instance)

**a. Backup bucket**
```
aws s3 mb s3://jobhunt-backup-<suffix> --region us-west-2
aws s3api put-public-access-block --bucket jobhunt-backup-<suffix> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

**b. IAM policy** (`jobhunt-instance-policy`) — least privilege:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:us-west-2:<ACCOUNT_ID>:parameter/jobhunt/*" },
    { "Effect": "Allow", "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-west-2:<ACCOUNT_ID>:alias/aws/ssm" },
    { "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::jobhunt-backup-<suffix>/*" }
  ]
}
```

**c. IAM role** `jobhunt-instance-role`, trusted entity **EC2**, with that policy attached. Then **EC2 → Instances → select the t2.micro → Actions → Security → Modify IAM role → jobhunt-instance-role**. No restart required.

**d. Confirm free-tier status:** **Billing → Free Tier** in the console. If the t2.micro 750-hour benefit is expired, note that t3.micro is same-size, newer-generation, and slightly cheaper (~$7.60 vs ~$8.50/mo) — a stop / change-type / start away.

No AWS access keys are created. The instance role is the only credential.

### 15.5 Adzuna — free, key required

1. <https://developer.adzuna.com> → **Sign up** → register an application.
2. You get an **`app_id`** and an **`app_key`**.
3. Verify the current free-tier call limit on the dashboard (`IDEA.md` cites ~1,000/month; treat that as unverified). If it is that low, Adzuna is a low-priority breadth source, polled once daily, not every 6 hours.

→ `/jobhunt/ADZUNA_APP_ID`, `/jobhunt/ADZUNA_APP_KEY`

### 15.6 USAJobs — free, key required

1. <https://developer.usajobs.gov/apirequest/> → request an API key. It arrives by email.
2. Requests require three headers: `Host: data.usajobs.gov`, `User-Agent: <the email you registered>`, `Authorization-Key: <key>`.

→ `/jobhunt/USAJOBS_API_KEY`, `/jobhunt/USAJOBS_USER_AGENT`

### 15.7 Not needed

| Dropped | Because |
|---|---|
| OpenAI | embeddings run locally via fastembed |
| Cloudflare | Tailscale handles access |
| AWS SES | Resend, no domain verification or sandbox ticket |
| Browserbase / Steel / Skyvern / Playwright | no server-side browser exists in this design |
| Fantastic.jobs / JSearch / Coresignal / TheirStack | only if free sources yield <30 relevant jobs/day |

### 15.8 Writing secrets to SSM

Run from the Mac with an admin AWS profile:

```bash
put() { aws ssm put-parameter --region us-west-2 --name "$1" --type SecureString --value "$2" --overwrite; }

put /jobhunt/ANTHROPIC_API_KEY   'sk-ant-...'
put /jobhunt/RESEND_API_KEY      're_...'
put /jobhunt/DIGEST_TO_EMAIL     'developer@cloudbaseservices.com'
put /jobhunt/DIGEST_FROM_EMAIL   'onboarding@resend.dev'
put /jobhunt/ADZUNA_APP_ID       '...'
put /jobhunt/ADZUNA_APP_KEY      '...'
put /jobhunt/USAJOBS_API_KEY     '...'
put /jobhunt/USAJOBS_USER_AGENT  'developer@cloudbaseservices.com'
put /jobhunt/DAILY_LLM_CAP_USD   '1.50'
```

On the instance, `/usr/local/bin/jobhunt-secrets` runs at boot (via `jobhunt-secrets.service`, ordered before the other three units):

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
# jq, not --output text: a value containing a tab or newline would corrupt
# a text-parsed env file. jq emits a shell-safe KEY='value' per line.
aws ssm get-parameters-by-path --region us-west-2 --path /jobhunt/ \
    --with-decryption --output json \
  | jq -r '.Parameters[] | "\(.Name | sub("^/jobhunt/";""))=\(.Value | @sh)"' \
  > /etc/jobhunt/env
chmod 600 /etc/jobhunt/env
chown root:jobhunt /etc/jobhunt/env
```

Each unit then carries `EnvironmentFile=/etc/jobhunt/env`.

**Local development** on the Mac uses a gitignored `.env` with the same variable names, loaded by `python-dotenv`.

### 15.9 Personal data (not credentials, but required before first run)

Goes in `profile/answer_bank.yaml`, never in the repo:

- **Work authorization status** — flagged as unconfirmed in `IDEA.md` §C and appears on nearly every application form. **Must be supplied by Jarra.**
- Salary: **$150,000 target / $125,000 floor** — confirmed.
- Contact: name, email, phone, San Leandro CA address, LinkedIn / GitHub / portfolio URLs.
- Sponsorship required: yes/no.
- EEO / gender / veteran / disability self-ID selections. Encode OFCCP Form CC-305 (OMB 1250-0005) verbatim — **re-verify the current form revision at build time**; `IDEA.md` reports an extension to 2029-07-31, which should be confirmed rather than hardcoded on trust.

## 16. Phasing

Each phase is independently verifiable and independently useful.

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Instance prep, swapfile, Tailscale, IAM role, SSM, systemd skeleton, migrations | `systemctl status` clean; UI reachable from phone; zero inbound SG rules |
| 1 | Discovery + dedup + deterministic prefilter + `run_log` (no LLM) | A full run ingests ≥300 jobs and prefilters to a plausible shortlist |
| 2 | Embeddings + Haiku relevance + Sharia screen | Scores correlate with Jarra's own judgment on a manual sample |
| 3 | Web UI: queue, detail, tracker, settings (read-only + status writes) | Jarra reviews a day's queue on the phone |
| 4 | `work_queue` + worker + document generation + one-page loop | "Prep" produces a verified one-page PDF pair in <30s |
| 5 | Resend digest + .xlsx attachment | A digest lands at 06:00 with correct counts |
| 6 | Eager generation for top ~8 + Batch API | Morning queue is pre-prepped; monthly spend still under target |

Ship phase 1 before anything else. If free sources do not yield ~30 relevant jobs/day, the answer is a paid source (Fantastic.jobs, ~$1/1,000 jobs), not more engineering.

## 17. Testing

- **Source adapters:** recorded HTTP fixtures per source; assert normalization to the canonical `Job` shape. No live network in tests.
- **Fingerprinting:** property test that trivial title/location variations collapse to one fingerprint and genuinely different roles do not.
- **Filters:** table-driven over salary strings, locations, and titles, including the ambiguous cases that motivated regex parsing.
- **Sharia screen:** fixture companies per tier; assert `sharia_source='user'` beats both blocklist and LLM, and that a cached verdict never triggers a second LLM call.
- **One-page loop:** a deliberately overlong resume must converge or flag within 2 retries — never loop.
- **Spend ceiling:** the LLM wrapper must raise, not silently proceed, once the daily cap is hit.
- **Worker:** a crash mid-item leaves the row re-claimable, not stuck in `running`.
- LLM calls are stubbed everywhere except one opt-in live smoke test.

## 18. Risks and open items

| Item | Handling |
|---|---|
| 1 GB RAM is genuinely tight | 2 GB swap + `peak_rss_mb` telemetry + a documented 2-minute upgrade path. Measure before spending. |
| Workday IP-level rate limiting | Dedicated slow lane, ETags, hard stop on 403. If it still blocks, drop Workday — the other three of the "big four" cover most tech roles. |
| Haiku 4.5's 4096-token cache minimum | Startup assertion on `cache_creation_input_tokens`. Caching failing silently is the risk, not caching being unavailable. |
| Adzuna free-tier limit unverified | Confirm at signup; demote to daily polling if ~1,000/month. |
| OFCCP CC-305 revision date | Re-verify before hardcoding; do not trust the date in `IDEA.md`. |
| WeasyPrint pagination ≠ Word pagination | Accepted: PDF is primary and verified; the `.docx` is an unverified fallback. |
| Model pricing and IDs move fast | Re-verify at build time. Anthropic model IDs used here: `claude-haiku-4-5`, `claude-sonnet-5`. |
| Review queue goes unread | This is the real failure mode. The digest email is the habit trigger; `/tracker` makes neglect visible. If the queue is ignored for two weeks, the honest conclusion is that the value was in discovery and phases 4–6 should be reconsidered. |
