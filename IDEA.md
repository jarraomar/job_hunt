# Technical Groundwork: Automated AI-Assisted Job Discovery & Application System (Jarra Omar)

## TL;DR
- **Build a discovery-and-preparation engine, not a full auto-submit bot.** Job *discovery* is cheap, legal, and highly automatable via free/official ATS JSON APIs (Greenhouse, Lever, Ashby, Workday CXS) plus free aggregators (Adzuna, HN "Who is Hiring" via Algolia, Remotive/RemoteOK). Job *submission* should be split: only Greenhouse, Lever, and Ashby expose documented applicant-facing POST endpoints, and even those are gated behind employer API keys; everything else (Workday, iCIMS, LinkedIn Easy Apply) should be prepared-and-queued for one-click human review, never blind-fired.
- **Costs are dominated by infrastructure, not AI.** With deterministic pre-filtering and model routing (Haiku/GPT-nano for classification, Sonnet only for final cover letters), LLM spend for 50–150 jobs/day is roughly $6–15/month; the EC2 t3.large instance (needed for headless browsers + LibreOffice) is the largest line item at ~$60/month.
- **Sharia screening is best done as a cheap tiered pipeline:** a hardcoded keyword/domain blocklist first, then a one-time cached LLM industry classification of each new employer, mirroring the Dow Jones Islamic Market / AAOIFI business-activity screens (exclude interest-based finance, alcohol, weapons/defense, entertainment). One-page cover letters need a deterministic LibreOffice→PDF→pypdf page-count loop, not word-count heuristics.

## Key Findings

1. **Free ATS APIs cover the target roles well.** Greenhouse, Lever, Ashby, SmartRecruiters, and Recruitee all publish unauthenticated JSON job-board feeds; Workday exposes an undocumented but stable CXS POST endpoint. These are the primary discovery layer and cost $0.
2. **Compensation data is inconsistent.** Ashby exposes structured salary (`includeCompensation=true`); Greenhouse and Workday almost never do (only in description text where pay-transparency laws apply). The salary filter must parse free text, not rely on a structured field.
3. **Automated submission is the account-ban risk zone.** Greenhouse (`POST /applications`), Lever (postings apply endpoint), and Ashby (`applicationForm.submit`) have real applicant-facing APIs, but the write paths require employer-side keys. LinkedIn/Indeed automation gets accounts restricted — one 2026 figure cites ~23% of browser-extension automation users restricted within 90 days.
4. **hiQ v. LinkedIn protects reading public data, not bypassing auth or ToS-bound logged-in use.** Scraping public job postings is low-risk under the CFAA in the 9th Circuit (California); automating logged-in LinkedIn/Indeed actions violates ToS and risks bans (not criminal liability for an individual).
5. **The cheap, reliable Sharia filter is a two-stage screen** modeled on DJIM/AAOIFI: keyword/domain blocklist → cached LLM classification of company description into allowed/excluded industry buckets.
6. **One-page enforcement must be verified, not estimated.** Convert the generated .docx to PDF with headless LibreOffice (`soffice --headless --convert-to pdf`) and count pages with pypdf; if >1 page, regenerate with a tighter length budget.

## Details

### A. Job Data Sources

**Tier 1 — Free/official ATS JSON feeds (primary layer, $0).** These require knowing each company's board token/slug in advance (no master search endpoint exists), so maintain a curated list of ~100–300 target employers plus discovery from aggregators.

| ATS | Endpoint pattern | Auth | Salary field | Notes |
|---|---|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | None (read) | Rarely (description text only) | Entire board in one response; HTML is entity-escaped, run through `html.unescape()`. `?questions=true` returns application form fields. |
| Lever | `GET api.lever.co/v0/postings/{company}?mode=json` | None | Sometimes | Flat JSON array; `createdAt` in epoch ms. |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` | None | Usually (best salary support) | Cleanest compensation data. |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{company}/postings` | None | Rarely | limit/offset pagination. |
| Recruitee | `GET {company}.recruitee.com/api/offers/` | None | Rarely | One call per company. |
| Workable | `{account}` public careers endpoints | None | Rarely | Newer endpoint uses opaque continuation token. |
| Workday | `POST {tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | None | No | `limit` max 20; throttles fast paging; rate-limits by source IP across all tenants; try underscores for hyphenated tenant slugs on 422. Two-phase: list then `/job/{externalPath}` for detail. |

iCIMS, Taleo, BambooHR, JazzHR, Breezy, Rippling: no clean unauthenticated public feed comparable to the above; best reached via aggregators or skipped. (Workday/Greenhouse/Lever/Ashby is the "big four" for tech roles — Greenhouse alone is used by Airbnb, Stripe, Figma, Anthropic, Databricks, Coinbase, Cloudflare, and Lyft.)

**Tier 2 — Free aggregators & niche boards ($0).**
- **Adzuna API:** free tier of **1,000 API calls/month (≈33/day)** per Adzuna's developer portal, authenticated with `app_id`+`app_key`, endpoint `api.adzuna.com/v1/api/jobs/us/search/1?...`. Good for market coverage and salary histograms, but salary is often **predicted, not stated**, and the description field is a truncated excerpt with `redirect_url` routing through Adzuna.
- **USAJobs API** (`developer.usajobs.gov`): free, US federal jobs — relevant for Computer Engineering / biomedical-adjacent roles.
- **Hacker News "Who is Hiring"** via the public **Algolia HN API** (`hn.algolia.com/api/v1/search`): excellent for the exact modern stack (Python/TS/React/AWS); parse monthly thread comments. Free.
- **Remotive, RemoteOK, WeWorkRemotely, Arbeitnow, Himalayas, Jobicy, Working Nomads:** all have free/public JSON feeds; strong for the Remote-first preference. Arbeitnow is EU-heavy.
- **The Muse** public API: engineering/data/product categories.
- **YC Work at a Startup / Wellfound (AngelList):** no clean free API; login-gated. Treat as manual or scraped-at-risk.

**Tier 3 — Paid aggregators (only if coverage gaps appear).**
- **JSearch (RapidAPI):** cheapest to prototype; wraps Google-for-Jobs (Indeed/ZipRecruiter/Glassdoor/LinkedIn). RapidAPI adds ~30% markup. No ATS coverage. Free tier is prototype-only.
- **Fantastic.jobs:** ~$1 per 1,000 jobs; 140k+ company career sites across 41 ATS platforms, AI+LinkedIn enrichment, up to 60 fields/job. Best value paid option for enriched, deduped ATS data.
- **Coresignal:** $49/mo Starter, Pro $800/mo (≈$0.08/job at 10k volume); 400M+ records, multi-source enriched (2 credits/record). Overkill/expensive for one job seeker.
- **TheirStack:** 315k+ sources, free tier available, ~$49/mo. **Bright Data:** infrastructure/marketplace, enterprise-priced.

**Scraping LinkedIn/Indeed/Glassdoor/ZipRecruiter — current legal status (2026).** The hiQ v. LinkedIn line (9th Cir. 2019, reaffirmed April 18, 2022 post-*Van Buren*) plus *Meta v. Bright Data* (Jan 2024) establish that scraping *publicly available* data without bypassing authentication is not a CFAA violation, and that ToS binds only *logged-in* use. hiQ itself ultimately settled via a Dec 6, 2022 stipulated injunction, paid damages, and agreed to destroy scraped data — so "public scraping is legal" is true for the CFAA but does not immunize you from ToS claims or IP blocking. **Practical rule:** reading public postings = low risk; logging into LinkedIn/Indeed to auto-apply or scrape = ToS violation and real ban risk.

**Recommended tiered source strategy:** Tier 1 ATS feeds for a curated employer list (highest signal, structured application questions, direct apply URLs) → Tier 2 free aggregators for breadth and remote roles → add Fantastic.jobs only if daily volume falls short. This maximizes coverage of full-stack/AI/cloud/embedded roles at near-zero cost.

### B. Automated Application Submission (highest-risk area — candid assessment)

**What an *applicant* can truly do programmatically:**
- **Greenhouse Job Board API `POST /v1/boards/{token}/jobs/{id}` (applications):** documented, accepts multipart form-data (resume/cover letter upload). BUT authentication uses the **Job Board API key** (HTTP Basic), which is the *employer's* secret — Greenhouse explicitly warns it must be proxied by your own server and would be exposed in client forms. **An individual applicant does not possess this key**, so the "official" Greenhouse submit path is for employers building career pages, not for an outside applicant. Greenhouse also does not validate required fields server-side.
- **Lever postings apply endpoint:** accepts JSON or multipart (resume requires multipart). Public-facing; supports fields like `source`, IP, timezone; must handle 429s. This is the closest to a genuinely usable applicant POST path, but forms are per-account customized and Lever advises redirecting to its hosted form if you can't handle the logic.
- **Ashby `applicationForm.submit`:** requires `candidatesWrite` permission (an API key) — again employer-side. File uploads via `file.createFileUploadHandle`. Must check `response.success` or applications silently fail.

**Conclusion:** True zero-touch API submission is generally *not* available to an outside applicant for most ATSes — the documented POST endpoints are gated behind employer API keys. The realistic automatable path is **browser automation of the public hosted application form**, which is exactly where bot detection lives.

**Bot detection on ATS application forms (2026):** No ATS publicly documents its stack, but practitioner and vendor evidence indicates:
- Cloudflare challenges/Turnstile are explicitly designed to block headless Selenium/Puppeteer/Playwright (per Cloudflare's own developer docs: automation frameworks "will be blocked by challenges"). A raw EC2 datacenter IP is itself a strong negative trust signal; Turnstile builds a trust score from TLS/JA3/JA4 fingerprint, IP reputation, and mouse movement.
- Auto-apply vendors report routing **Workday, LinkedIn, iCIMS, SmartRecruiters, and custom forms to manual-required review** because their forms are too variable/hostile to automate; **Greenhouse is the one most reliably auto-submitted**. reCAPTCHA and email-verification codes are encountered on ATS flows.
- Ashby launched an applicant fraud-detection product in September 2025; newer ATSes are actively adding bot/identity defenses.

**Account bans — what triggers them:**
- **LinkedIn:** flags "human-impossible velocity" and low-relevance actions; ~23% of browser-extension automation users restricted within 90 days (single-sourced 2026 "ConnectSafely.ai" figure, treat as directional). Community-estimated safe thresholds: ~100 connection requests/week (~20/day). First enforcement = 24–72h block or CAPTCHA; repeat = permanent ban. Real-browser sessions within limits are hard to detect; API/injection scrapers detected within days. (LinkedIn permanently banned Apollo.io and Seamless.ai in March 2025 for unauthorized automation/scraping.)
- **Indeed:** estimated 30–50 automated submissions/day before flags; temporary locks or ID verification; permanent bans rare for individuals but do occur; appeals frequently rejected with no reason given.

**How existing tools actually work:**
- **LazyApply:** Chrome extension, DOM injection, mass-blast (up to 1,500/day on top tier), same static resume; ~2.1–2.4★ Trustpilot; one user reported 14,000 applications with poor results; ~2–3% callback (≈ manual cold-apply). Triggers CAPTCHA on Workday/Lever. Plans $99–$149 lifetime.
- **Simplify Copilot:** autofill assistant — fills forms across ATSes but *you* review/submit. Lower ban risk.
- **AIHawk (Auto_Jobs_Applier_AIHawk):** open-source, **crossed 30,000 GitHub stars** (ApplyGhost: "in open-source terms is genuine traction"); Python+Playwright+YAML+OpenAI; LinkedIn Easy Apply only; can't follow redirects to Workday/Greenhouse/Lever; users report temp bans, CAPTCHA loops, permanent restrictions; brutal setup.
- **Sonara, LoopCV, Jobhire, JobRight, Teal, Careerflow:** range from volume auto-appliers (static resume) to review-first copilots. The honest pattern across reviews: volume without per-application tailoring tracks the baseline ~0.4% interview rate; complaints cluster on quality, relevance, and refunds.

**Browser-automation options (server-side), with benchmark reality:**

| Tool | Model | Notes |
|---|---|---|
| Playwright / Puppeteer + stealth | Self-host | Cheapest; vanilla headless is instantly detected by Cloudflare (UA contains "HeadlessChrome", `navigator.webdriver`); stealth patches help but don't beat deep fingerprinting. Use `--headless=new`. |
| browser-use | Open-source (Python) | Claims **89.1% success across 586 WebVoyager tasks** (gpt-4o) in its own SOTA report — but independent replication (Notte Labs' open-operator-evals) failed to reproduce it, finding **20–50% overestimation**. |
| Stagehand | Open-source (TS) | Structured act/extract/observe; runs on Browserbase. |
| Skyvern | Open-source + cloud | Vision+LLM; **85.85% on WebVoyager (643 tasks/15 sites)** per Skyvern 2.0 launch; leads specifically on form-filling/WRITE tasks; native 2FA/CAPTCHA; ~$0.05/step cloud. |
| Browserbase | Managed | Stealth, CAPTCHA, proxies; $20 Developer / $99 Startup plans; Stagehand's recommended backend. |
| Steel.dev | Open-source + cloud | Self-hostable; free tier $10/mo credit ≈ 100 browser-hours. |
| Anthropic computer-use / Playwright MCP | Agentic | Flexible but slow and token-expensive per session. |

Benchmark context: on the WebVoyager leaderboard, **Magnitude now leads at 93.9% pass@1**, ahead of browser-use (89.1%), OpenAI Operator (87.0%), and Skyvern (85.9%) — but these are lab benchmarks on general web tasks, not adversarial ATS forms with active bot detection, so real-world submission reliability will be materially lower.

**Recommended hybrid architecture:**
1. **Auto-submittable (small subset):** Lever public apply endpoint where the form is standard and no CAPTCHA is present. Greenhouse/Ashby write APIs require credentials you won't have. Realistically, treat *all* submission as human-in-the-loop by default.
2. **Prepare-and-queue (the default and recommendation):** for every matched job, generate the tailored cover letter + resume, pre-fill the answer bank, and push to a **review queue**.
3. **Review queue design:** a lightweight web UI served on the EC2 box (single-user, behind an SSH tunnel or Cloudflare Access) beats an email digest for the actual apply step, because it can deep-link to the pre-filled application and show the generated documents inline. Use the **daily email as the notification/summary** (with the .xlsx), and the web UI as the action surface. This keeps the human in control of the ban-risk step while automating ~95% of the labor.

### C. Form-Fill Knowledge Base

Store a declarative **YAML answer bank** with these sections: identity/contact (name, email, phone, address — San Leandro, CA), work authorization ("Authorized to work in the US" / no sponsorship needed — confirm per Jarra's status), visa sponsorship ("No"), the standard EEO/OFCCP self-ID sets (verbatim below), salary expectation ($150k target / $125k floor), notice period, references, per-company "why this company" (generated), and URLs (LinkedIn/GitHub/portfolio).

**Standard US self-identification wordings to encode verbatim:**
- **Disability — OFCCP Form CC-305**, OMB Control No. 1250-0005, "Voluntary Self-Identification of Disability." Note: the previously printed **04/30/2026 expiration is superseded** — OMB's Notice of Action (July 16, 2026) approved OFCCP's renewal "without change" and **extended Form CC-305 through July 31, 2029** (DirectEmployers Association). Three options: "Yes, I have a disability, or have had one in the past"; "No, I do not have a disability and have not had one in the past"; "I do not want to answer." Includes the disclaimers "Disabilities include, but are not limited to" and "No one who makes hiring decisions will see it."
- **Race/Ethnicity (EEO):** Hispanic or Latino; White; Black or African American; Native Hawaiian or Other Pacific Islander; Asian; American Indian or Alaska Native; Two or More Races; plus "Decline to self-identify."
- **Gender:** Male; Female; Decline to self-identify (many forms add "Non-binary").
- **Protected Veteran status:** "I identify as one or more of the classifications of a protected veteran"; "I am not a protected veteran"; "I don't wish to answer."

**Matching approach:** embed each answer-bank key and each incoming form label with a small embedding model (e.g., OpenAI `text-embedding-3-small`); take cosine top-1. If similarity ≥ threshold (~0.80), auto-fill; if in a gray zone (~0.65–0.80), send label+candidate to a cheap LLM (Haiku/GPT-nano) for a yes/no confirmation; if below, **log the unanswered question** to an `unmapped_questions` table (job ID, verbatim label, field type) and flag it in the daily email so Jarra can add the mapping and redeploy.

### D. Cost Optimization

**Current API pricing (mid-2026):**
- **Anthropic:** Haiku 4.5 $1/$5 per MTok; Sonnet 4.6 $3/$15; Opus $5/$25. Batch API −50%; prompt caching −90% on cached input (discounts stack — a cached batch request can cost ~5% of standard).
- **OpenAI:** GPT-4.1-nano $0.10/$0.40; GPT-5.4-nano $0.20/$1.25; GPT-5.4 $2.50/$15; GPT-5.5 $5/$30. Batch −50%; cached input discounted 75–90%.
- **Embeddings:** OpenAI `text-embedding-3-small` is the cheap workhorse for resume/job similarity and form-label matching.

**Model routing:**
- Deterministic pre-filter (free): regex/keyword on title, hard salary floor ($125k), location rules, and the Sharia industry blocklist; plus embedding cosine similarity of the job description against Jarra's resume vector. Only jobs passing all of these reach any LLM.
- Cheap model (Haiku 4.5 / GPT-5.4-nano) for relevance classification and industry screening.
- Expensive model (Sonnet 4.6) *only* for the final cover letter on jobs that will actually be queued.

**Per-unit cost estimate:** a relevance classification (~1.5k input, ~200 output tokens) on Haiku ≈ $0.0025; a cover letter (~2k input, ~600 output) on Sonnet ≈ $0.015.

**Dedup & storage:** fingerprint each job by normalized (company + title + location) hash plus source URL; persist seen jobs in SQLite. Cross-source dedup collapses the Indeed/ZipRecruiter/aggregator triplicates.

**Realistic monthly LLM cost (50–150 jobs/day):** assume ~100 jobs/day evaluated, ~70% killed by the free pre-filter, ~30 reaching Haiku classification, ~8 reaching a Sonnet cover letter. That's ~30×$0.0025 + ~8×$0.015 ≈ $0.20/day ≈ **$6/month**; even at 150/day with heavier usage, **under $15/month**. AI is not the cost driver — infrastructure is.

### E. Infrastructure

- **Instance:** headless Chromium needs ~300–500 MB steady-state per instance (spiking to 1–1.5 GB), and LibreOffice conversion adds 300–600 MB per invocation. **t3.large (2 vCPU / 8 GB, Ubuntu) is the safe recommendation** (browserless.io: "a t3.medium or t3.large instance with 4–8 GB of RAM"); t3.medium (4 GB) is the practical floor and risks OOM under concurrent browser + LibreOffice load; t3.small (2 GB) is inadequate. **t3.medium/large are not free-tier eligible.** If the design pins Playwright ≥1.57 (which bundles Chrome for Testing and can balloon RAM to ~20 GB with 3 workers per a 2026 GitHub issue), verify actual per-instance footprint. Ubuntu is required — Playwright does not support Amazon Linux natively.
- **Chromium flags:** `--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-background-networking`; increase `/dev/shm` (`--shm-size=2g`) or use `--disable-dev-shm-usage` to avoid the 64 MB Docker default crash; reuse browser *contexts* not new browser instances; always `context.close()`/`browser.close()` in try-finally to avoid zombie processes; `--headless=new` is also harder to detect (Gap B).
- **Scheduling:** a **systemd timer** is the cleanest for a single always-on EC2 box (better logging/dependency handling than cron); EventBridge only if you move to Lambda/Fargate (not viable for long headless-browser runs on Lambda's limits).
- **Storage:** **SQLite** is sufficient and simplest for single-user, single-process daily batch (seen-jobs, answer-bank mappings, application log). Postgres only if you later add concurrency or a multi-user UI.
- **Secrets:** **AWS SSM Parameter Store (SecureString)** is the cheap default (free) for API keys; Secrets Manager only if you need rotation (~$0.40/secret/month). Avoid plaintext `.env` on the instance.
- **Observability:** structured logging to CloudWatch Logs; a daily run-summary metric (jobs seen/filtered/queued/errored).

### F. Daily Email Report

- **AWS SES** is the natural choice from EC2. **Important free-tier update:** the old 62,000/month EC2 free tier was discontinued (Aug 1, 2023). For **accounts created before July 15, 2025**, the free tier is **3,000 message charges/month for the first 12 months only**; for **accounts created on/after July 15, 2025**, there is **no 3,000-message tier — instead up to $200 in AWS Free Tier credits usable within 12 months** (per the AWS SES pricing page). Beyond that, standard rate is **$0.10 per 1,000 emails**. New accounts start in **sandbox** (200/day, verified recipients only); request production access via Service Quotas (typically 1–3 business days). For one email/day to yourself, cost is effectively $0 and sandbox is fine once you verify your own address.
- **Alternatives:** Resend, Postmark, SendGrid all have small free tiers and simpler setup; for a single daily self-email any of them works, but SES keeps everything in AWS.
- **Attachment:** generate an .xlsx with **openpyxl** (or xlsxwriter), one row per application: job title, company, company website, salary (parsed), location, remote/hybrid/onsite, proximity-to-San-Leandro score, match score, stretch-flag, status (queued/submitted/skipped), and a cover-letter file reference/link.

### G. Cover Letter + Resume Generation

- **One-page enforcement (deterministic loop):** render the generated .docx → PDF with `soffice --headless --convert-to pdf` (LibreOffice) → count pages with **pypdf** (`len(reader.pages)`). If >1, regenerate with a reduced word budget (feed the model the current word count and instruct trimming) and re-check. Word-count heuristics alone are unreliable across fonts/margins; actual page-count verification is the only robust method. Install `libreoffice libreoffice-writer` on the instance.
- **ATS-friendly formatting (2026):** single-column, standard section headings, no text boxes/tables/headers-footers for critical content, standard fonts, .docx or PDF (PDF only if the ATS accepts it), no images/icons that parsers drop. Many parsers still break on multi-column layouts and content inside headers/footers.
- **Per-role tailoring:** advisable but bounded — tailor the summary and skill emphasis to the job's keywords, but keep factual history fixed (fabrication risk). Over-tailoring that misrepresents experience is the real hazard.
- **Avoiding generic LLM tone:** feed the model specific artifacts (a concrete project from CloudBase Services, a metric, the company's actual product) and instruct it to reference one specific job-description requirement and one company-specific detail; ban clichés ("synergize", "data-driven", "I am excited to apply"); keep to 3 short paragraphs; use Jarra's real voice via a few-shot example. Recruiters detect *generic*, not *AI* — specificity is the defense. (A March 2026 Robert Half survey found 67% of US HR leaders say reviewing AI-generated applications has slowed hiring, so quality/specificity, not volume, is what converts.)

### H. Legal / Ethical (2026)

- **CFAA / scraping:** reading public job postings is low-risk in the 9th Circuit (California) per hiQ/*Van Buren*/*Meta v. Bright Data*. Bypassing auth or paywalls is a criminal-access risk everywhere.
- **ToS:** automating logged-in LinkedIn/Indeed actions violates ToS and risks account bans (civil/contract, not criminal for an individual). This is the single biggest practical exposure.
- **AI-in-hiring laws** (Illinois HB 3773 eff. Jan 1 2026 with applicant-notification duties; NYC Local Law 144 bias audits; EU AI Act fully applicable Aug 2 2026 classifying recruitment AI as "high-risk"; Colorado; Utah) regulate *employers'* use of AI to screen candidates — they do **not** prohibit a candidate using AI to write applications. No current US law bars AI-generated applications.
- **Detection & disclosure:** employers increasingly use human pattern-recognition and some detectors (GPTZero, Copyleaks, Originality.ai — none reliably >90% accurate; biased against non-native English writers and neurodivergent/structured writers). A minority of employers (notably legal/defense/finance — roughly one in five in some surveys) reject flagged applications or request disclosure; ~15% ask candidates to disclose AI assistance. Norm in 2026: AI-assisted drafting is common and accepted if the output is specific and truthful; blind mass-blasting damages reputation and floods employers (a documented complaint in HN hiring threads).
- **California specifics:** strong data-privacy regime (CCPA/CPRA) governs data *you hold on others*, not your own job search; the 9th Circuit's scraper-friendly CFAA posture is favorable to Jarra's discovery layer.

## Recommendations

**Stage 1 — Discovery + scoring MVP (build first).**
- Ingest Tier 1 ATS feeds for a curated list of ~150 Bay Area / remote target employers + HN Algolia + Remotive/RemoteOK/Arbeitnow + Adzuna + USAJobs.
- Dedup to SQLite; run the free deterministic pre-filter (salary floor $125k, location ordering Remote>Hybrid>onsite-with-San-Leandro-proximity, keyword match to target role families: Full-Stack/AI, Cloud/DevOps, Python/React/AWS/Docker, Computer Engineering, Biomedical/Embedded).
- Layer the Sharia two-stage screen (blocklist → cached Haiku industry classification against DJIM/AAOIFI-style exclusions: interest-based finance/banking/lending/credit/insurance, alcohol, weapons/defense, TV/movie/entertainment). The DJIM/AAOIFI precedent classifies these as the "haram" business-activity sectors, so encode them explicitly and cache each employer's verdict permanently to avoid re-billing.
- Add a ~5–10% random "stretch" allowance that lets through roles above experience level or adjacent pivots (e.g., biotech/biomedical) that pass all other filters.
- Emit the daily SES email + .xlsx. **Ship this before any submission automation.**

**Stage 2 — Document generation + review queue.**
- Add embedding-based resume/JD scoring and the Sonnet cover-letter generator with the LibreOffice+pypdf one-page loop.
- Build the lightweight EC2 web review UI with pre-filled deep links and the answer-bank autofill; log unmapped questions.

**Stage 3 — Cautious, narrow auto-submit (only if desired).**
- Limit to Lever public-form roles without CAPTCHA, at human pace (randomized delays, low daily volume). Keep everything else human-in-the-loop. Never automate logged-in LinkedIn/Indeed.

**Thresholds that change the plan:**
- If Tier 1+2 free sources yield <~30 relevant jobs/day, add Fantastic.jobs (~$1/1,000 jobs).
- If LLM spend exceeds ~$25/month, shift cover-letter generation to Batch API and cheaper models.
- If any auto-submit path produces a CAPTCHA or a warning, **stop that path immediately** and revert to queue-only.
- If the review queue backlog is ignored, the value is in discovery+prep, not submission — don't escalate ban risk for a step you're not using.

## Caveats
- **"Public API" ≠ "you may apply through it."** The documented Greenhouse/Ashby submit endpoints need employer API keys; an outside applicant cannot use them. Plan for browser automation of hosted forms or human submission.
- **Ban-risk figures are largely single-sourced or community-estimated** (the 23%/90-day LinkedIn figure from "ConnectSafely.ai"; Indeed 30–50/day). Treat as directional, not authoritative.
- **Browser-agent benchmark numbers are lab results and disputed** — browser-use's 89.1% was not reproduced independently (20–50% overestimation found); real ATS-form reliability under bot detection will be much lower.
- **ATS bot-detection stacks are undocumented** and change without notice; any auto-submit code will need ongoing maintenance and will break.
- **Pricing and model names move fast** — Anthropic and OpenAI both refreshed lineups multiple times in 2026; re-verify rates at build time.
- **AWS free tiers changed in 2025** — the EC2 750-hour and SES 62k tiers are gone/replaced for new accounts (new accounts get a $200 credit instead); budget t3.large at standard on-demand (~$60/month).
- **hiQ settled** — "public scraping is legal" is a CFAA statement, not blanket immunity from ToS enforcement or blocking.
- **OFCCP CC-305 dates move** — confirm the current form revision (extended to July 31, 2029) at build time before hardcoding the disability self-ID text.