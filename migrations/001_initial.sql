-- Phase 1 subset of the spec section 5 schema.
-- Later phases add scores, applications, work_queue, answer_bank,
-- unmapped_questions, and llm_spend in their own migrations.

CREATE TABLE companies (
  company_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              TEXT NOT NULL,
  normalized_name   TEXT NOT NULL UNIQUE,
  domain            TEXT,
  ats_type          TEXT,
  board_token       TEXT,
  sharia_verdict    TEXT NOT NULL DEFAULT 'unknown',
  sharia_sector     TEXT,
  sharia_reason     TEXT,
  sharia_source     TEXT,
  sharia_decided_at TIMESTAMPTZ
);

CREATE TABLE jobs (
  job_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fingerprint   TEXT NOT NULL UNIQUE,
  company_id    BIGINT NOT NULL REFERENCES companies(company_id),
  source        TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  title         TEXT NOT NULL,
  location      TEXT,
  remote_type   TEXT,
  salary_min    INTEGER,
  salary_max    INTEGER,
  salary_source TEXT NOT NULL DEFAULT 'none',
  description   TEXT NOT NULL,
  apply_url     TEXT NOT NULL,
  posted_at     TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL,
  closed_at     TIMESTAMPTZ,
  filtered_out  BOOLEAN NOT NULL DEFAULT FALSE,
  filter_reason TEXT,
  UNIQUE(source, source_job_id),
  -- A reason is required exactly when the job was filtered. Without this,
  -- "why was this dropped?" silently becomes unanswerable.
  CONSTRAINT filter_reason_iff_filtered
    CHECK ((filtered_out AND filter_reason IS NOT NULL)
        OR (NOT filtered_out AND filter_reason IS NULL))
);
CREATE INDEX idx_jobs_company ON jobs(company_id);
CREATE INDEX idx_jobs_posted ON jobs(posted_at DESC NULLS LAST);
CREATE INDEX idx_jobs_live ON jobs(last_seen_at DESC) WHERE filtered_out = FALSE;

CREATE TABLE run_log (
  run_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at     TIMESTAMPTZ NOT NULL,
  finished_at    TIMESTAMPTZ,
  jobs_seen      INTEGER NOT NULL DEFAULT 0,
  jobs_new       INTEGER NOT NULL DEFAULT 0,
  jobs_filtered  INTEGER NOT NULL DEFAULT 0,
  errors         INTEGER NOT NULL DEFAULT 0,
  duration_ms    INTEGER,
  -- True when the run returned on its wall-clock budget with work outstanding.
  -- This is how we learn whether the budget is set sanely (spec section 13.3).
  budget_hit     BOOLEAN NOT NULL DEFAULT FALSE,
  notes          TEXT
);

CREATE TABLE http_cache (
  url           TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TIMESTAMPTZ NOT NULL
);
