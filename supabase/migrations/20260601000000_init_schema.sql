-- ─────────────────────────────────────────────────────────────────────────────
-- FareWatch initial schema (consolidated from sessions 1–8)
-- Idempotent: safe to run against a fresh local DB or the existing prod DB.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── watches ──────────────────────────────────────────────────────────────────
create table if not exists watches (
  id                uuid primary key default gen_random_uuid(),
  origin            text not null,
  destination       text not null,
  date_from         date not null,
  date_to           date not null,
  passengers        integer not null default 1,
  target_price      numeric(10, 2) not null,
  client_name       text not null,
  client_email      text not null,
  is_active         boolean not null default true,
  created_at        timestamptz not null default now(),
  trip_type         text not null default 'one_way',
  return_date_from  date,
  return_date_to    date,
  last_error        text,
  is_paused         boolean not null default false,
  booking_reference text,
  booked_at         timestamptz,
  client_token      text
);

-- ── price_history ────────────────────────────────────────────────────────────
create table if not exists price_history (
  id                   uuid primary key default gen_random_uuid(),
  watch_id             uuid not null references watches(id) on delete cascade,
  price                numeric(10, 2) not null,
  currency             text not null default 'USD',
  checked_at           timestamptz not null default now(),
  stops_outbound       integer,
  stops_inbound        integer,
  connection_airports  text,
  airline              text,
  flight_number        text,
  departing_at         text,
  returning_at         text,
  return_flight_number text
);

-- ── sent_alerts ──────────────────────────────────────────────────────────────
create table if not exists sent_alerts (
  id        uuid primary key default gen_random_uuid(),
  watch_id  uuid not null references watches(id) on delete cascade,
  price     numeric(10, 2) not null,
  sent_at   timestamptz not null default now()
);

-- ── Row Level Security ───────────────────────────────────────────────────────
alter table watches       enable row level security;
alter table price_history enable row level security;
alter table sent_alerts   enable row level security;

-- App authenticates itself via Flask login, so the anon key is allowed full
-- access. (Re-created idempotently.)
drop policy if exists "Allow all" on watches;
create policy "Allow all" on watches       for all using (true) with check (true);

drop policy if exists "Allow all" on price_history;
create policy "Allow all" on price_history for all using (true) with check (true);

drop policy if exists "Allow all" on sent_alerts;
create policy "Allow all" on sent_alerts   for all using (true) with check (true);
