# FareWatch database (Supabase) — how it works

The database schema lives in version-controlled migration files under
`supabase/migrations/`. There are **two** databases:

| Environment | Where | Used by | Duffel token |
|---|---|---|---|
| **Local dev** | your machine (`supabase start`) | local `python app.py` / `check_prices.py` | `duffel_test_…` |
| **Production** | Supabase cloud (`qelanerqtfzqsgyddfmw`) | Render (web + cron) | `duffel_live_…` |

They never share data. Local `.env` points at `http://127.0.0.1:54321`;
Render points at the cloud project.

## Daily local development

```bash
supabase start          # boot the local stack (Postgres + REST API + Studio)
python app.py           # app runs against the LOCAL database
supabase stop           # shut it down when done
```

- Local Studio (browse the DB): http://127.0.0.1:54323
- Local DB starts empty — add test watches through the UI.

## Changing the schema (the only correct way now)

Never edit tables by hand in the dashboard. Instead:

```bash
# 1. Create a new, empty migration file
supabase migration new add_something

# 2. Write your SQL in the file it created under supabase/migrations/

# 3. Apply it to your LOCAL database and test
supabase migration up          # applies new migrations to local
#   (or `supabase db reset` to rebuild local from scratch — wipes local data)

# 4. Commit the migration file to git
git add supabase/migrations/ && git commit -m "..."

# 5. Apply the same change to PRODUCTION
supabase db push               # runs any unapplied migrations on the cloud DB
```

Check both sides agree at any time:

```bash
supabase migration list        # Local and Remote columns should match
```

## One-time setup already done

- `supabase link --project-ref qelanerqtfzqsgyddfmw` (CLI linked to prod)
- `supabase migration repair --status applied 20260601000000` (baselined the
  existing prod schema so push only applies *new* migrations)
