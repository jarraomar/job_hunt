-- The location screen (pipeline/filters/location.py).
--
-- Stored rather than computed on read: the queue filters on it, the settings
-- page aggregates it, and re-deriving it in SQL is not possible -- the
-- classifier is a vocabulary and a precedence order, not an expression.
--
-- Backfilled by scripts/reclassify.py, which is also how a change to
-- geography.yaml is applied to jobs already in the table.
ALTER TABLE jobs ADD COLUMN location_class TEXT NOT NULL DEFAULT 'unknown';

-- Deliberately not a CHECK constraint against a fixed list. A new class would
-- then need a migration before the code that emits it could ship, and the
-- failure mode of an unrecognised value here is a mislabelled badge rather
-- than a corrupted funnel -- unlike applications.status, which is checked.
CREATE INDEX idx_jobs_location_class ON jobs(location_class);
