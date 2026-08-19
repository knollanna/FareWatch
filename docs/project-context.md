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

**Hotels** now work too — same idea (watch a specific hotel + dates, alert when
the nightly rate drops under target), built on **LiteAPI** after Duffel Stays
turned out to be sales-gated (see §6/§9). Advisor-facing side is done; client-
facing hotel display is the last pending piece.

---

## 2. Architecture

Two programs sharing one database — they never call each other:

| Part | Files | Runs | Job |
|---|---|---|---|
| **Web app** | `app.py` + `templates/` | always-on (gunicorn on Render) | admin dashboard + public client pages |
| **Price checker** | `check_prices.py` | Render cron, **every 2h, 24/7** (`0 */2 * * *`) | check flights (Duffel) **then** hotels (LiteAPI), store prices, send alerts |

External services: **Duffel** (flight fares), **LiteAPI/Nuitée** (hotel rates),
**SendGrid** (email), **Slack** (webhook), **Supabase/Postgres** (DB), **Render**
(hosting).

---

## 3. Stack & key files

- `app.py` — Flask: login, dashboard (`/`), add/edit/pause/resume/**archive**/
  delete, `/history/<id>` (JSON for charts, **public** so client pages work),
  `/client/<token>` (public client page), `/usage`, `/trends`.
- `check_prices.py` — the cron. Checks flights **then** hotels; writes
  `price_history` / `hotel_price_history`, fires alerts (swallow-safe).
- `duffel.py` — flights. `get_lowest_fare(...)` returns `(price, currency,
  flight_details, error, stop_tiers, date_prices)`. Handles rate limits.
- `hotel_prices.py` — hotels (LiteAPI). `get_lowest_hotel_rate(hotel_id, dates,
  guests, rooms, refundable_only)` → `(rate, error)` shaped for `hotel_price_history`;
  `find_hotels(city, country)` → hotel IDs for the add-watch picker.
- `alerts.py` — email (SendGrid) + Slack (Block Kit) + error emails, for flights
  **and** hotels (`send_hotel_alert` / `send_hotel_slack_alert` / `send_hotel_error_alert`).
- `usage.py` — `/usage` page (SendGrid/Duffel/Supabase/Render metrics).
- `duffel_stays.py` — **abandoned** (Duffel Stays sales-gated; hotels use LiteAPI instead).
- Templates: `base.html`, `index.html` (flight dashboard), `hotels.html` (hotel
  dashboard), `client.html`, `usage.html`, `trends.html`, `add_watch.html`,
  `login.html`, `client_not_found.html`. One stylesheet: `static/style.css`.
  `static/airports.json` powers flight autocomplete.
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

**Two Supabase databases + separate provider keys per env, kept isolated:**

| | Database | Duffel token | LiteAPI key |
|---|---|---|---|
| Local dev | local Supabase stack (Docker, `supabase start`) | `duffel_test_…` (sandbox) | LiteAPI **sandbox** key (test hotels, e.g. `lp1d641`) |
| Production | Supabase cloud (`qelanerqtfzqsgyddfmw`) | `duffel_live_…` | LiteAPI **production** key (set in Render) |

This split exists because local + prod once shared one DB and a local
`check_prices.py` run with the test token polluted production with fake "Duffel
Airways" fares. Same rule for hotels: keep the LiteAPI **sandbox** key local so
sandbox test-hotel rates never land in prod `hotel_price_history`. Now isolated.

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
`LITEAPI_KEY` (hotels — web needs it for the picker, cron for checking),
`SENDGRID_API_KEY`, `SENDER_EMAIL`, `SLACK_WEBHOOK_URL`, `BASE_URL`,
`APP_PASSWORD`, `FLASK_SECRET_KEY`, `RENDER_API_KEY` (optional). All declared in
`render.yaml` (`sync: false` secrets set in the Render dashboard). App password is
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
20. **Hotels on LiteAPI (2026-07-18)** — Duffel Stays never got un-gated (sales
    ignored repeated emails; Amadeus self-serve was decommissioned 2026-07-17), so
    hotels pivoted to **LiteAPI/Nuitée** (self-serve, searches free — monetises
    bookings). Built in phases:
    - **P1** `hotel_prices.py` + cron checks active `hotel_watches`, stores net
      `total` (+ per-night, per-night-per-person) in `hotel_price_history`.
    - **P2** hotel alerts (email/Slack/error), swallow-safe (baseline from
      `sent_alerts`); migration made `sent_alerts.watch_id` nullable + CHECK
      exactly-one-of(watch_id, hotel_watch_id) + hotel_watch_id cascade.
    - **P3** `/hotels` page: city→property picker (`find_hotels`), add/pause/
      resume/delete. Verified end-to-end in the running app.
    - **P4** client-facing hotel cards on `/client/<token>` (flights + hotels share
      a token by email) + `/hotel_history/<id>` Chart.js history on client & admin
      cards. Nav item renamed "watches" → "flights". Verified in the running app.
21. **Route price-history context on add-watch (2026-08-02)** — new `route_stats.py`
    aggregates `price_history` across **all watches on a route, active AND archived**,
    so a closed watch's retained history finally gets used: when adding a watch you
    see what the route has historically cost before setting a target. `route_price_stats`
    returns lowest / median (typical) / latest + observation & watch counts + date
    range (null prices dropped so a failed check never reads as a $0 low). Exposed via
    login-gated `GET /api/route-stats?origin=&destination=`; the add-watch form fetches
    it on blur once both airport codes are filled and shows a "📊 route history" box
    (hidden when there's no history). v1 is deliberately **route-wide** (no seasonality
    filtering) — `route_stats.py` holds the reusable primitives (`_route_watch_ids`,
    `_prices_for_watches`) for future trends (per-season lows, booking lead time,
    trend direction). Verified end-to-end in the running app; deployed to prod.

22. **Client-page hardening (2026-08-18)** — done ahead of advertising FareWatch
    publicly as a free lead magnet, which turns client tokens from "links held by
    people Anna knows" into "links held by strangers whose names are public".
    Tokens were `firstname-lastname-xxxx`: the name half is public and the random
    half was 4 hex chars (65,536), walkable by a script in minutes against an app
    with no rate limiting. Now `firstname-<secrets.token_urlsafe(16)>` (128 bits),
    first name only so a forwarded URL doesn't carry the surname. Plus: app-wide
    `X-Robots-Tag: noindex, nofollow, noarchive` + `/robots.txt`, app-wide
    `Referrer-Policy: no-referrer` (the client page links out, and the default
    would hand the token to the destination's logs), a 30-day post-trip expiry on
    `/client/<token>` and `/history/<id>`, first-name-only on the rendered page,
    and a 200-per-5-minutes-per-IP limit on both public routes.

23. **Client page wears the annaknoll palette (2026-08-18)** — `/client/<token>` is
    now the last screen of a funnel that starts at annaknoll.com/travel, so it uses
    that site's green/ink tokens and Faustina + Karla instead of FareWatch's own
    green/Syne. Scoped to `.client-theme` on `<body>` in `static/style.css`; the
    admin dashboard is deliberately untouched, because nobody but Anna sees it.
    Not only a recolour — the old defaults failed the accessibility contract badly
    on a white card (`--muted` #888 at 3.54:1, `--accent` #1D9E75 at 3.39:1,
    `.client-subtitle` #bbb at 1.92:1, against a 7:1 requirement). Every pair now
    measures ≥7:1. Also: `<h1>` and a `<main>` landmark where there were neither,
    the history toggle is a 44px control with a ≥3:1 edge, and chart stop-tiers get
    distinct point *shapes* as well as colours because three hues 1.2:1 apart from
    each other are identical in grayscale.

---

## 7. Key decisions & gotchas

- **Prices are totals everywhere internally**; `target_price` is a total. UI/
  alerts show **per-person** (÷ passengers) because that's how targets are set.
- **Clients created before 2026-08-18 still carry 16-bit tokens.** The entropy fix
  only affects tokens minted after it deployed, and `_get_or_create_token` reuses a
  client's existing token by email — so adding a new watch for an old client hands
  them the same short token again. Anna decided on 2026-08-18 to defer the rotation
  rather than reissue every link; running `generate_tokens.py` is what clears it.
  Note the rotation is cheaper than it looks: alert emails build the client link
  fresh from the token at send time, so the next alert re-links everyone
  automatically and only people who bookmarked the page would notice.
- **The client page has its own theme, the dashboard does not.** `.client-theme` on
  `<body>` in `client.html` / `client_not_found.html` redefines the palette tokens.
  Editing a shared component in `style.css` therefore lands on two different-looking
  pages; check both. Anything that must hold its contrast in the client theme needs
  a token, not a hex literal — the chart reads its colours out of the custom
  properties for exactly this reason.
- **A client link stops working 30 days after the last date it covers.** This is
  `CLIENT_PAGE_TTL_DAYS` in `app.py`, enforced by `_token_expired` on both
  `/client/<token>` and `/history/<id>`, and it looks exactly like a broken link
  when it fires. One live or recent watch keeps the whole page up, and adding a
  new watch for that client brings it back on its own — no token reissue needed.
  Expired and unknown tokens return the *same* 404 page on purpose, so probing
  can't confirm a token was ever real.
- **The public routes are rate-limited to 200 requests per 5 minutes per IP**
  (`PUBLIC_RATE_LIMIT` / `PUBLIC_RATE_WINDOW`, in-memory, keyed on `remote_addr`
  via ProxyFix). Sized around a real household: the client page auto-refreshes
  every 5 minutes and fires one `/history` call per watch. It is a speed bump on
  top of the token's 128 bits, not the access control. Single gunicorn worker, so
  the dict is process-global; a Render spin-down resets it, which is fine.
- **`generate_tokens.py` invalidates every existing client link.** It reissues
  all tokens across `watches` and `hotel_watches` in one pass and prints the new
  URLs. Have the "here's your new link" message ready before running it.
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
- **LiteAPI (hotels):** `POST /hotels/rates` (auth via **`X-API-Key`**), searches
  are **free** (they monetise bookings — ideal for a poller). We track the **net
  `retailRate.total`** (not `suggestedSellingPrice`); target is **per-night-per-
  person**. Refundable via `cancellationPolicies.refundableTag` (`RFN`/`NRFN`);
  `refundable_only=True` sends the native **`refundableRatesOnly`** param (server-side
  RFN filter) — do NOT also send `maxRatesPerHotel` (LiteAPI caps to the N cheapest,
  usually non-refundable, BEFORE filtering → 0 rates). We capture `boardName`/`boardType`
  (meal plan) per rate for comparability. Sandbox key returns fixed test hotels (Oslo
  `lp1d641` etc.). 429 backoff honours `Retry-After`.
- **LiteAPI storage terms (confirmed 2026-07-18 via their support chatbot — not a
  signed legal opinion):** persisting a per-hotel price-history time-series is fine
  as *analytics/price-tracking*, BUT stored rates must **not** be shown as guaranteed
  quotes (they're live shopping data). So: client cards show the current "lowest
  observed" rate + a "live, can change until booked" caveat; alerts carry the same
  caveat; the price-history **chart is admin-only** (`/hotel_history` is
  `@login_required`, removed from the client page). Store only minimal fields (never
  the full payload); don't persist `offerId` long-term. If booking is ever added:
  re-shop + `POST /rates/prebook` before payment. `boardName`/`boardType` now captured.
  `POST /hotels/min-rates` evaluated + **rejected** (too lean — no refundable filter,
  no rate/board detail). Still deferred: storing `paymentTypes`, pinning board per watch.
- **Chart.js** must be a CDN version that exists — cdnjs pruned `4.4.3` (404),
  blanking all charts; currently `4.5.0`. Charts must build after layout
  (DOMContentLoaded) or render 0-size.
- **Booking:** FareWatch only *searches*; Anna books manually on Duffel (no
  pre-fillable Duffel search URL exists — we show route/date/flight to copy).
- **Storage:** ~1 MB used of 500 MB free; per-date capture adds ~15%/row. Years
  of runway. Downsampling old data is the deferred safety net (not built yet).
- **Local Supabase: broken `postgres:17.6.1.127` image on Apple Silicon (2026-08-02).**
  Supabase CLI (through at least v2.111) pins `public.ecr.aws/supabase/postgres:17.6.1.127`,
  whose **arm64 manifest is mislabeled** — the image reports `Architecture=arm64`
  but the binaries are x86, so `supabase start` fails with the db container in an
  **exit-0 restart loop and EMPTY logs**. Symptom is silent: `docker logs` shows
  nothing; `docker run … postgres --version` gives `exec: exec format error` even
  though native arm64 images (e.g. `alpine`) run fine. **Fix:** retag a working
  build as the pinned tag — `docker tag public.ecr.aws/supabase/postgres:17.6.1.132
  public.ecr.aws/supabase/postgres:17.6.1.127` — then `supabase start` sees `.127`
  present and skips the broken pull. **Caveat:** the retag is lost if the image is
  removed/purged; a `supabase stop` + fresh pull re-fetches the broken `.127`. A
  permanent fix (pin a good postgres image in `config.toml`) is not yet done.
- **Local Supabase: `anon` lacks table GRANTs (2026-08-02).** The init migration
  enables RLS + "Allow all" policies but relies on Supabase's platform default-
  privilege grants, which **don't get applied locally** the way they do in prod —
  so `anon` (the key the app uses) gets `permission denied for table watches` (SQL
  42501) against a fresh local DB, even though prod works. **Fix (local only):**
  `docker exec supabase_db_farewatch psql -U postgres -d postgres -c "GRANT USAGE
  ON SCHEMA public TO anon, authenticated, service_role; GRANT ALL ON ALL TABLES IN
  SCHEMA public TO anon, authenticated, service_role; GRANT ALL ON ALL SEQUENCES IN
  SCHEMA public TO anon, authenticated, service_role;"`. Not needed in prod.

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
- **Local `supabase start` won't come up healthy (2026-08-02 playbook):** work
  through these in order.
  1. **Run from the project root** (`farewatch/`), not a subdir or the parent
     `Projects/` — the CLI resolves `./supabase/config.toml` from cwd, and there's a
     *separate* Supabase project in the parent dir that will grab ports 54321/54322
     and block farewatch. `supabase stop --project-id <name>` to clear a stray one.
  2. **Corrupt Docker image store** (I/O errors, "blob … input/output error",
     `docker system df` failing): a single `docker pull` reuses poisoned cached
     layers. Fix with a full purge — Docker Desktop → Troubleshoot → *Clean / Purge
     data* (in v4.84+ it's the bug icon, not in Settings), or `docker system prune
     -a --volumes`. Safe here (prod is remote Supabase + Render; local is disposable).
  3. **db in exit-0 restart loop with empty logs** = the broken `postgres:17.6.1.127`
     image — see the retag fix in §7 gotchas.
  4. **`permission denied for table` from the app/anon** = missing local grants —
     see the GRANT fix in §7 gotchas.
  5. Then `supabase db reset` to apply migrations (local DB starts empty — real data
     lives in the linked remote project, not locally).

---

## 9. Current status & what's pending

**Live in production:** flight monitoring, stops-aware email+Slack alerts, client
pages, dashboard with grouping/ordering/close, Trends (incl. cheapest day to fly),
usage page, route price-history context on the add-watch form. **Hotels (LiteAPI):** the full feature is built + deployed — checking,
alerts, `/hotels` admin UI, and client-facing cards. The price-history **chart is
admin-only** — deliberately, per the LiteAPI storage terms in §7; the client page
shows a "lowest observed" card with the live-rates caveat and no chart. **Not live
yet:** the hotel migrations aren't pushed to prod and there's no production
**`LITEAPI_KEY`** — see the go-live checklist below. Until then the prod picker/cron
no-op with "LITEAPI_KEY is not set".

**Pending / next:**
- **Hotels go-live checklist** — do these in order when ready to run hotels live.
  Hotel migrations are applied locally + committed to the repo but **deliberately
  NOT pushed to prod** (the feature is dormant, so we don't touch prod prematurely):
  1. `supabase login` (the CLI session expires) → **`supabase db push --linked
     --include-all`** to apply ALL pending hotel migrations to prod. **Do this
     first** — the checker inserts `board_name`/`board_type` etc., so prod would
     error on the missing columns. **`--include-all` is required, not optional:**
     `20260602000000_add_hotel_tables.sql` sorts *before* five migrations already
     applied in prod (`20260602010000` … `20260609010000`), and a plain `db push`
     refuses to insert migrations that predate the last row in the remote history
     table. Confirm with `supabase db push --linked --dry-run` first (read-only) —
     it lists exactly the three hotel migrations and nothing else.
  2. Set the production **`LITEAPI_KEY`** on Render (web + cron). Needs LiteAPI
     production access: business details + ToS + a card on file (free "Build" tier,
     commission-on-bookings; ToS storage question **resolved** — see §7). Use the
     **production** key, never the sandbox one (test-hotel pollution).
  3. Add a hotel watch → **Run Now** on the `farewatch-price-check` cron to confirm.
- **Duffel Stays** — abandoned as the hotel path (sales never responded); LiteAPI
  replaced it. Left here only as history.
- **"Cheapest day to fly over time"** view on Trends — needs a week+ of
  `date_prices` history to be meaningful.
- **Downsampling** old `price_history` — build when storage approaches a threshold.
- **Auto-close** past-date watches (currently a manual "close" button).
