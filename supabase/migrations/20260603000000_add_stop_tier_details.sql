-- ─────────────────────────────────────────────────────────────────────────────
-- Per-stop-tier flight details for the expandable tier table on each card.
--
-- The price_* tier columns already hold the cheapest fare at each stop level.
-- This adds the cheapest offer's DETAILS at each tier (airline, flight numbers,
-- outbound/return dates, stops, connections) as JSON, so each tier row can
-- expand to show what that fare actually is. Shape:
--   { "nonstop": {airline, flight_number, departing_at, arriving_at,
--                 returning_at, return_flight_number, stops_outbound,
--                 stops_inbound, connection_airports},
--     "1_stop": {...}, "2_plus": {...} }   (a tier is null if not offered)
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE price_history
  ADD COLUMN IF NOT EXISTS stop_tier_details jsonb;
