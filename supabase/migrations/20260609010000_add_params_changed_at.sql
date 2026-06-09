-- ─────────────────────────────────────────────────────────────────────────────
-- History-epoch boundary for edited watches.
--
-- A watch can be edited in place (dates, route, passengers). When a *material*
-- field changes, the trip the watch tracks changes, but its price_history /
-- sent_alerts rows stay attached to the same watch_id — so charts, Trends, and
-- the alert baseline would blend two different trips (and totals across a
-- passenger change aren't even comparable).
--
-- params_changed_at marks when the trip last materially changed. History/Trends/
-- sparkline queries and the alert dedup baseline filter to rows recorded AT OR
-- AFTER this timestamp, so the current trip shows clean history and a fresh
-- baseline. Old rows are kept in the DB (not deleted), just not shown as current.
--
-- NULL = never materially edited → no filter, show all history. Additive.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE watches
  ADD COLUMN IF NOT EXISTS params_changed_at timestamptz;
