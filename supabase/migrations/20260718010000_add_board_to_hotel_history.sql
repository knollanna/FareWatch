-- ─────────────────────────────────────────────────────────────────────────────
-- Board (meal plan) on hotel price history — for rate comparability.
--
-- We track the cheapest rate each check. Without recording the board type, the
-- tracked "cheapest" can silently flip between e.g. Room Only and Breakfast
-- Included from one check to the next, making the price trend apples-to-oranges.
-- LiteAPI returns boardType (code, e.g. RO/BB) + boardName (human) per rate; we
-- persist both so the history stays meaningful and the UI can show the meal plan.
--
-- Additive + nullable; older rows stay NULL.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE hotel_price_history
  ADD COLUMN IF NOT EXISTS board_name text,
  ADD COLUMN IF NOT EXISTS board_type text;
