-- ─────────────────────────────────────────────────────────────────────────────
-- Per-date price distribution.
--
-- Each check stores the cheapest fare found for EACH departure date in the
-- watch's window (not just the single winning date), as JSON:
--   { "2026-08-07": 450.50, "2026-08-08": 472.00, ... }
-- For round-trips this is keyed by OUTBOUND date (cheapest across return dates),
-- so we get a clean "which day to fly is cheapest" distribution over time without
-- storing every outbound×return combination. Prices are totals (all passengers).
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE price_history
  ADD COLUMN IF NOT EXISTS date_prices jsonb;
