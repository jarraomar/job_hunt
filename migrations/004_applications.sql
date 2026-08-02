-- Phase 3: human-owned state. Deliberately separate from `jobs`, which every
-- discovery pass rewrites via ON CONFLICT -- a status column there would be
-- erased by the next repost of the same posting.

CREATE TABLE applications (
  job_id        BIGINT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  status        TEXT NOT NULL DEFAULT 'queued',
  applied_at    TIMESTAMPTZ,
  responded_at  TIMESTAMPTZ,
  interview_at  TIMESTAMPTZ,
  closed_at     TIMESTAMPTZ,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- A free-text status would break every funnel query the moment a typo
  -- reached the database.
  CONSTRAINT status_is_known CHECK (
    status IN ('queued', 'applied', 'responded', 'interview', 'rejected', 'dismissed')
  ),
  -- "applied" without a timestamp makes the conversion stats silently wrong.
  CONSTRAINT applied_has_a_timestamp CHECK (
    status IN ('queued', 'dismissed') OR applied_at IS NOT NULL
  )
);

CREATE INDEX idx_applications_status ON applications(status);
CREATE INDEX idx_applications_applied_at ON applications(applied_at) WHERE applied_at IS NOT NULL;

-- Reusable answers to the questions application forms keep asking.
CREATE TABLE answer_bank (
  answer_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question    TEXT NOT NULL UNIQUE,
  answer      TEXT NOT NULL,
  category    TEXT,
  used_count  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Questions seen on forms with no stored answer yet. Surfaced in /settings so
-- the bank grows from real forms rather than from guesses about them.
CREATE TABLE unmapped_questions (
  question    TEXT PRIMARY KEY,
  seen_count  INTEGER NOT NULL DEFAULT 1,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
