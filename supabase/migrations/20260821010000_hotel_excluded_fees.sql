-- ─────────────────────────────────────────────────────────────────────────────
-- Hotels: record taxes/fees that are NOT in the tracked total.
--
-- retailRate.total is "what the end user will pay" only for taxes flagged
-- `included: true`. Each entry in retailRate.taxesAndFees carries its own
-- boolean, and anything false — a resort fee, a facility fee, a city tax — is
-- collected by the property on arrival. Without this the card understates what
-- the stay costs, and a client acts on that number.
--
-- ONE aggregate column, not the breakdown, on purpose: the LiteAPI storage
-- question (docs §7) is still open, so the retained shape stays minimal. It's a
-- whole-stay figure, matching total_amount, and 0 means "nothing excluded".
--
-- Nullable + additive; existing rows stay NULL, which the UI reads as "unknown"
-- rather than "none" — those checks ran before we captured it.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE hotel_price_history
  ADD COLUMN IF NOT EXISTS excluded_fees_amount decimal;
