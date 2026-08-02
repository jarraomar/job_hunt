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

**Private, not public.** The repo describes your job search in detail even with
personal data removed.

Personal documents now live outside the repo (`~/.local/share/jobhunt/profile/`)
rather than relying on `.gitignore`, so there is nothing to accidentally stage.
Check once anyway:

```bash
git ls-files | grep -iE "resume|cover letter|identity" || echo "clean"
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
```

Answer **N** to "Want to modify these settings?" — `[tool.vercel]` in
`pyproject.toml` carries both the entrypoint and the build command, so dashboard
settings would only be a second place for them to drift.

There **is** a build step, but no JavaScript one:

```toml
[tool.vercel]
entrypoint = "api.index:app"

[tool.vercel.scripts]
build = "python scripts/vendor_model.py"
```

It runs after dependencies install and vendors the 65 MB ONNX embedding model
into the bundle; without it every cold start would re-download into an ephemeral
`/tmp`. Tailwind is built on your Mac and `web/static/app.css` is committed —
that is what keeps the deployment one Python function instead of two runtimes.
CI fails if the committed CSS is stale.

**Do not run `vercel git connect`.** Deploys go through GitHub Actions using a
token, so the project never needs Vercel's GitHub App installed on the repo:

```bash
vercel pull --yes --environment=production --token=$VERCEL_TOKEN
vercel build --prod
vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN
```

This is the documented path — `vercel project add` exists specifically to create
a project "without an existing Git integration". It also removes a failure mode:
with no repo connected there are no automatic deploys to disable, so Vercel can
never ship a commit whose tests failed.

What you give up: Vercel's bot commenting preview URLs on pull requests, and
auto-deploy on push. Both are things this design does from Actions anyway.
Deployments appear as CLI deploys rather than carrying commit and branch
metadata — attach it with `--meta githubCommitSha=$GITHUB_SHA` if you want it
in the dashboard. Instant Rollback is unaffected.

**The token is still a personal credential.** Scoping it to the team does not
make the deploy identity impersonal — it acts with the permissions of whoever
created it, and Vercel has no true service accounts below Enterprise. Fine for
a single-user project; rotate it if it ever leaks.

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
- **Settings → Git →** nothing to do, since the repo is never connected. If you
  later connect one, set `git.deploymentEnabled: false` in `vercel.json` rather
  than toggling the dashboard — version-controlled, and it cannot drift.
- 🖱 **Settings → Deployment Protection →** Vercel Authentication, scope
  **All Deployments**. Verify in a private window that it challenges for login.
  *(Requires Pro.)*
- **Account Settings → Tokens → Create Token**, scoped to this team. Copy it
  now; it is shown once. Plain alphanumeric, roughly 24 characters — e.g.
  `A1b2C3d4E5f6G7h8I9j0K1l2`. The CLI rejects any value containing `-` or `.`,
  which is how you can tell it apart from the OIDC token below.

  **`VERCEL_OIDC_TOKEN` is not this token.** `vercel link` and `vercel pull`
  write one into `.env.local`: a short-lived JWT for *runtime* workload identity
  federation, so a deployed function can reach AWS or GCP without static keys.
  Being a JWT it contains dots, so passing it to `--token` fails with
  *"its contents are invalid. Must not contain: '-', '.'"*. Nothing in this
  project reads `.env.local` — dotenv was removed — so the file is inert here
  and only matters to `vercel dev`. It is gitignored either way.

### Step 4 — Neon Postgres

<https://neon.com> → new project `jobhunt`, region **AWS us-east-1** — the same
region as Vercel's `iad1`, so queries do not cross a continent.

Neon names the default branch **`production`**, not `main`. Create the sibling
from the CLI rather than the dashboard — it prints the connection string on
creation, so nothing has to be copied by hand:

```bash
npx neonctl@latest auth                       # browser OAuth, one time
npx neonctl@latest branches create --name staging --parent production
```

Branching is what makes staging honest: it forks production copy-on-write, so
the schema and data come with it. Migrating `production` first means `staging`
reports "already up to date" the moment it exists.

Read the four strings back without retyping them:

```bash
npx neonctl@latest connection-string production --pooled   # → Vercel production
npx neonctl@latest connection-string production            # → GH  NEON_DIRECT_URL_PRODUCTION
npx neonctl@latest connection-string staging --pooled      # → Vercel preview
npx neonctl@latest connection-string staging               # → GH  NEON_DIRECT_URL_STAGING
```

**Both branches share one role password.** Rotating `neondb_owner` invalidates
all four strings at once, so a rotation means re-pushing four values, not two.

Each branch exposes **two** connection strings that are not interchangeable:

| | Host contains | Used for | Why |
|---|---|---|---|
| **Pooled** | `-pooler` | runtime | Serverless opens a connection per instance; the direct endpoint exhausts `max_connections`. Costs session-level advisory locks and SQL-level `PREPARE` — which is why work claiming uses `FOR UPDATE SKIP LOCKED`. |
| **Direct** | *(no `-pooler`)* | migrations only | DDL wants a stable session. |

They differ by one substring, so if you do copy them by hand, copy carefully:

```
pooled  postgresql://neondb_owner:npg_R4nd0mStr1ng@ep-cool-darkness-a1b2c3d4-pooler.c-12.us-east-1.aws.neon.tech/neondb?sslmode=require
direct  postgresql://neondb_owner:npg_R4nd0mStr1ng@ep-cool-darkness-a1b2c3d4.c-12.us-east-1.aws.neon.tech/neondb?sslmode=require
                                                                            ^^^^^^^ present in one, absent in the other
```

You need four strings total: pooled + direct, for `production` and `staging`.

### Step 5 — Push the values

`vercel env add` reads the value from stdin and prompts if it is a terminal, so
a pasted secret never enters shell history. One scope per invocation.

`--token` is only for CI. Locally, `vercel login` already stored your
credentials, so omit the flag entirely:

```bash
vercel env add DATABASE_URL production          # local: no --token
vercel env add DATABASE_URL production --token=$VERCEL_TOKEN   # CI only
```

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

### A trap: **nothing reliably keeps a file out of the upload**

This section previously recommended `excludeFiles`. That advice was wrong, and
the correction matters more than the original trap.

**Measured on 2026-08-01.** Vercel's build writes an upload manifest to
`.vercel/output/functions/python.func/.vc-config.json` under `filePathMap`.
Inspecting it is the only way to know what actually ships. It contained **1,397
files**, including `Resume.pdf`, all of `profile/`, and `.env.local` — despite
`vercel.json` listing `profile/**` and `*.pdf` under `excludeFiles`.

Two mechanisms were tested and neither worked:

| Mechanism | Result |
|---|---|
| `excludeFiles` under `functions` | **Ignored.** `maxDuration` from the *same config block* was applied, so the key matched — the exclusion simply had no effect on this bundling path. |
| `.vercelignore` | **No effect.** Manifest unchanged at 1,398 files. It also breaks `--prebuilt` deploys with a misleading `ENOENT` naming a file that plainly exists, walking to a different file on each retry. |

Phase 0 confirmed the consequence in production: a deployed run crawled board
tokens that existed only in the local `profile/targets.yaml`.

**The only mechanism that works is keeping the file outside the project
directory.** Personal data now lives in `~/.local/share/jobhunt/profile/`, which
is the compiled-in default for `JOBHUNT_PROFILE_DIR`, and its contents reach
production as `PROFILE_JSON` instead. A test asserts the default never points
back inside the repo.

`excludeFiles` is still worth setting — it governs the 500 MB *function bundle*
limit, which is a real constraint — but do not treat it as a privacy control.

**Check the manifest before any deploy that touches file layout:**

```bash
vercel build --prod
python3 -c "
import json
m = json.load(open('.vercel/output/functions/python.func/.vc-config.json'))['filePathMap']
print(f'{len(m)} files')
for p in ['profile/', 'Resume', 'seed/', '.env']:
    hits = [k for k in m if k.startswith(p)]
    print(f'  {p:12s} {len(hits)} {\"LEAK\" if hits else \"clean\"}')"
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

One script runs everything. It starts the database, applies migrations, builds
the CSS, and serves the UI with auto-reload:

```bash
./scripts/dev.sh              # http://localhost:8000
./scripts/dev.sh discover     # one crawl, then exit
./scripts/dev.sh score        # one scoring pass, then exit
```

| Route | What it shows |
|---|---|
| <http://localhost:8000/> | Ranked queue — score, salary, age, rationale, Sharia badge |
| <http://localhost:8000/tracker> | Funnel and weekly conversion |
| <http://localhost:8000/settings> | Answer bank, Sharia overrides, run log, today's model spend |

`--reload` watches `web/`, `pipeline/` and `api/`, so template and code edits
appear on refresh. **Tailwind is not watched** — after changing classes in a
template, re-run `./scripts/build_css.sh` (or just restart the script).

Nothing but `DATABASE_URL` is required, and it points at the local cluster,
never at Neon. `dev.sh` sets it for you; set it yourself only for one-off
commands:

```bash
export DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev"
```

Put it in your shell profile, or use [`direnv`](https://direnv.net) with a
`.envrc` scoped to this directory. `.envrc` is not gitignored by default — add
it if you go that route.

`JOBHUNT_TEST_DATABASE_URL` is read only by the test suite and defaults to
`jobhunt_test` on the same cluster. **Never point it at Neon** — the tests
`TRUNCATE`.

### Running against real credentials without copying them

`vercel env run` injects the deployed values for one command, so a key never
lands in a file or in shell history:

```bash
vercel env run -e production -- ./scripts/dev.sh score
```

Note that `vercel env pull` writes `DATABASE_URL="[SENSITIVE]"` — literally
that string, redacted. The pulled file cannot be sourced to reach Neon; use
`env run` instead. Sourcing it also sets `VERCEL=1`, which makes the profile
loader refuse to read from disk, exactly as it does in production.

### Two local databases

| Database | Used by | Reset with |
|---|---|---|
| `jobhunt_dev` | `dev.sh`, the report scripts | recreate by hand |
| `jobhunt_test` | the test suite (`TRUNCATE`s constantly) | `./scripts/dev_db.sh reset` |

`dev_db.sh reset` drops **only** the test database. `dev_db.sh nuke` destroys
the whole cluster and takes `jobhunt_dev` with it — that distinction exists
because the old `reset` twice wiped the corpus as collateral damage.

### What the code reads today

Only `DATABASE_URL` has no default.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | **none — required** | Postgres connection string |
| `JOBHUNT_SALARY_FLOOR` | `125000` | Jobs whose *known* ceiling is below this are filtered out. Unknown salary always passes. |
| `JOBHUNT_HOME_CITY` | `San Leandro` | Proximity ranking |
| `JOBHUNT_HOME_STATE` | `CA` | Proximity ranking |
| `JOBHUNT_RUN_BUDGET_SECONDS` | `600` | Seconds before a run returns early with `budget_hit=true`. **Coupled to `maxDuration` in `vercel.json`** — Vercel hard-kills at the ceiling and logs nothing for a killed invocation, so a budget above it can never fire. Currently 800/600 on Pro. A test asserts the two stay in sync. |
| `JOBHUNT_USER_AGENT` | see `config.py` | Sent on every outbound request. Keep a working contact address in it — an operator who can reach a human is less likely to reach for an IP block. |
| `JOBHUNT_PROFILE_DIR` | `~/.local/share/jobhunt/profile` | Résumé, competency bullets, identity, `targets.yaml`. **Outside the repo deliberately** — see the upload-manifest trap above. |
| `JOBHUNT_MIGRATIONS_DIR` | `./migrations` | Rarely overridden |
| `JOBHUNT_MODEL_CACHE` | `./model_cache` | Vendored ONNX embedding model. Written by `scripts/vendor_model.py` at build time; gitignored. |
| `DAILY_LLM_CAP_USD` | `1.50` | Hard daily ceiling, enforced against the `llm_spend` table rather than an in-process counter — a serverless tally resets on every cold start. |
| `JOBHUNT_DAILY_JUDGE_LIMIT` | `50` | Jobs sent to Haiku per run. Judging all ~800 daily would cost ~$4/day. |
| `JOBHUNT_JUDGE_PER_COMPANY_CAP` | `3` | Max judged per employer. One company supplied 23% of live postings and 17 of the top 25 by score; without this the budget goes to near-duplicate roles at one employer. |
| `JOBHUNT_TARGETS_JSON` | unset | Board tokens as JSON. Set in Vercel; on disk locally. |
| `PROFILE_JSON` | unset | The whole profile as JSON. Set in Vercel; on disk locally. |
| `ANTHROPIC_API_KEY` | unset | Optional, unlike `DATABASE_URL`: discovery is most of the work and needs no model access, so a missing key must not stop a crawl. `pipeline/llm.py` raises at the call site instead. |

---

## Access control — read before deploying the UI

**The application has no login, and none is planned.** Vercel Authentication is
the only gate. Adding a second password would mean storing a hash, handling
sessions, and getting cookie flags right, all to protect a single-user app that
already sits behind SSO.

That makes one dashboard setting load-bearing:

🖱 **Project → Settings → Deployment Protection → Vercel Authentication →
Standard Protection** (all deployments, production domain included).

**Turning this off publishes your home city, phone number, work-authorization
answers, salary floor, and every job you are considering** to anyone who finds
the hostname. It is the kind of switch that gets flipped during unrelated
debugging — "just checking whether protection is what's breaking this" — and
never flipped back.

Production-domain protection is a **Pro** feature. On Hobby the production URL
is public and this design does not hold.

Verify rather than assume:

```bash
# From a machine that is not logged in, or a private window:
curl -s -o /dev/null -w '%{http_code}\n' https://<your-production-url>/
# 401 or 307  -> protected, correct
# 200         -> PUBLIC. Stop and fix before doing anything else.
```

**Crons are unaffected.** Vercel's scheduler invokes functions internally and
bypasses Deployment Protection, so discovery and scoring keep running behind
it. Confirm rather than assume — the failure is silent, the UI keeps working
while the pipeline quietly stops:

```bash
vercel crons ls
vercel logs --since 20m | grep -iE "discover|score"
```

If protection ever does block them, the documented remedy is Protection Bypass
for Automation (Pro).

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

**Corrected.** This previously said "`profile/` is gitignored, so it is absent
from a Vercel build." Gitignoring has no bearing on what Vercel uploads — the
directory shipped anyway, which is why it now lives outside the repo entirely
(see the upload-manifest trap above).

The profile reaches production as one JSON variable instead. About 5 KB against
Vercel's 64 KB per-deployment cap. Push it to **both** scopes — production reads
the Neon production branch, preview reads staging:

```bash
./venv/bin/python scripts/sync_profile.py | vercel env add PROFILE_JSON production
./venv/bin/python scripts/sync_profile.py | vercel env add PROFILE_JSON preview
```

Re-run both after editing the résumé or the competency bullets; the deployed
copy does not track the files.

**This is your identity going into Vercel's environment store** — name, email,
phone, city, and work-authorization answers. That is deliberate and it is the
only way the deployed judge sees your résumé, but it is worth knowing rather
than discovering.

Two consequences of the judge's prompt design:

- The résumé is inside a **cached prompt prefix**, so it is sent once per
  five-minute window rather than on every call. Growing it past 4,096 tokens is
  what makes caching engage at all — see `pipeline/judge.py`.
- `scripts/measure_prefix.py` reports whether the prefix still clears that
  threshold. Run it after any résumé edit; below the line, caching silently
  no-ops and every judgement pays roughly 5× more.

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
| `VERCEL_TOKEN` | GitHub **only** | `A1b2C3d4E5f6G7h8I9j0K1l2` — personal, rotate if leaked. Not `VERCEL_OIDC_TOKEN`. | Phase 0 |
| `VERCEL_ORG_ID` | GitHub | `team_a1B2c3D4e5F6g7H8` | Phase 0 |
| `VERCEL_PROJECT_ID` | GitHub | `prj_9zY8xW7vU6tS5rQ4` | Phase 0 |
| `CRON_SECRET` | Vercel prod + preview | self-generated, ≥32 chars | Phase 0 |
| `ANTHROPIC_API_KEY` | Vercel prod + preview | `sk-ant-api03-…`, distinct per env | Phase 2 |
| `DAILY_LLM_CAP_USD` | Vercel prod + preview | `1.50` | Phase 2 |
| `PROFILE_JSON` | Vercel prod + preview | `scripts/sync_profile.py`, ~5 KB | Phase 2 |
| `JOBHUNT_TARGETS_JSON` | Vercel prod + preview | board tokens as JSON | Phase 1 |
| `JOBHUNT_DAILY_JUDGE_LIMIT` | optional | `50` — ~$0.13/day | Phase 2 |
| `JOBHUNT_JUDGE_PER_COMPANY_CAP` | optional | `3` | Phase 2 |
| `BLOB_READ_WRITE_TOKEN` | Vercel, auto | injected by connecting the Blob store | Phase 4 |
| `SMTP_HOST` / `SMTP_PORT` | Vercel prod | `smtp.gmail.com` / `587` | Phase 5 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Vercel prod | `jarraomar4@gmail.com` / 16-char App Password | Phase 5 |
| `DIGEST_TO_EMAIL` / `DIGEST_FROM_EMAIL` | Vercel prod | `jarraomar4@gmail.com` | Phase 5 |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Vercel prod + preview | developer.adzuna.com | optional |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | Vercel prod + preview | developer.usajobs.gov | optional |
