# FareWatch

A flight-fare monitoring tool for a travel advisor. You set up **watches** on
specific routes/dates for clients, and FareWatch checks Duffel + LiteAPI every
couple of hours, records the price, and emails + Slacks you (and the client)
when a fare hits the target. Each client also gets a private link to a live
status page.

Deployed at **https://farewatch.annaknoll.com** (Render).

> **Hotel monitoring is live** (since 2026-08-21), built on **LiteAPI** — Duffel
> Stays was abandoned after it turned out to be sales-gated. Hotel rates are
> tracked **per night**, and alerts fire on the cheapest **refundable** rate.

> **LiteAPI flights are live too** (since 2026-09-07), checked alongside Duffel
> on every watch. Duffel doesn't return United or Delta even on their own
> fortress-hub routes; LiteAPI does. Both are queried every check and the
> cheaper price wins per stop tier — see `flight_merge.py`.

---

## How it works (architecture)

FareWatch is two programs that share one database — they never call each other:

| Part | File(s) | Runs | Job |
|---|---|---|---|
| **Web app** | `app.py` + `templates/` | Always on (gunicorn on Render) | The admin dashboard + the public client pages. Reads/writes the DB; renders pages. |
| **Price checker** | `check_prices.py` | Every 2 hours (Render cron) | Looks up fares on Duffel **and** LiteAPI (cheaper wins per tier), looks up hotel rates on LiteAPI, saves prices, sends alerts. |

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
                   ▲              ▲            ▲
     Duffel + LiteAPI (fares)  SendGrid     Slack
       LiteAPI (hotel rates)   (email)     (webhook)
```

---

## File-by-file

**Application code**
- `app.py` — Flask web app: login, the watch dashboard, add/edit/pause/resume/
  delete, the price-history JSON endpoint (`/history/<watch_id>` — public, no
  login required, so the client page's price chart can load it), the public
  `/client/<token>` pages (shows all watches for a token — including paused ones
  — so clients never hit a 404 just because their watches are paused), and the
  `/usage` page.
- `check_prices.py` — the cron job. Fetches fares, stores price history, fires
  alerts. The "automation" of FareWatch. Every Supabase call retries on a
  transient Gateway Timeout (`_execute_with_retry`) rather than crashing the
  run — Supabase's own API Gateway has been unreliable lately.
- `duffel.py` — flights integration. `get_lowest_fare(...)` searches Duffel and
  returns the cheapest fare + flight details, plus the cheapest price at each
  stop level (nonstop / 1-stop / 2+). A round trip is tiered by its **worst leg**
  (`_worst_leg_stops`), so one stop each way is a 1-stop trip, not a 2-stop one.
  Handles rate limits by honouring Duffel's `ratelimit-reset` Unix-timestamp
  header with up to 6 retries.
- `flight_prices.py` — the same search, via **LiteAPI**. Added to cover United
  and Delta, which Duffel doesn't return even on their own fortress-hub routes.
  `get_lowest_fare_liteapi(...)` returns the identical shape as
  `duffel.get_lowest_fare` so the two are drop-in comparable. Guards against
  LiteAPI's sandbox silently answering for the production key
  (`_is_sandbox_response`).
- `flight_merge.py` — combines a Duffel result and a LiteAPI result into one:
  every watch queries both providers each check, and the cheaper price wins per
  stop tier (tagged `source`: `"duffel"` or `"liteapi"`). Only compares
  numerically when both sides report the same currency.
- `alerts.py` — notifications: client fare-drop email (SendGrid), Slack message
  (webhook), and internal error email. Currency is passed through from Duffel
  (not assumed to be USD). The Google Flights link is built from the winning
  fare's own departure/return dates and passenger count (a free-text query with
  only one date silently gets a Google-invented return ~4 days later).
- `usage.py` — powers the `/usage` page (SendGrid / Duffel / Supabase / Render
  consumption).
- `hotel_prices.py` — hotel rates via **LiteAPI**. `get_hotel_rate_pair()` returns
  the cheapest rate and the cheapest refundable one; `find_places()` /
  `find_hotels()` back the add-watch picker.
- `route_stats.py` — aggregates `price_history` across all watches on a route
  (active and archived) so the add-watch form can show historical lowest /
  median / latest price before a target is set. Backs the login-gated
  `GET /api/route-stats` endpoint.

**Templates** (`templates/`) — `base.html` (layout), `login.html`, `index.html`
(flight dashboard), `hotels.html` (hotel dashboard), `client.html` (public client
page), `client_not_found.html`, `usage.html`, `trends.html` (per-watch price
trends), `add_watch.html`. Styling is one file: `static/style.css`.

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
  client name/email/token, `is_active` / `is_paused` / `is_archived` (closed/past),
  `last_error`, booking ref.
- **`price_history`** — one row per price check (on *every* check, not just
  alerts). Price + currency + `checked_at` + flight details (airline, flight
  numbers, departure/return times, stops, connections). Also records the
  cheapest fare at each **stop level** that check (`price_nonstop`,
  `price_1_stop`, `price_2_plus_stops`) — so a nonstop priced just above the
  cheapest connecting fare is no longer thrown away. **Rows written before
  2026-08-30 tiered round trips by summed stops**, so a 1-stop-each-way fare sits
  under `price_2_plus_stops` in older history; charts spanning that date show the
  2+ series drop and the 1-stop series appear. Also stores `stop_tier_details`
  (JSON, per-tier flight details for the expandable fare-options table) and
  `date_prices` (JSON: cheapest fare per departure date in the window, for the
  "cheapest day to fly" trend). `source` (added 2026-09-07) records which
  provider — `duffel` or `liteapi` — won the overall-cheapest fare that check;
  rows from before that migration default to `duffel`, the only source until
  then. Each stop tier's own winning source rides inside `stop_tier_details`
  instead of more columns, since a nonstop and a 1-stop can come from different
  providers on the same check. This is the dataset behind the price charts, the
  Trends page, and any future trend / stop-quality analysis.
- **`sent_alerts`** — a log of alerts sent (drives the "alerts sent" metric).
  Alerts fire per **stop tier**: when any of nonstop / 1-stop / 2+ hits a new low
  at/below the target (tracked against each tier's own price history), one
  notification names every tier that improved — unless the watch is
  `nonstop_only`, in which case only the Nonstop tier can alert. Rows are owned by
  exactly one watch: `watch_id` for flights, `hotel_watch_id` for hotels, enforced
  by a `num_nonnulls(...) = 1` check.
- **`hotel_watches`** — one hotel watch: property, dates, guests, and
  `target_price_per_night`.
- **`hotel_price_history`** — one row per check, holding both the cheapest rate
  and the cheapest **refundable** rate (`refundable_*` columns), plus any
  taxes/fees payable at the property.

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
| `LITEAPI_KEY` | LiteAPI key for hotel **and flight** rates (same key, both enabled on it). **Sandbox** locally, **production** (private key) in prod. Web needs it for the hotel picker, cron for checking both. |
| `SENDGRID_API_KEY` | SendGrid key for sending alert emails. |
| `SENDER_EMAIL` | The verified "from" address (also gets a copy of every alert). |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for alerts. Optional — blank = skip. |
| `BASE_URL` | Public app URL, used to build client-dashboard links in emails. |
| `APP_PASSWORD` | The single password to log into the admin UI. |
| `FLASK_SECRET_KEY` | Long random string that signs login sessions. |
| `RENDER_API_KEY` | Optional — enables live status/last-deploy on the usage page. |
| `PYTHON_VERSION` | Pinned to `3.13.0` in `render.yaml` for both the web service and cron job. |

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

| | Database | Duffel token | LiteAPI key |
|---|---|---|---|
| **Local dev** | local Supabase stack | `duffel_test_…` | LiteAPI **sandbox** key |
| **Production** | Supabase cloud project | `duffel_live_…` | LiteAPI **production** key |

Keeping these separate is why local experiments and manual `check_prices.py`
runs can't pollute real client-facing data. LiteAPI's environment is decided by
**which key you use, not the endpoint** — both hit the same URL, so a leftover
sandbox key returns sandbox data with no error to signal it (`flight_prices.py`
checks for this explicitly; see `_is_sandbox_response`).
