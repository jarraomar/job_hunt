# Environment reference

Every variable the system reads, where its value comes from, and the exact
command that puts it where it needs to be.

**Configuration comes from the real environment.** Nothing reads a dotenv file.
`load_settings()` reads `os.environ` and raises if `DATABASE_URL` is missing —
there is deliberately no fallback, so a misconfigured deploy fails loudly rather
than writing to a database nobody reads.

Three stores. Several variables live in more than one with **different values**:

| Store | Scope | Set with |
|---|---|---|
| Shell | local development | `export` / shell profile / `direnv` |
| Vercel | deployed runtime | `vercel env add` |
| GitHub Actions | CI/CD | `gh secret set` |

---

## Does this get me through the rest of development?

Working through §Provisioning below, stopping before Adzuna/USAJobs, covers
**every credential** the project needs through Phase 6. Nothing after that
requires a new account.

Two things it does **not** give you:

- **Phase 0 has code deliverables too** — `api/index.py`, `vercel.json`, and
  the three `.github/workflows/` files. Provisioning without them produces a
  correctly-configured project whose first deploy fails on an empty build. The
  credentials are still valid; they just have nothing to run.
- **Step 1 assumes a GitHub repo exists.** As of now the work is uncommitted
  and unpushed, so that is genuinely where to start.

**On cost:** Vercel Pro bills $20/mo from the day you upgrade, and nothing
before Phase 3 needs it. Neon, GitHub, Anthropic and Gmail SMTP are free or
usage-billed. Steps 1–2 and 4–5 can all be done now; step 3 (the Pro upgrade)
can wait until there is a UI to protect. If you would rather do it all in one
sitting, that is fine — just know the subscription starts running.

**What can't be done from the CLI.** Three things are dashboard-only, and they
are marked 🖱 below: the Pro upgrade, Deployment Protection, and creating the
Blob store. Everything else is `vercel` / `gh` / `neonctl`.

---

## Provisioning — in dependency order

The order matters. `.vercel/project.json` does not exist until the Vercel
project is created and linked, and three GitHub secrets are read out of that
file. Neon is independent and could come earlier, but its connection strings
are needed before any `vercel env add`, so it sits where it does.

```
1. GitHub repo ──► 2. CLIs ──► 3. Vercel project ──► 4. Neon ──► 5. env vars
                                       │                              │
                                 project.json ─────────────────► GH secrets
```

### Step 1 — GitHub repository

Everything downstream attaches to this.

```bash
git init                                    # if not already a repo
git add -A && git commit -m "feat: phase 1 discovery pipeline"

gh repo create job_hunt --private --source=. --push
```

**Private, not public.** `profile/` is gitignored, but the repo still describes
your job search in detail.

Confirm `Resume.pdf` and `Personalized Cover Letter.docx` are not staged — they
are in `.gitignore`, but check once:

```bash
git ls-files | grep -iE "resume|cover letter" || echo "clean"
```

### Step 2 — Command-line tools

```bash
npm i -g vercel          # the only Node dependency, and it never ships
brew install gh jq
gh auth login
vercel login
```

### Step 3 — Vercel project

```bash
vercel link              # prompts: create a new project → name it job_hunt
vercel git connect       # attaches the GitHub repo you made in step 1
```

`vercel link` writes `.vercel/project.json` (gitignored). Read the two IDs out
of it — you will need them in step 5:

```bash
jq -r '"org=\(.orgId)  project=\(.projectId)"' .vercel/project.json
# org=team_a1B2c3D4e5F6g7H8  project=prj_9zY8xW7vU6tS5rQ4
```

Then, in the dashboard:

- 🖱 **Upgrade the team to Pro** ($20/mo). This is what unlocks
  production-domain protection. On Hobby the production URL is public and this
  design does not hold — the UI shows your résumé, contact details and salary
  floor. *(Deferrable until Phase 3.)*
- **Settings → Git →** disable automatic deployments. GitHub Actions owns
  deploys so tests gate them; leaving this on means Vercel ships on every push
  regardless of whether the suite passed.
- 🖱 **Settings → Deployment Protection →** Vercel Authentication, scope
  **All Deployments**. Verify in a private window that it challenges for login.
  *(Requires Pro.)*
- **Account Settings → Tokens → Create Token**, scoped to this team. Copy it
  now; it is shown once. Looks like:
  `vercel_a1B2c3D4e5F6g7H8i9J0kL1m`

### Step 4 — Neon Postgres

<https://neon.com> → new project `jobhunt`, region **AWS us-east-1** — the same
region as Vercel's `iad1`, so queries do not cross a continent.

Then **Branches → New Branch**, name it `staging`, parent `main`. Branching is
what makes staging honest: it forks production data copy-on-write, so the UI is
exercised against realistically-shaped rows instead of an empty database.

Each branch exposes **two** connection strings that are not interchangeable:

| | Host contains | Used for | Why |
|---|---|---|---|
| **Pooled** | `-pooler` | runtime | Serverless opens a connection per instance; the direct endpoint exhausts `max_connections`. Costs session-level advisory locks and SQL-level `PREPARE` — which is why work claiming uses `FOR UPDATE SKIP LOCKED`. |
| **Direct** | *(no `-pooler`)* | migrations only | DDL wants a stable session. |

They differ by one substring, so copy carefully:

```
pooled  postgresql://jobhunt_owner:npg_R4nd0mStr1ng@ep-cool-darkness-a1b2c3d4-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
direct  postgresql://jobhunt_owner:npg_R4nd0mStr1ng@ep-cool-darkness-a1b2c3d4.us-east-1.aws.neon.tech/neondb?sslmode=require
                                                                             ^^^^^^^ present in one, absent in the other
```

You need four strings total: pooled + direct, for `main` and for `staging`.

### Step 5 — Push the values

`vercel env add` reads the value from stdin and prompts if it is a terminal, so
a pasted secret never enters shell history. One scope per invocation.

**To Vercel** — pooled strings:

```bash
vercel env add DATABASE_URL production
# ? What's the value of DATABASE_URL?
#   postgresql://jobhunt_owner:npg_R4nd0mStr1ng@ep-cool-darkness-a1b2c3d4-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require

vercel env add DATABASE_URL preview
#   ...same shape, but the staging branch's pooled string
```

**To GitHub Actions** — direct strings and the Vercel identifiers:

```bash
gh secret set NEON_DIRECT_URL_PRODUCTION    # prompts; paste main's direct string
gh secret set NEON_DIRECT_URL_STAGING       # prompts; paste staging's direct string
gh secret set VERCEL_TOKEN                  # prompts; the token from step 3

# IDs are not secret, so --body is fine here:
gh secret set VERCEL_ORG_ID     --body "$(jq -r .orgId     .vercel/project.json)"
gh secret set VERCEL_PROJECT_ID --body "$(jq -r .projectId .vercel/project.json)"
```

A `--body` argument lands in your shell history. Use it for the IDs; use the
prompt or `--body-file` for anything secret.

**CRON_SECRET** — generated rather than obtained. Vercel sends it as
`Authorization: Bearer <value>` on every cron invocation and the handler rejects
anything else; without it, anyone who guesses the URL can trigger your pipeline.

```bash
for scope in production preview; do
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
    | vercel env add CRON_SECRET "$scope"
done
# generates e.g. 8Kq2mZvR7nJx4TbW1yLdF6sHgP0aQeC3uInO5rVtXkY
```

### Step 6 — Verify

```bash
vercel env ls           # DATABASE_URL and CRON_SECRET, in production + preview
gh secret list          # five entries
```

Once `api/index.py` and `vercel.json` exist, one more check that is easy to
forget and unpleasant to discover from a silent schedule:

```bash
vercel crons run /api/cron/discover
```

Confirm it reaches the function **after** Deployment Protection is set to All
Deployments. If protection blocks it, the documented remedy is Protection Bypass
for Automation, available on Pro.

### Useful afterwards

```bash
vercel env rm DATABASE_URL production
vercel env pull .env.local      # local copy of deployed values; gitignored
gh secret delete VERCEL_TOKEN
```

Nothing in this project reads `.env.local` — `vercel env pull` is only for
inspecting what is deployed.

---

## Local development

Only `DATABASE_URL` is needed to run Phase 1, and it points at the local
cluster, never at Neon:

```bash
export DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev"
```

Put it in your shell profile, or use [`direnv`](https://direnv.net) with a
`.envrc` scoped to this directory. `.envrc` is not gitignored by default — add
it if you go that route.

`JOBHUNT_TEST_DATABASE_URL` is read only by the test suite and defaults to the
local cluster. **Never point it at Neon** — the tests `TRUNCATE`.

### What the code reads today

Eight variables. Only the first has no default.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | **none — required** | Postgres connection string |
| `JOBHUNT_SALARY_FLOOR` | `125000` | Jobs whose *known* ceiling is below this are filtered out. Unknown salary always passes. |
| `JOBHUNT_HOME_CITY` | `San Leandro` | Proximity ranking (Phase 2) |
| `JOBHUNT_HOME_STATE` | `CA` | Proximity ranking (Phase 2) |
| `JOBHUNT_RUN_BUDGET_SECONDS` | `600` | Seconds before a run returns early with `budget_hit=true`. Vercel Pro hard-kills at 800s; 600 leaves room to record the run and respond. |
| `JOBHUNT_USER_AGENT` | see `config.py` | Sent on every outbound request. Keep a working contact address in it — an operator who can reach a human is less likely to reach for an IP block. |
| `JOBHUNT_PROFILE_DIR` | `./profile` | Where `targets.yaml` and personal data live |
| `JOBHUNT_MIGRATIONS_DIR` | `./migrations` | Rarely overridden |

---

## Phase 2+ credentials

Get these in the same sitting if you like — none are read by code yet.

### Anthropic — Phase 2 (scoring), the first one you will actually need

<https://console.anthropic.com> → **Settings → API keys → Create key**. Create
**two**, named `job-hunt-production` and `job-hunt-staging`, so a staging
misfire cannot exhaust the production budget or muddy its spend data. Then
**Settings → Billing → Limits** → set a monthly cap (suggest **$25**) as a
second ceiling behind the in-code `llm_spend` cap.

Keys look like `sk-ant-api03-A1b2C3d4...` and are shown once.

```bash
vercel env add ANTHROPIC_API_KEY production   # the job-hunt-production key
vercel env add ANTHROPIC_API_KEY preview      # the job-hunt-staging key
echo "1.50" | vercel env add DAILY_LLM_CAP_USD production
```

### Gmail SMTP — Phase 5 (daily digest)

Your existing App Password over standard SMTP. No email vendor, no domain
verification, no third party holding a sending key.

An App Password is 16 characters, usually displayed in groups of four
(`abcd efgh ijkl mnop`). Google accepts either form — **strip the spaces**, or
the value looks truncated in `vercel env ls`.

```bash
echo "smtp.gmail.com"       | vercel env add SMTP_HOST         production
echo "587"                  | vercel env add SMTP_PORT         production
echo "jarraomar4@gmail.com" | vercel env add SMTP_USERNAME     production
vercel env add SMTP_PASSWORD production        # prompts: abcdefghijklmnop
echo "jarraomar4@gmail.com" | vercel env add DIGEST_TO_EMAIL   production
echo "jarraomar4@gmail.com" | vercel env add DIGEST_FROM_EMAIL production
```

Three things to know:

- **Port 587 with STARTTLS**, not 465 and not 25. Vercel functions run on
  Lambda, which blocks outbound port 25 but permits 587. Confirm the digest
  sends on the first deploy rather than waiting for a 06:05 schedule.
- **The App Password is a full-mailbox credential**, not a scoped sending key.
  A leak grants SMTP access to the account rather than to a send endpoint. It
  belongs only in Vercel env vars and is revocable at
  <https://myaccount.google.com/apppasswords>.
- Gmail's limit is ~500 recipients/day. We send one message a day.

### Vercel Blob — Phase 4 (generated PDFs)

🖱 **Project → Storage → Create Database → Blob**, name it `jobhunt-docs`.
Connecting it injects `BLOB_READ_WRITE_TOKEN` automatically — you do not set
this by hand.

Blob URLs are unguessable but not authenticated, and these documents contain
your résumé, so treat the URL itself as the secret and never render one outside
the authenticated UI.

### PROFILE_JSON — Phase 2

`profile/` is gitignored, so it is absent from a Vercel build. The data is small
and structured, so it ships as one JSON variable (Vercel's cap is 64 KB per
deployment). `scripts/sync_profile.py` will serialize and push it:

```bash
python scripts/sync_profile.py | vercel env add PROFILE_JSON production
```

### Adzuna and USAJobs — optional, skip for now

Only worth adding if the free sources stop yielding enough. USAJobs is the
better of the two: most federal postings are citizen-restricted and you are a
US-born citizen, so it has no competition from non-citizen applicants — though
pay bands often sit below the $125k floor.

```bash
vercel env add ADZUNA_APP_ID  production      # developer.adzuna.com
vercel env add ADZUNA_APP_KEY production
vercel env add USAJOBS_API_KEY    production  # developer.usajobs.gov/apirequest
vercel env add USAJOBS_USER_AGENT production  # must be the registered email
```

---

## Full variable list

| Variable | Store | Example / source | Needed by |
|---|---|---|---|
| `DATABASE_URL` | shell | `postgresql://jobhunt@localhost:5433/jobhunt_dev` | now |
| `DATABASE_URL` | Vercel prod + preview | Neon **pooled**, per branch | Phase 0 |
| `JOBHUNT_*` | optional, any | defaults in `pipeline/config.py` | — |
| `NEON_DIRECT_URL_PRODUCTION` / `_STAGING` | GitHub | Neon **direct**, per branch | Phase 0 |
| `VERCEL_TOKEN` | GitHub | `vercel_a1B2c3D4e5F6g7H8i9J0kL1m` | Phase 0 |
| `VERCEL_ORG_ID` | GitHub | `team_a1B2c3D4e5F6g7H8` | Phase 0 |
| `VERCEL_PROJECT_ID` | GitHub | `prj_9zY8xW7vU6tS5rQ4` | Phase 0 |
| `CRON_SECRET` | Vercel prod + preview | self-generated, ≥32 chars | Phase 0 |
| `ANTHROPIC_API_KEY` | Vercel prod + preview | `sk-ant-api03-…`, distinct per env | Phase 2 |
| `DAILY_LLM_CAP_USD` | Vercel prod + preview | `1.50` | Phase 2 |
| `PROFILE_JSON` | Vercel prod + preview | `scripts/sync_profile.py` | Phase 2 |
| `BLOB_READ_WRITE_TOKEN` | Vercel, auto | injected by connecting the Blob store | Phase 4 |
| `SMTP_HOST` / `SMTP_PORT` | Vercel prod | `smtp.gmail.com` / `587` | Phase 5 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Vercel prod | `jarraomar4@gmail.com` / 16-char App Password | Phase 5 |
| `DIGEST_TO_EMAIL` / `DIGEST_FROM_EMAIL` | Vercel prod | `jarraomar4@gmail.com` | Phase 5 |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Vercel prod + preview | developer.adzuna.com | optional |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | Vercel prod + preview | developer.usajobs.gov | optional |
