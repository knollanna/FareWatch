-- ─────────────────────────────────────────────────────────────────────────────
-- Hotels: track price PER NIGHT, not per night per person.
--
-- Hotels sell room-nights. A queen room costs what it costs whether one or two
-- people sleep in it, so dividing by `guests` was a flights idiom (fares really
-- are per passenger) carried across when hotels were built alongside them. The
-- per-person figure also read as a nightly rate on the card, so a $802 two-night
-- stay for two showed as "$201/night/person" and looked like it didn't reconcile.
--
-- `rooms` is retired at the same time. Multi-room never worked: LiteAPI returns
-- one retailRate.total PER occupancy ("that specific room's price alone") and
-- expects the caller to sum them, while get_lowest_hotel_rate took min() across
-- all rates — so a 2-room watch stored the cheaper single room's price and called
-- it the stay total. Every watch to date is rooms=1, so nothing recorded is
-- affected. The column stays (NOT NULL DEFAULT 1) but is no longer written or read.
--
-- Conversions below are behaviour-preserving: a target of $250/night/person for
-- 2 guests becomes $500/night, which is the same threshold against the same rate.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. New target unit on hotel_watches
ALTER TABLE hotel_watches
  ADD COLUMN IF NOT EXISTS target_price_per_night decimal;

UPDATE hotel_watches
   SET target_price_per_night = ROUND(target_price_per_night_per_person * GREATEST(guests, 1), 2)
 WHERE target_price_per_night IS NULL
   AND target_price_per_night_per_person IS NOT NULL;

-- Backfill done, so the new column can carry the NOT NULL the old one had.
ALTER TABLE hotel_watches ALTER COLUMN target_price_per_night SET NOT NULL;

-- The old column stays for reference but must stop being required, or inserts
-- from the new code (which no longer sets it) would fail.
ALTER TABLE hotel_watches ALTER COLUMN target_price_per_night_per_person DROP NOT NULL;

-- 2. hotel_price_history keeps per_night_amount (total / nights) as the tracked
--    figure — it already exists and is exactly the new unit. The per-person
--    column just stops being written.
ALTER TABLE hotel_price_history ALTER COLUMN per_night_per_person_amount DROP NOT NULL;

-- 3. ⚠️ NOT IDEMPOTENT — this multiplies, so running it twice would double the
--    baseline. Safe as a migration (Supabase records it and never re-applies),
--    but do NOT copy this statement into a console to "re-run just in case".
--
--    sent_alerts.price is the hotel dedup baseline (get_hotel_alerted_low), and
--    for hotel rows it holds per-night-per-person. Left alone, the first reading
--    in the new unit (~$401) would be compared against an old baseline (~$200),
--    read as "not a new low", and silently suppress the alert. Convert the
--    existing rows so the baseline keeps meaning the same thing.
UPDATE sent_alerts sa
   SET price = ROUND(sa.price * GREATEST(hw.guests, 1), 2)
  FROM hotel_watches hw
 WHERE sa.hotel_watch_id = hw.id;
