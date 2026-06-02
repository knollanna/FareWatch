-- ─────────────────────────────────────────────────────────────────────────────
-- Hotel monitoring — schema (Session 10A)
-- Adds hotel_watches, hotel_price_history, and a nullable hotel_watch_id link
-- on the existing sent_alerts table. Additive only; no existing data touched.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. hotel_watches
CREATE TABLE IF NOT EXISTS hotel_watches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz DEFAULT now(),
  is_active boolean DEFAULT true,
  last_error text,
  accommodation_id text NOT NULL,
  accommodation_name text NOT NULL,
  accommodation_city text,
  accommodation_country text,
  check_in date NOT NULL,
  check_out date NOT NULL,
  guests integer NOT NULL DEFAULT 1,
  rooms integer NOT NULL DEFAULT 1,
  refundable_only boolean DEFAULT true,
  target_price_per_night_per_person decimal NOT NULL,
  watch_mode integer NOT NULL DEFAULT 1,
  preferred_rate_code text,
  preferred_rate_name text,
  consecutive_room_not_found integer DEFAULT 0,
  client_name text,
  client_email text,
  client_token text
);

-- 2. hotel_price_history
CREATE TABLE IF NOT EXISTS hotel_price_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hotel_watch_id uuid NOT NULL REFERENCES hotel_watches(id) ON DELETE CASCADE,
  checked_at timestamptz DEFAULT now(),
  total_amount decimal NOT NULL,
  per_night_amount decimal NOT NULL,
  per_night_per_person_amount decimal NOT NULL,
  currency text NOT NULL,
  nights integer NOT NULL,
  rate_name text,
  refundable boolean
);

-- 3. Link hotel watches from sent_alerts (nullable)
ALTER TABLE sent_alerts
  ADD COLUMN IF NOT EXISTS hotel_watch_id uuid REFERENCES hotel_watches(id);

-- 4. RLS — match the existing tables (app authenticates via Flask; anon key
--    is granted full access through an "Allow all" policy on every table).
ALTER TABLE hotel_watches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE hotel_price_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all" ON hotel_watches;
CREATE POLICY "Allow all" ON hotel_watches       FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all" ON hotel_price_history;
CREATE POLICY "Allow all" ON hotel_price_history FOR ALL USING (true) WITH CHECK (true);
