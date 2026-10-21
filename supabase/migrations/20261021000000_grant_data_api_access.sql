-- ─────────────────────────────────────────────────────────────────────────────
-- Grant Data API access to every table explicitly.
--
-- From 2026-10-30 Supabase stops auto-granting Data API access to new tables in
-- "public" on existing projects. Production is unaffected: its five tables keep
-- the grants they already have. The gap is anywhere the schema is REBUILT from
-- migrations — `supabase db reset`, a new project, a preview branch — which now
-- creates these tables with no grants, so the anon key (the one the app uses for
-- everything) gets "permission denied" (SQL 42501). Locally that already
-- happened before this date; docs/project-context.md §7 has the old workaround.
--
-- This migration sorts after every CREATE TABLE, so a replay grants on all five.
-- On production every statement is a no-op (the roles already hold these
-- privileges), so it is safe to push.
--
-- The app talks to Supabase with the anon key behind RLS "Allow all" policies,
-- so anon needs full row access, not read-only. authenticated and service_role
-- are included to match what production holds today; nothing here uses
-- authenticated (no Supabase Auth users). TRUNCATE/REFERENCES/TRIGGER, which
-- the old default also handed out, are left off because nothing needs them.
--
-- FUTURE TABLES: put the same GRANT lines in the migration that creates the
-- table. Nothing grants them any more.
-- ─────────────────────────────────────────────────────────────────────────────
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.watches             TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.price_history       TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.sent_alerts         TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.hotel_watches       TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.hotel_price_history TO anon, authenticated, service_role;
