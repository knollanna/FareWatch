-- ─────────────────────────────────────────────────────────────────────────────
-- Per-tier alerted-price tracking on sent_alerts (fixes the "swallow bug").
--
-- The alert dedup baseline used to read from price_history, which is written on
-- EVERY check regardless of whether the notification actually sent. A transient
-- SendGrid/Slack failure therefore recorded the new low, poisoning the baseline
-- so the alert was never retried — the one alert that mattered was swallowed.
--
-- These columns record the per-stop-tier price that each SUCCESSFUL alert went
-- out at (sent_alerts rows are only inserted when a channel actually sends).
-- check_prices.py now derives the "previous low" baseline from here, so a failed
-- send leaves the baseline untouched and the alert is retried on the next check.
--
-- Additive + nullable; existing rows/data untouched. Prices are TOTALS (all pax).
-- Note: rows written before this migration have NULL here, so a watch that had
-- already alerted may re-alert its current low once after deploy — expected.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE sent_alerts
  ADD COLUMN IF NOT EXISTS alerted_price_nonstop      numeric(10, 2),
  ADD COLUMN IF NOT EXISTS alerted_price_1_stop       numeric(10, 2),
  ADD COLUMN IF NOT EXISTS alerted_price_2_plus_stops numeric(10, 2);
