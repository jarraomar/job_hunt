-- Phase 2: scoring, judging, and the Sharia screen.

CREATE TABLE scores (
  job_id            BIGINT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  embed_similarity  DOUBLE PRECISION,
  rule_score        DOUBLE PRECISION,
  freshness_score   DOUBLE PRECISION,
  total_score       DOUBLE PRECISION NOT NULL,
  relevance_verdict TEXT,
  rationale         TEXT,
  is_stretch        BOOLEAN NOT NULL DEFAULT FALSE,
  model             TEXT,
  judged_at         TIMESTAMPTZ,
  scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- A verdict without a model is a row we cannot attribute or re-bill
  -- correctly later; a model without a verdict means a judge call was paid
  -- for and thrown away.
  CONSTRAINT verdict_and_model_together
    CHECK ((relevance_verdict IS NULL) = (model IS NULL))
);

-- The queue reads this every page load: highest first, unjudged included.
CREATE INDEX idx_scores_rank ON scores(total_score DESC);
-- Finding what still needs a judge call is the hot path in the scoring cron.
CREATE INDEX idx_scores_unjudged ON scores(total_score DESC) WHERE judged_at IS NULL;

-- Per-call spend, so the daily ceiling is enforced against recorded fact
-- rather than an in-process counter that resets on every cold start.
CREATE TABLE llm_spend (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  day                 DATE NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
  model               TEXT NOT NULL,
  purpose             TEXT NOT NULL,
  input_tokens        INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL,
  cost_usd            NUMERIC(10,6) NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_spend_day ON llm_spend(day);
