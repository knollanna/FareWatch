# FareWatch — Project Context & History

> A living context document for re-establishing the full picture quickly (e.g.
> after a conversation reset). For the product pitch see
> [product-overview.md](product-overview.md); for setup/run details see the main
> [README](../README.md); the **schema source of truth** is
> `supabase/migrations/`.

---

## 1. What it is

A flight-fare monitoring tool for **Anna Knoll, an independent travel advisor
(Fora Travel)**. She sets up **watches** on routes/dates for clients; FareWatch
checks Duffel every 2 hours, records prices, and emails + Slacks her (and the
client) when a fare hits target. Each client gets a private link to a live
status page. Deployed at **farewatch.annaknoll.com**.

Hotels are planned but **blocked** (see §9).

---

## 2. Architecture

Two programs sharing one database — they never call each other:

| Part | Files | Runs | Job |
|---|---|---|---|
| **Web app** | `app.py` + `templates/` | always-on (gunicorn on Render) | admin dashboard + public client pages |
| **Price checker** | `check_prices.py` | Render cron, **every 2h, 24/7** (`0 */2 * * *`) | search Duffel, store prices, send alerts |

External services: **Duffel** (flight fares), **SendGrid** (email), **Slack**
(webhook), **Supabase/Postgres** (DB), **Render** (hosting).

---

## 3. Stack & key files

- `app.py` — Flask: login, dashboard (`/`), add/edit/pause/resume/**archive**/
  delete, `/history/<id>` (JSON for charts, **public** so client pages work),
  `/client/<token>` (public client page), `/usage`, `/trends`.
- `check_prices.py` — the cron. Fetches fares, writes `price_history`, fires alerts.
- `duffel.py` — flights. `get_lowest_fare(...)` returns `(price, currency,
  flight_details, error, stop_tiers, date_prices)`. Handles rate limits.
- `alerts.py` — email (SendGrid) + Slack (Block Kit) + internal error email.
- `usage.py` — `/usage` page (SendGrid/Duffel/Supabase/Render metrics).
- `duffel_stays.py` — **not built** (hotels, blocked).
- Templates: `base.html`, `index.html` (dashboard), `client.html`, `usage.html`,
  `trends.html`, `add_watch.html`, `login.html`, `client_not_found.html`.
  One stylesheet: `static/style.css`. `static/airports.json` powers autocomplete.
- Utility scripts: `prepare_airports.py`, `generate_tokens.py`.

---

## 4. Database schema (current)

RLS is ON for every table with an "Allow all" policy (the app gates access via a
shared password + anon key).

- **`watches`** — `origin`, `destination`, `date_from`, `date_to`, `passengers`,
  `target_price` (**stored as TOTAL** = per-person × passengers), `trip_type`,
  `return_date_from/to`, `client_name/email/token`, `is_active`, `is_paused`,
  `is_archived` (closed/past), `last_error`, `booking_reference`, `booked_at`,
  `params_changed_at` (set when a *material* edit changes the trip — see §7).
- **`price_history`** — one row per check: `price` (overall cheapest total),
  `currency`, `checked_at`, flight details (`airline`, `flight_number`,
  `departing_at`, `returning_at`, `return_flight_number`, `stops_outbound`,
  `stops_inbound`, `connection_airports`), per-tier prices (`price_nonstop`,
  `price_1_stop`, `price_2_plus_stops`), `stop_tier_details` (JSONB: per-tier
  flight details), `date_prices` (JSONB: cheapest fare per departure date).
- **`sent_alerts`** — log of alerts (`watch_id`, `price`, `sent_at`,
  `hotel_watch_id`, plus `alerted_price_nonstop` / `alerted_price_1_stop` /
  `alerted_price_2_plus_stops`). The per-tier `alerted_price_*` columns record
  what each **successful** alert went out at and are the alert **dedup baseline**
  (see §7) — added 2026-06-09 to fix the swallow bug.
- **`hotel_watches`**, **`hotel_price_history`** — built, **unused** (Stays
  blocked). `sent_alerts.hotel_watch_id` links to them.

---

## 5. Environments & workflow

**Two Supabase databases + two Duffel tokens, kept separate:**

| | Database | Duffel token |
|---|---|---|
| Local dev | local Supabase stack (Docker, `supabase start`) | `duffel_test_…` (sandbox) |
| Production | Supabase cloud (`qelanerqtfzqsgyddfmw`) | `duffel_live_…` |

This split exists because local + prod once shared one DB and a local
`check_prices.py` run with the test token polluted production with fake "Duffel
Airways" fares. Now isolated.

**Schema changes go through migrations** (never hand-edit the dashboard):
```
supabase migration new <name>   # write SQL
supabase migration up           # apply locally
git add supabase/migrations/ && git commit
supabase db push                # apply to prod (CLI is linked + baselined)
```
Details in `supabase/README.md`. Local stack needs Docker Desktop running.
**Gotcha:** when adding a column the app SELECTs, push the migration to prod
**before** deploying the code, or prod 500s on the missing column.

**Deploy:** push to `main` → Render auto-deploys both web + cron. Free-tier web
spins down after ~15 min idle (cold start on next visit) — normal, and the cron
runs independently. No per-spin-up charge; metered on instance-hours (free 750/mo).
Python is pinned to **3.14.5** via `PYTHON_VERSION` in `render.yaml` (both
services), matching the local `.venv`.

**Env vars:** `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `DUFFEL_API_TOKEN`,
`SENDGRID_API_KEY`, `SENDER_EMAIL`, `SLACK_WEBHOOK_URL`, `BASE_URL`,
`APP_PASSWORD`, `FLASK_SECRET_KEY`, `RENDER_API_KEY` (optional). App password is
currently `REDACTED`.

---

## 6. Feature history (what was built, roughly in order)

1. **Foundation** — Flask app, Supabase, watches + price_history, login.
2. **Duffel integration** — `get_lowest_fare`; cron checks every N hours.
3. **Alerts** — SendGrid email with flight details + Google Flights link.
4. **Client pages** — `/client/<token>`, readable name-based tokens.
5. **Render deploy** — gunicorn + cron + custom domain (Cloudflare CNAME).
6. **UI redesign** — fonts (DM Serif/Syne/DM Mono), accent `#1D9E75`, card layout,
   metrics row, inline add form, airport autocomplete.
7. **Usage page** — live SendGrid/Duffel/Supabase/Render metrics.
8. **Slack alerts** + a fixed bug where alerts had silently stopped firing
   (previous-low was read *after* inserting the new row).
9. **Per-person pricing** — target entered per-person, stored as total; displays
   show per-person + total.
10. **Round-trip** support, **stops** info, **"book on Duffel"** + manual booking ref.
11. **Local-dev environment** — Supabase CLI + Docker, migrations, prod baseline.
12. **Hotels schema (10A)** — tables created. **10B blocked** (Stays not enabled).
13. **Stop-tier capture & display** — cheapest fare per stop level (nonstop/1/2+),
    expandable per-tier table on cards.
14. **Stops-aware alerts** — alert when ANY tier hits a new low at/below target.
15. **Trends page** — per-watch price over time, by time of day, by day of week,
    **cheapest day to fly**.
16. **Close/archive** — `is_archived`, Past-watches section, "dates passed" flag.
17. **Per-date capture** — `date_prices` (cheapest fare per departure date).
18. **Alert reliability & Duffel rate-limit fixes (2026-06-09)** — (a) alert dedup
    baseline moved from `price_history` to `sent_alerts.alerted_price_*`, so a
    failed email/Slack send is **retried** next run instead of being swallowed;
    (b) fixed the Duffel 429 backoff — `ratelimit-reset` is an RFC 2616 **HTTP
    date**, not a number; the old `float()` parse silently fell back to a ~1s wait
    so retries never waited out the window and failed; (c) call spacing 0.3s→0.6s
    to stay under the 120 req/60s search limit. Also: Render Python pinned to
    3.14.5; add-watch target label corrected to "per person".
19. **Edit-aware history epochs (2026-06-09)** — editing a watch's trip-identity
    fields (origin, destination, date windows, passengers) sets `params_changed_at`;
    history chart, sparkline, Trends, and the alert baseline all filter to rows at/
    after it, so an edited watch no longer blends two trips (critical for passenger
    changes, where totals aren't comparable). Old rows are kept, just not shown as
    current. Non-material edits (target, client name/email) don't bump the epoch.

---

## 7. Key decisions & gotchas

- **Prices are totals everywhere internally**; `target_price` is a total. UI/
  alerts show **per-person** (÷ passengers) because that's how targets are set.
- **Alert rule:** fire when a stop tier is **at/below target** AND beats the
  **lowest price we've successfully ALERTED for that tier** — the dedup baseline,
  read from `sent_alerts.alerted_price_*` (via `get_alerted_tier_lows`), **NOT**
  from `price_history`. Key gotcha (the 2026-06-09 swallow-bug fix): basing the
  baseline on *sent* alerts means a failed send records nothing in `sent_alerts`
  and is retried next run, instead of `price_history` advancing the baseline and
  silently swallowing the alert. Two gates prevent spam. Email+Slack fire together,
  each in its own try/except; `sent_alerts` is written **only if** at least one
  channel succeeds (that's what makes the retry work).
- **Editing a watch = possible new history epoch.** `edit_watch` bumps
  `watches.params_changed_at` only when a *material* field changes (route, date
  windows, passengers — `MATERIAL_WATCH_FIELDS` in app.py). All history reads
  (`/history`, sparkline via `_attach_watch_extras`, client page, Trends) and the
  alert baseline (`get_alerted_tier_lows`) filter to `>= params_changed_at` via
  `_since_params_change`, so old-trip prices don't pollute the current trip. NULL
  = never edited → no filter. Right after a material edit the card shows "no price
  yet" until the next check writes a row in the new epoch — expected.
- **"Method Not Allowed" (405) on edit/save is almost always a STALE BROWSER
  CACHE, not a server bug.** The action routes (`/edit`, `/pause`, `/archive`,
  `/delete`, `/book`, `/resume`, `/unarchive`) are **POST-only**, so any *GET* to
  them (a refresh/back-button/bookmark onto an `/edit/<id>` URL, or a stale cached
  dashboard tab submitting against old page state) returns Werkzeug's raw 405.
  Confirmed 2026-06-09: server + form markup were correct (a full POST → 302), and
  a **hard refresh (Cmd+Shift+R) fixed it**. Diagnose with the failing request's
  Method+URL in DevTools, or test in Incognito (rules out extensions/cache).
  Hardened 2026-06-09: an `@app.errorhandler(405)` now redirects any such GET to
  the dashboard (→ /login if unauthed) instead of showing the raw 405 page.
- **Flight times are airport-local** (Duffel returns local time) — do NOT convert
  to viewer timezone; that's correct/expected. Trends time-of-day uses the
  viewer's local tz (Anna = EST).
- **Duffel:** charges per *booking*, not per *search* (searches free). Rate-limited
  by speed (429s, limit 120 req/60s) — we throttle 0.6s/call (~100/min) + retry
  honoring `ratelimit-reset` (an RFC 2616 **HTTP date**, not a number/timestamp).
  **Stays not enabled** on the account.
- **Duffel `total_amount` = all passengers** (a 2-pax fare ≈ 2× 1-pax).
- **Chart.js** must be a CDN version that exists — cdnjs pruned `4.4.3` (404),
  blanking all charts; currently `4.5.0`. Charts must build after layout
  (DOMContentLoaded) or render 0-size.
- **Booking:** FareWatch only *searches*; Anna books manually on Duffel (no
  pre-fillable Duffel search URL exists — we show route/date/flight to copy).
- **Storage:** ~1 MB used of 500 MB free; per-date capture adds ~15%/row. Years
  of runway. Downsampling old data is the deferred safety net (not built yet).

---

## 8. Operational runbook

- **Local dev:** `supabase start` → `python app.py` (127.0.0.1:5000, not
  localhost) → `python check_prices.py`. `.env` points at the local stack + test
  token. Stop with `supabase stop`.
- **Run a check in prod:** Render → `farewatch-price-check` cron → **Run Now**.
- **Clean sandbox data:** identify `price_history` rows with `airline='Duffel
  Airways'` / `flight_number LIKE 'ZZ%'`.
- **Tier/per-date data** only appears on rows checked *after* the relevant feature
  deployed; older rows show "after next check" notes.

---

## 9. Current status & what's pending

**Live in production:** flight monitoring, stops-aware email+Slack alerts, client
pages, dashboard with grouping/ordering/close, Trends (incl. cheapest day to fly),
usage page.

**Pending / next:**
- **Hotels** — `duffel_stays.py` + 2 API routes + tests were scoped (Session 10B)
  but **blocked**: Duffel Stays returns "feature not enabled — contact sales."
  Anna re-sent the Duffel contact-us access request on 2026-06-09 (awaiting
  reply); a token's read/write setting does NOT grant Stays — it's an account-
  level entitlement Duffel enables. (Alternative considered: Expedia Rapid/EPS API —
  TAAP itself is a portal, not an API, so not usable.) Resume when enabled; the
  correct endpoints are `/stays/accommodation/suggestions` + `POST /stays/search`
  (NOT `/places/suggestions`, which is the flights endpoint).
- **"Cheapest day to fly over time"** view on Trends — needs a week+ of
  `date_prices` history to be meaningful.
- **Downsampling** old `price_history` — build when storage approaches a threshold.
- **Auto-close** past-date watches (currently a manual "close" button).
