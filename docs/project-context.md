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

**Hotels** work too — same idea (watch a specific hotel + dates, alert when the
nightly rate drops under target), built on **LiteAPI** after Duffel Stays turned
out to be sales-gated (see §6/§9). **Live in production since 2026-08-21**:
checking, alerts, the `/hotels` admin UI, and client-facing cards. Hotel prices
are tracked **per night** (per room, one room) — never per person; a room costs
what it costs however many people sleep in it.

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
- `hotel_prices.py` — hotels (LiteAPI). Public surface:
  `get_hotel_rate_pair(hotel_id, dates, guests)` → `{"cheapest", "refundable"}`,
  what the cron calls (two API calls, or one when the cheapest is already
  refundable); `get_lowest_hotel_rate(...)` → `(rate, error)`, the single-fetch
  primitive underneath it; `find_places(text_query)` → free-text hotel/place
  lookup for the picker; `find_hotels(city, country, hotel_name, place_id)` →
  LiteAPI hotel IDs. **No `rooms` parameter** — multi-room was retired (§7).
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
  `params_changed_at` (set when a *material* edit changes the trip — see §7),
  **`nonstop_only`** (alert on the Nonstop tier alone — see §7).
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
- **`hotel_watches`** — one hotel watch: `accommodation_id/name/city/country`
  (LiteAPI), `check_in`, `check_out`, `guests`, **`target_price_per_night`** (the
  live target), `refundable_only` (now only a *display* preference — see §7),
  `is_active`, `last_error`, `consecutive_room_not_found`, `client_name/email/token`.
  Legacy/unused, kept but not written: `rooms`, `target_price_per_night_per_person`,
  `watch_mode`, `preferred_rate_code/name`.
- **`hotel_price_history`** — one row per check. Cheapest rate overall in
  `total_amount`, **`per_night_amount`** (the tracked figure = total ÷ nights),
  `excluded_fees_amount` (taxes/fees charged at the property, not in the total),
  `rate_name`, `board_name`, `board_type`, `refundable` (describes the *cheapest*
  rate), `currency`, `nights`, `checked_at`. The cheapest **refundable** rate sits
  alongside in `refundable_total_amount` / `refundable_per_night_amount` /
  `refundable_excluded_fees_amount` / `refundable_rate_name` /
  `refundable_board_name` / `refundable_board_type` — NULL when the stay has none,
  which the UI shows as "none found", never as zero. Legacy, no longer written:
  `per_night_per_person_amount`.
- `sent_alerts.hotel_watch_id` links hotel alerts here; for those rows `price`
  holds the **refundable** nightly figure (the dedup baseline).

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
**Gotcha — migration and deploy are ONE operation, migration first.** Any column
the app *reads or writes*: push the migration before the code ships. It bit twice
in two days (2026-08-21, 2026-08-22), both times because the wording used to say
only "SELECTs":
  * a column the **web app reads** missing in prod → 500 on the page;
  * a column the **cron writes** missing in prod → `.execute()` raises, the hotel
    checker dies mid-run and the job exits non-zero. Flights survive (the two
    subsystems are isolated in `__main__`), so the symptom is a half-failed cron,
    not an obvious outage.
The reverse gap — migration applied, old code still running — is usually harmless
*except* when a migration rewrites values the old code reads (the 2026-08-21
per-person → per-night conversion rewrote `sent_alerts.price`, which would have
made the old code fire a bogus alert). Don't leave that gap across a cron run.
**Don't use `--dry-run`. Just run the push.**
```
supabase db push --linked
```
It lists exactly what it will apply and asks to confirm before doing anything, so
it gives you the same information the dry run does without the failure mode. The
dry run prints "Would push these migrations" and stops; the real run prints
"Applying migration ...". Mistaking one for the other deployed code against a
missing column three times (2026-08-21 twice, 2026-08-22), each time because the
dry run looked like it had done the job. Dropped from the procedure 2026-08-22.

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
kept in the password manager, not here — see the Render dashboard for the live value.

**Email deliverability — DNS and SendGrid settings that must not drift
(set up 2026-08-22).** A client on Yahoo never received a fare alert. SendGrid's
Activity Feed said *Delivered* with `250 ok dirdel`, so Yahoo accepted the message
at SMTP and then discarded it — a bounce would have been visible, this was not.
Cause: mail claiming to be from `travel@annaknoll.com` was DKIM-signed by
`sendgrid.net` with a `sendgrid.net` return-path, so neither identifier aligned
with the From domain. Gmail flagged it in the open as "via sendgrid.net". Yahoo
has enforced alignment since Feb 2024 and is happy to accept-then-drop.

Fixed by **SendGrid Domain Authentication** (Settings → Sender Authentication),
which added three CNAMEs at Cloudflare. Verified headers now read
`dkim=pass header.i=@annaknoll.com header.s=s1`, `spf=pass` via
`em5746.annaknoll.com`, `dmarc=pass header.from=annaknoll.com`.

Standing rules — each of these silently breaks authentication if changed:

- **Every SendGrid record in Cloudflare stays DNS-only (grey cloud).** Proxying
  makes Cloudflare answer with its own IPs; SendGrid's periodic re-validation then
  fails and mail quietly reverts to `sendgrid.net` signing. The DKIM selectors are
  not web traffic at all — receivers fetch them over DNS — and the return-path
  subdomain is SMTP. There is no origin of ours to protect, so the orange cloud
  buys nothing here.
- **Do NOT add SendGrid to the root SPF record.** It is
  `v=spf1 include:_spf.google.com ~all` and must stay that way for Workspace mail;
  the `em5746` subdomain carries its own SPF and aligns as a subdomain.
- **Click tracking is OFF and must stay off.** It rewrote every link through
  SendGrid's redirector — including `{BASE_URL}/client/{token}`, so **each client's
  access token was travelling through a third party and landing in their click
  logs**. That is the exact leak `Referrer-Policy: no-referrer` in `app.py` exists
  to prevent. The rewritten links were plain `http://` as well.
- **Open tracking is OFF.** Nothing reads it (`/usage` pulls only `requests`), it
  appended an `http://` 1×1 pixel, and Apple Mail Privacy Protection makes open
  data meaningless anyway.
- **A SendGrid 202 means queued, not delivered.** `_sendgrid_send` returns True on
  202, so the swallow-safe dedup baseline treats an accepted-then-dropped message
  as a successful send. Ground truth is the Activity Feed and the Suppressions
  lists (Bounces / Blocks / Spam Reports / Invalid), where a listed address is
  dropped silently while the API still returns 202.
- **DMARC:** still to add as `v=DMARC1; p=none; rua=mailto:travel@annaknoll.com`.
  `p=none` changes no delivery behaviour and buys aggregate reports. Do not move to
  quarantine/reject without reading those reports first.

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
      `total` + per-night in `hotel_price_history`. (Originally also stored a
      per-night-per-person figure; that unit was dropped 2026-08-21 — see §7.)
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

24. **Hotels go-live + hardening (2026-08-21/22)** — pre-production review, then
    live. Highlights, all detailed in §7: prices switched from per-night-per-person
    to **per night** (hotels sell room-nights); `rooms` retired after finding
    multi-room never worked; the picker rebuilt on `/data/places` free-text search
    after "Hilton Garden Inn" + London matched nothing (Heathrow files under
    Hounslow); taxes/fees charged **at the property** captured and surfaced;
    past-dated stays skipped; a **minimum-drop threshold** (5% and $15/night) after
    two alerts fired on a 7-cent move; and refundable vs cheapest tracked
    separately with **alerts on the refundable rate only**, after the tracked
    "cheapest" swapped a refundable queen for a non-refundable twin to save seven
    cents and called it a new low.
25. **Email authentication (2026-08-22)** — a Yahoo client stopped receiving
    alerts. SendGrid reported *Delivered*; Yahoo had accepted the message and
    silently discarded it, because mail from `travel@annaknoll.com` was signed by
    `sendgrid.net` and aligned with nothing. Fixed with SendGrid **Domain
    Authentication**; DKIM/SPF/DMARC now all pass as `annaknoll.com`. Click and
    open tracking turned **off** — click tracking had been rewriting the client
    dashboard link through SendGrid's redirector, putting each client's access
    **token** in a third party's logs. Standing rules in §5.
26. **Nonstop-only flight watches (2026-08-22)** — `watches.nonstop_only`. Alerts
    on the Nonstop tier alone, with a caveated fallback to the cheapest tier when a
    route has no direct service. See §7.

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
- **⚠️ LiteAPI storage terms — STILL UNRESOLVED; went live anyway (Anna's call,
  2026-08-21).** The production `LITEAPI_KEY` was set and hotels enabled with the
  retention question open. Nothing below has been answered by LiteAPI — the
  account being approved is not the same as them agreeing to what we store.
  **Revisit before this accumulates much history; the fallback below gets more
  expensive the longer the series grows.** ⚠️ *Anna — correct this paragraph if
  you did get a specific answer; it is written as "proceeded with it open"
  because none was recorded here.*

  **The written terms.** Read from <https://liteapi.travel/terms/> (Nuitee Connect
  ToS) on **2026-08-21**. Quoted so nobody re-litigates this from memory:
  - §3 prohibits "Using Nuitee Connect data for **competitive analysis,
    benchmarking, or derivative works**".
  - §3 also prohibits "Mapping Nuitee Connect data to third-party sources or
    datasets" and "Redistributing or reselling Nuitee Connect inventory or data".
  - §5 prohibits "To scrape, harvest, or bulk-download inventory" and "To build,
    train, or enrich third-party datasets, machine learning models, or mapping
    systems".
  - The ToS is **silent on caching/retention** — there is no "you may cache rates
    for N hours" carve-out to rely on.

  Note the public marketing summary of the terms is milder than the ToS itself
  ("exploit… beyond permitted delivery of travel services"). **The ToS is the
  operative document**; don't clear this against the summary.

  **The conflict.** `hotel_price_history` retains one row per watch per check and
  charts it over time. That is a plausible reading of a *derivative work* built
  from their rate data, and the chart is analytics on top of it. The exposure is
  the **retention**, not the checking.

  **What is NOT in tension** (so a future reader doesn't over-correct): the core
  use — looking up rates for a specific client's stay and delivering travel
  services — is squarely inside the granted license. Polling two named hotels every
  2h with backoff is not "bulk-download inventory". Tracking one property for one
  client is not competitive benchmarking. The Google Travel link in alerts builds a
  search URL from a hotel name; that is not "mapping to third-party datasets",
  though it's worth naming if we ask.

  **Why the old note was wrong.** This was previously recorded as *confirmed
  2026-07-18 via their support chatbot* — flagged even then as "not a signed legal
  opinion". A chatbot's assurance does not vary a contract, and it contradicts the
  written §3. Treat the 2026-07-18 answer as void.

  **What resolves it:** written confirmation from a *human* at LiteAPI/Nuitée,
  describing exactly what is stored — the `hotel_price_history` columns, no raw
  payload, no `offerId`, admin-only chart, one series per client watch, never shown
  as a guaranteed quote. Per `anna-tools/rules/data-sources.md`, written permission
  is the legitimate path when a provider prohibits something we need.

  **Fallback if they say no:** hotels still work without persistence — check the
  rate, alert when under target, keep only the alert record instead of a time
  series. That loses the history chart, and the "new low" dedup baseline needs
  rethinking, since `sent_alerts.price` also stores a value derived from their data.

  **Presentation safeguards already built** (keep these regardless): stored rates
  are never shown as guaranteed quotes — client cards show "lowest observed" + a
  "live, can change until booked" caveat, alerts carry the same caveat, and the
  price-history **chart is admin-only** (`/hotel_history` is `@login_required`,
  removed from the client page). Only minimal fields are stored (never the full
  payload); `offerId` is never persisted. If booking is ever added: re-shop +
  `POST /rates/prebook` before payment. `boardName`/`boardType` captured.
  `POST /hotels/min-rates` evaluated + **rejected** (too lean — no refundable
  filter, no rate/board detail). Still deferred: storing `paymentTypes`, pinning
  board per watch.
- **Past-dated hotel watches are skipped, not checked (2026-08-21).** A stay whose
  `check_out` is before today has nothing left to price; left running it called
  LiteAPI 12x/day forever, got "no rooms" every time, and left a permanent error on
  the card. `check_all_hotel_watches` now skips them and the card shows a **past**
  badge plus "Stay has ended" instead of the stale error. The comparison is
  `check_out < today`, so a stay ending *today* is still checked — deliberate slack,
  since the cron runs UTC while hotel dates are local. Hotel watches still have no
  `is_archived`, so this is the whole guard; the flights-side "auto-close past-date
  watches" item is still open and the two would sensibly be built together.
- **Flights: `watches.nonstop_only` alerts on the Nonstop tier alone (2026-08-22).**
  Some clients will only fly direct, so a 1-stop fare under target is noise. The
  stop tiers were already tracked separately (`price_history.price_nonstop` /
  `price_1_stop` / `price_2_plus_stops`, with per-tier baselines in
  `sent_alerts.alerted_price_*`), so this is a filter on which tier may ALERT — the
  same shape hotels use for refundable vs cheapest. **All tiers are still fetched,
  stored and charted**; filtering at the Duffel search instead would destroy the
  1-stop/2+ history, break the tier table and Trends, and lose the "nonstop $780 vs
  1-stop $540" context. **Fallback differs from hotels on purpose:** a route with NO
  nonstop service on those dates falls back to alerting on the single cheapest tier
  with a caveat (`NONSTOP_NOTE` in alerts.py, carried by email/text/Slack alike),
  because a flight watch going permanently silent on a route that simply has no
  direct service is worse than a caveated alert. `nonstop_only` is in
  `MATERIAL_WATCH_FIELDS`, so toggling it bumps `params_changed_at` and resets the
  baseline — **it must therefore also be set in `edit_watch`'s `updates` dict**, or
  the epoch check compares False against None and re-epochs on every single edit.
  Flights deliberately have **no** minimum-drop threshold (unlike hotels): fares
  move in dollars, not pennies.
- **Refundable and cheapest are tracked separately; alerts fire on REFUNDABLE only
  (2026-08-21).** The tracked "cheapest" rate flips between room types and between
  refundable and non-refundable, so it once swapped a refundable queen for a
  non-refundable twin to save 7c and reported it as a new low. Each check now
  fetches both and stores them side by side in one row: existing columns = cheapest
  overall, `refundable_*` = cheapest genuinely refundable (NULL when none exists —
  the UI must not render that as zero). **`target_price_per_night` and every alert
  apply to the refundable figure**; the cheapest is display context and can never
  move `sent_alerts.price`. A watch whose only rates are non-refundable therefore
  never alerts, however cheap — deliberate: a client is normally booked refundable.
  `refundable_only` is now a *display preference* (which rate the card leads with),
  not a request filter. Two LiteAPI calls per watch per check, **except** when the
  cheapest is already refundable, which skips the second. Chose parallel columns
  over a `rate_kind` discriminator with two rows: one-row-per-check keeps
  `_attach_hotel_extras` and the chart unchanged. Wart: the old `refundable`
  boolean now means "was the *cheapest* refundable", beside `refundable_*` columns —
  not renamed, because a rename breaks the deployed code the instant the migration
  lands (see 2026-08-21 cron crash).
- **Alerts need a MEANINGFUL drop (2026-08-21):** at least 5% AND at least 15/night
  below the last alerted low (`MIN_DROP_PCT` / `MIN_DROP_ABS` in check_prices.py).
  Hotel rates wobble by pennies; "any new low" fired twice in one evening on a 7c
  move. The floor binds hardest on cheap rooms (~19% at $80/night) — lower it if
  budget properties get watched. First alert for a watch still fires with no baseline.
- **Hotels are tracked PER NIGHT, not per person (2026-08-21).** Hotels sell
  room-nights — a queen room costs the same whether one or two people sleep in it.
  Per-person was a *flights* idiom (fares really are per passenger) carried across
  when hotels were built alongside them, and it also misread on the card: an $802
  two-night stay for two showed as "$201/night/person", which looks like a nightly
  rate that doesn't reconcile with the total. Target is now
  `hotel_watches.target_price_per_night`; the tracked figure is
  `hotel_price_history.per_night_amount` (total ÷ nights). Old columns kept but
  nullable and unwritten. Card shows 2dp so `nightly × nights = total` checks by eye.
- **`rooms` is retired, and multi-room never worked.** LiteAPI returns one
  `retailRate.total` **per occupancy** — "that specific room's price alone" — and
  expects the caller to sum them by `occupancyNumber`. `get_lowest_hotel_rate` took
  `min()` across all rates, so a 2-room watch would have stored the cheaper single
  room's price as the stay total, then divided it by every guest. Every watch to
  date is `rooms=1`, so nothing recorded was affected. `_build_occupancies` is now
  deliberately single-room. **If multi-room is ever wanted, it needs real work**
  (group by `occupancyNumber`, cheapest per room, sum) — not just re-adding the field.
- **`retailRate.total` includes taxes flagged `included: true` only.** Their
  `taxesAndFees` array carries a per-line `included` boolean, and anything false is
  **charged separately at the property** (city tax, resort fee). Captured since
  2026-08-21 as a single `hotel_price_history.excluded_fees_amount` (whole-stay
  sum, 0 = none, NULL = checked before we captured it) — deliberately one number
  rather than the breakdown, to keep the retained shape minimal while the storage
  question above is open. Cards and all three alert bodies append "+ X in
  taxes/fees payable at the property" when it's non-zero.
- **Hotel picker: `countryCode` is ISO-3166 alpha-2, and "UK" is not one** (GB is).
  The two-letter box makes near-misses look correct, and LiteAPI answers an
  unmatched code with **HTTP 200 and an empty list** — indistinguishable from "this
  city has no hotels". `/hotels/search` now maps UK→GB (plus EN→GB, SF→FI, EL→GR)
  and says so when a search comes back empty. Also: `/data/hotels` takes a
  **`hotelName`** loose match — without it a big city returns an arbitrary slice
  and the property you want probably isn't in it. `limit` defaults to 200 their
  side (max 5000); ours was hardcoded to 25, which hid most of any real city.
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

**Everything is live in production.** Flights: monitoring, stops-aware
email+Slack alerts, client pages, dashboard with grouping/ordering/close, Trends
(incl. cheapest day to fly), usage page, route price-history context on the
add-watch form, and **nonstop-only watches**. **Hotels (LiteAPI), live since
2026-08-21:** checking, alerts, the `/hotels` admin UI with place-based search,
and client-facing cards. Tracked **per night**, with the cheapest **refundable**
rate alerted on and the cheapest overall shown as context (§7).

The price-history **chart is admin-only** — deliberately, per the LiteAPI storage
terms in §7; the client page shows a "lowest observed" card with the live-rates
caveat and no chart.

The hotels go-live checklist (migrations → production key → confirm) is **done**;
what was learned doing it is recorded in §5 (migration/deploy ordering) and §7
(LiteAPI gotchas), and the retracted parts in `docs/hotel-go-live-fixes.md`.

---

### ⚠️ Open — the one that gets more expensive with time

**The LiteAPI storage/retention question is unresolved** (§7). Hotels went live
with it open; that was a deliberate call. Nothing LiteAPI has said covers what we
retain — the account being approved is not the same as them agreeing to it. The
no-persistence fallback is cheap now and gets less so as history accumulates, so
this is worth settling before much more accrues. §7 has the wording to send.

### Pending / next

- **`/usage` doesn't count the hotel tables.** `get_supabase_usage()` counts
  `watches` / `price_history` / `sent_alerts` only, so the 500 MB storage gauge —
  the thing that decides when downsampling becomes real — understates.
- **A SendGrid 202 means queued, not delivered** (§5). `_sendgrid_send` returns
  True on 202, and that advances the swallow-safe dedup baseline, so a message a
  provider accepts and then silently drops looks identical to a delivered one and
  is never retried. Narrow, but it is the same class of bug the baseline exists to
  prevent. Fixing it properly means the Activity API or a SendGrid event webhook.
- **DMARC is at `p=none`** (added 2026-08-22). Read the `rua` aggregate reports
  for a couple of weeks before considering `quarantine`; don't move on a hunch.
- **"Cheapest day to fly over time"** view on Trends — needs a week+ of
  `date_prices` history to be meaningful.
- **Downsampling** old `price_history` — build when storage approaches a threshold.
- **Auto-close past-date watches** (flights: currently a manual "close" button).
  Hotels already *skip* past-dated stays in the checker (§7) but have no
  `is_archived`; the two are worth building together.
- **Deferred hotel scaffolding** (§7): `watch_mode=2` / `preferred_rate_code`,
  pinning a board type per watch, and a hotel edit route (no `params_changed_at`
  epoch for hotels until there is one).
- **Duffel Stays** — abandoned as the hotel path (sales never responded); LiteAPI
  replaced it. Kept here only as history.
