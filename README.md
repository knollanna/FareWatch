# FareWatch

A flight-fare monitoring tool for a travel advisor. You set up **watches** on
specific routes/dates for clients, and FareWatch checks Duffel every couple of
hours, records the price, and emails + Slacks you (and the client) when a fare
hits the target. Each client also gets a private link to a live status page.

Deployed at **https://farewatch.annaknoll.com** (Render).

> Hotel monitoring is in progress (schema is built; the Duffel Stays integration
> is paused pending Stays access on the Duffel account).

---

## How it works (architecture)

FareWatch is two programs that share one database — they never call each other:

| Part | File(s) | Runs | Job |
|---|---|---|---|
| **Web app** | `app.py` + `templates/` | Always on (gunicorn on Render) | The admin dashboard + the public client pages. Reads/writes the DB; renders pages. |
| **Price checker** | `check_prices.py` | Every 2 hours (Render cron) | Looks up fares on Duffel, saves prices, sends alerts. |

```
            ┌─────────────┐         ┌──────────────┐
 you ─────▶ │   app.py    │         │check_prices  │ ◀── Render cron (every 2h)
 clients ─▶ │  (web app)  │         │   (cron)     │
            └──────┬──────┘         └──────┬───────┘
                   │                       │
                   ▼                       ▼
            ┌────────────────────────────────────┐
            │      Supabase (Postgres) DB         │
            └────────────────────────────────────┘
                   ▲           ▲            ▲
              Duffel (fares) SendGrid     Slack
                            (email)     (webhook)
```

---

## File-by-file

**Application code**
- `app.py` — Flask web app: login, the watch dashboard, add/edit/pause/resume/
  delete, the price-history JSON endpoint, the public `/client/<token>` pages,
  and the `/usage` page.
- `check_prices.py` — the cron job. Fetches fares, stores price history, fires
  alerts. The "automation" of FareWatch.
- `duffel.py` — flights integration. `get_lowest_fare(...)` searches Duffel and
  returns the cheapest fare + flight details. Handles rate limits.
- `alerts.py` — notifications: client fare-drop email (SendGrid), Slack message
  (webhook), and internal error email.
- `usage.py` — powers the `/usage` page (SendGrid / Duffel / Supabase / Render
  consumption).
- `duffel_stays.py` — *(not built yet)* hotel integration, Session 10B.

**Templates** (`templates/`) — `base.html` (layout), `login.html`, `index.html`
(dashboard), `client.html` (public client page), `client_not_found.html`,
`usage.html`, `add_watch.html`. Styling is one file: `static/style.css`.

**One-off / utility scripts**
- `prepare_airports.py` — downloads & filters the airport dataset into
  `static/airports.json` (used by the autocomplete). Run once; re-run only to
  refresh.
- `generate_tokens.py` — backfills client tokens for existing watches.

**Config & infra**
- `requirements.txt` — Python dependencies.
- `Procfile` — tells Render to serve with `gunicorn app:app`.
- `render.yaml` — declares the web service + cron job + their env vars.
- `.env` / `.env.example` — secrets (`.env` is gitignored; `.env.example` lists
  the keys).
- `supabase/migrations/` — the database schema as version-controlled SQL.
  See `supabase/README.md` for the migration workflow.

---

## Data model (Supabase / Postgres)

- **`watches`** — one row per flight watch: route, date window(s), passengers,
  `target_price` (stored as a **total** = per-person × passengers), trip type,
  client name/email/token, `is_active` / `is_paused`, `last_error`, booking ref.
- **`price_history`** — one row per price check (on *every* check, not just
  alerts). Price + currency + `checked_at` + flight details (airline, flight
  numbers, departure/return times, stops, connections). This is the dataset
  behind the price charts and any future trend analysis.
- **`sent_alerts`** — a log of alerts sent (drives the "alerts sent" metric and
  the once-per-new-low gating). Has a nullable `hotel_watch_id` for later.
- **`hotel_watches`**, **`hotel_price_history`** — built for hotel monitoring
  (not yet used by any code).

Every table has Row Level Security on with an "Allow all" policy: the app
authenticates itself with the shared password and talks to Supabase with the
anon key, so the DB itself doesn't restrict per-row access.

---

## Environment variables

All configuration is via env vars (local: `.env`; production: Render dashboard).

| Variable | What it does |
|---|---|
| `SUPABASE_URL` | Supabase project URL (local stack: `http://127.0.0.1:54321`). |
| `SUPABASE_ANON_KEY` | Supabase anon/public key. |
| `DUFFEL_API_TOKEN` | Duffel API token. **Test** token locally, **live** in prod. |
| `SENDGRID_API_KEY` | SendGrid key for sending alert emails. |
| `SENDER_EMAIL` | The verified "from" address (also gets a copy of every alert). |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for alerts. Optional — blank = skip. |
| `BASE_URL` | Public app URL, used to build client-dashboard links in emails. |
| `APP_PASSWORD` | The single password to log into the admin UI. |
| `FLASK_SECRET_KEY` | Long random string that signs login sessions. |
| `RENDER_API_KEY` | Optional — enables live status/last-deploy on the usage page. |

---

## Local development

We run a **full local copy of Supabase** (via the Supabase CLI + Docker) so local
work never touches production data. Local uses the **test** Duffel token; only
the Render cron uses the **live** token.

```bash
# 1. One-time: install Docker Desktop + the Supabase CLI (brew install supabase/tap/supabase)

# 2. Python deps
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 3. Start the local database stack (Postgres + API + Studio)
supabase start            # prints local SUPABASE_URL + keys; also at http://127.0.0.1:54323

# 4. Make sure .env points at the LOCAL stack + the test Duffel token (see .env)

# 5. Run the web app
python app.py             # http://127.0.0.1:5000  (password = APP_PASSWORD)

# 6. Run a price check by hand
python check_prices.py

# stop the DB when done
supabase stop
```

> Use **127.0.0.1**, not `localhost`, if the browser blocks the local app.

### Changing the database schema

Never hand-edit tables in the dashboard. Use migrations so local + prod + git
stay in sync. Full workflow in **`supabase/README.md`**; in short:

```bash
supabase migration new my_change   # write SQL in the new file
supabase migration up              # apply + test locally
git add supabase/migrations/ && git commit
supabase db push                   # apply the same change to production
```

---

## Deployment (Render)

Production runs on Render, configured by `render.yaml`:
- a **web service** (`gunicorn app:app`), and
- a **cron job** (`python check_prices.py`, schedule `0 */2 * * *` — every 2h).

Env vars are set in the Render dashboard (not committed). The custom domain
`farewatch.annaknoll.com` points at the web service via a Cloudflare CNAME.
Pushing to `main` on GitHub auto-deploys both services.

Production database changes are applied with `supabase db push` (the CLI is
linked to the prod Supabase project).

---

## Test vs production at a glance

| | Database | Duffel token |
|---|---|---|
| **Local dev** | local Supabase stack | `duffel_test_…` |
| **Production** | Supabase cloud project | `duffel_live_…` |

Keeping these separate is why local experiments and manual `check_prices.py`
runs can't pollute real client-facing data.
