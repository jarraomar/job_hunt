-- Per-source fetch scheduling.
--
-- Spec section 4.3: the cron tick is a clock, not a crawl frequency -- each
-- source carries its own interval. Remotive makes this mandatory rather than
-- optional: its own API response states "we advise max. 4 times a day...
-- excessive requests will be blocked", so a 10-minute tick that fetched it
-- every time would breach their terms within the hour.

CREATE TABLE source_state (
  source                TEXT PRIMARY KEY,
  last_fetch_started_at TIMESTAMPTZ,
  last_fetch_ok_at      TIMESTAMPTZ,
  consecutive_errors    INTEGER NOT NULL DEFAULT 0
);
