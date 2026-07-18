-- ─────────────────────────────────────────────────────────────────────────────
-- Make sent_alerts usable for hotel alerts too.
--
-- sent_alerts was flights-only: watch_id was NOT NULL and hotel_watch_id (added
-- with the hotel tables) had no delete rule. A hotel alert has a hotel_watch_id
-- but no flight watch_id, so:
--   1. watch_id must become nullable, and
--   2. exactly one of (watch_id, hotel_watch_id) should be set per row, and
--   3. hotel_watch_id needs ON DELETE CASCADE so deleting a hotel watch cleans up
--      its alerts (flights' watch_id already cascades).
--
-- Existing rows all have watch_id set + hotel_watch_id null, so they satisfy the
-- new CHECK unchanged. Additive/relaxing only.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. watch_id nullable (a hotel alert row has none)
ALTER TABLE sent_alerts ALTER COLUMN watch_id DROP NOT NULL;

-- 2. hotel_watch_id cascades on delete, matching watch_id's behaviour
ALTER TABLE sent_alerts DROP CONSTRAINT IF EXISTS sent_alerts_hotel_watch_id_fkey;
ALTER TABLE sent_alerts
  ADD CONSTRAINT sent_alerts_hotel_watch_id_fkey
  FOREIGN KEY (hotel_watch_id) REFERENCES hotel_watches(id) ON DELETE CASCADE;

-- 3. Each alert belongs to exactly one watch — a flight OR a hotel, never both/neither
ALTER TABLE sent_alerts DROP CONSTRAINT IF EXISTS sent_alerts_exactly_one_watch;
ALTER TABLE sent_alerts
  ADD CONSTRAINT sent_alerts_exactly_one_watch
  CHECK (num_nonnulls(watch_id, hotel_watch_id) = 1);
