-- ─────────────────────────────────────────────────────────────────────────────
-- "Closed / past" state for watches.
--
-- is_archived is a third end-state, distinct from pause (temporary) and delete
-- (permanent removal). Archived watches keep all their price history but are
-- hidden from the active/paused lists and the client pages, and are skipped by
-- the price-check cron. Used to close out trips whose travel dates have passed.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE watches
  ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false;
