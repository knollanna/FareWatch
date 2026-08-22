-- ─────────────────────────────────────────────────────────────────────────────
-- Hotels: track the cheapest REFUNDABLE rate alongside the cheapest rate overall.
--
-- The tracked "cheapest" rate can flip between room types and between refundable
-- and non-refundable between checks. On 2026-08-21 it swapped a refundable queen
-- for a non-refundable twin to save seven cents and reported it as a new low —
-- a worse room at the same price, presented as good news.
--
-- Two kinds are now fetched and stored per check. Existing columns keep their
-- meaning (the cheapest rate overall); the refundable_* set mirrors them for the
-- cheapest genuinely-refundable rate. One row per check still holds, so nothing
-- that reads "the latest row" has to change.
--
-- Alerts fire on the REFUNDABLE price only — see docs §7. The non-refundable
-- figure is display context and never moves the dedup baseline.
--
-- NOTE the existing `refundable` boolean now reads as "was the CHEAPEST rate
-- refundable", sitting beside columns prefixed refundable_. Renaming it to
-- cheapest_is_refundable would be clearer, but a rename breaks the currently
-- deployed code the moment this migration lands and the cron dies until the
-- deploy catches up (as it did on 2026-08-21). Additive only, deliberately.
--
-- All nullable: NULL means "not captured / no refundable rate found", which the
-- UI must not render as zero.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE hotel_price_history
  ADD COLUMN IF NOT EXISTS refundable_total_amount         decimal,
  ADD COLUMN IF NOT EXISTS refundable_per_night_amount     decimal,
  ADD COLUMN IF NOT EXISTS refundable_excluded_fees_amount decimal,
  ADD COLUMN IF NOT EXISTS refundable_rate_name            text,
  ADD COLUMN IF NOT EXISTS refundable_board_name           text,
  ADD COLUMN IF NOT EXISTS refundable_board_type           text;
