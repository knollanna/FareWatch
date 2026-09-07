-- ─────────────────────────────────────────────────────────────────────────────
-- Track which provider won the overall-cheapest fare on each price check.
--
-- Added when LiteAPI flights got wired into the cron alongside Duffel (see
-- HANDOFF-INFLIGHT.md) to cover United and Delta, which duffel.py doesn't
-- return even on their own fortress-hub routes. Every existing row was priced
-- by Duffel (the only source until now), so the default backfills history
-- correctly rather than leaving it NULL.
--
-- Per-tier source (nonstop/1-stop/2+ can each come from a different provider
-- on the same check) rides inside the existing stop_tier_details jsonb column
-- instead of more columns here — each tier's detail dict now carries a
-- "source" key alongside airline/flight_number/etc.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE price_history
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'duffel';
