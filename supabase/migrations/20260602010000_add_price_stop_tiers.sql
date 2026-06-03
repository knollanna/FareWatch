-- ─────────────────────────────────────────────────────────────────────────────
-- Capture the cheapest fare per stop level on each price check.
-- Foundational step for treating "same price, fewer stops" as a better deal.
--
-- price (the existing column) stays the overall-cheapest fare. These three add
-- the cheapest TOTAL fare available at each stop level on that check, or NULL if
-- no flight at that level was offered. "2_plus" means 2 or more stops.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE price_history
  ADD COLUMN IF NOT EXISTS price_nonstop      numeric,
  ADD COLUMN IF NOT EXISTS price_1_stop       numeric,
  ADD COLUMN IF NOT EXISTS price_2_plus_stops numeric;
