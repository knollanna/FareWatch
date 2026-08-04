# Hotels (LiteAPI) — go-live code review brief

> For a fresh session reviewing the hotel-monitoring feature before it's enabled
> in production. Everything is committed to `main` and **works on sandbox**, but is
> **dormant in prod** (no production `LITEAPI_KEY`, migrations not pushed to prod).
> Flights are a separate code path and unaffected — except the **shared
> `sent_alerts` table** (see invariant #5).

## Start here
Read **`docs/project-context.md`** first — the living context doc. Most relevant:
§2 (architecture), §3 (key files), §6 item 20 (hotel phases 1–4), §7 (LiteAPI +
storage-terms gotchas), §8 (runbook), **§9 (go-live checklist)**.

## Files to review (current state on `main`)
- **`hotel_prices.py`** — LiteAPI client. `get_lowest_hotel_rate` (request build,
  `refundableRatesOnly`, 429/`Retry-After` backoff, tolerant `_extract_rates`),
  `find_hotels` (picker lookup).
- **`check_prices.py`** — `check_all_hotel_watches` (the cron path),
  `get_hotel_alerted_low` (swallow-safe dedup baseline), `set/clear_hotel_error`.
- **`alerts.py`** — `send_hotel_alert` / `send_hotel_slack_alert` /
  `send_hotel_error_alert` + `_build_hotel_alert_html/text`, `_sendgrid_send`.
- **`app.py`** — hotel routes: `/hotels`, `/hotels/search`, `/add_hotel`,
  `/hotel/{pause,resume,delete}/<id>`, `/hotel_history/<id>` (**`@login_required`**),
  `_attach_hotel_extras`; `client_page` now also loads hotels; `_get_or_create_token`
  shares a client token across `watches` + `hotel_watches`.
- **`templates/hotels.html`** — admin hotel dashboard (city→property picker JS,
  cards, admin-only history chart).
- **`templates/client.html`** — "Your hotels" section (client cards, "lowest
  observed" wording, **no** history chart).

## Hotel migrations
- `20260602000000_add_hotel_tables.sql` — `hotel_watches`, `hotel_price_history`,
  `sent_alerts.hotel_watch_id` (Session 10A schema).
- `20260718000000_hotel_sent_alerts.sql` — `sent_alerts.watch_id` nullable,
  `hotel_watch_id` ON DELETE CASCADE, CHECK exactly-one-of(watch_id, hotel_watch_id).
- `20260718010000_add_board_to_hotel_history.sql` — `board_name` / `board_type`.

## Key invariants / decisions to verify
1. **Swallow-safe alerts:** dedup baseline reads from `sent_alerts` (successful
   sends only), NOT `hotel_price_history`, so a failed send retries next run.
   `sent_alerts` row inserted only if email OR Slack succeeded.
2. **Tracks NET `retailRate.total`** (not `suggestedSellingPrice`); target is
   **per-night-per-person** (`per_night_per_person_amount`).
3. **`refundable_only` → native `refundableRatesOnly`**; must NOT be combined with
   `maxRatesPerHotel` (LiteAPI caps to N-cheapest, usually non-refundable, BEFORE
   the refundable filter → 0 rates).
4. **Compliance (per LiteAPI):** stored rates shown as "lowest observed" + "rates
   are live and can change until booked"; hotel **history is admin-only**
   (`/hotel_history` is `@login_required`, removed from client page); only minimal
   fields stored (never full payload); **`offerId` never persisted**.
5. **Shared `sent_alerts` CHECK** — verify flight alerts still satisfy
   exactly-one-of(watch_id, hotel_watch_id) (flights set `watch_id`, hotel_watch_id NULL).
6. **`board_name` captured** for meal-plan comparability (the tracked "cheapest"
   could otherwise flip between Room Only / Breakfast Included).
7. **Test/prod isolation:** sandbox key local, production key in prod; hotel
   migrations applied locally only until go-live.

## Known deferred — do NOT flag as gaps
- `watch_mode=2` (track a specific preferred rate) + `preferred_rate_code/name` +
  `consecutive_room_not_found`: scaffolded in the schema, **not implemented**
  (v1 = cheapest matching rate, `watch_mode=1`).
- Storing `paymentTypes`; pinning board type per watch.
- Hotel watches have **no edit route** yet, so no `params_changed_at` epoch (flights
  have one). Add both together if hotel editing is ever built.
- Hotel watches use only `is_active` (no `is_paused`/`is_archived`).

## Go-live steps (from §9)
1. `supabase login` → **`supabase db push`** — apply ALL pending hotel migrations to
   prod. **Do this FIRST** (the checker inserts `board_name` etc.; prod would error
   on missing columns).
2. Set the **production** `LITEAPI_KEY` on Render (web + cron) — production key,
   never the sandbox one (test-hotel pollution).
3. Add a hotel watch → **Run Now** on the `farewatch-price-check` cron to confirm.

## Running locally for review/testing
- `supabase start` (needs Docker). If `anon` "permission denied for table …", apply
  the local GRANT fix in §7. If the postgres image pull is broken, see the §7 image-pin note.
- `.env` holds the LiteAPI **sandbox** key → fixed test hotels (Oslo `lp1d641` etc.).
- Web: run the dev server (`flask --app app run` / preview), log in with password `REDACTED`.
- Exercise `/hotels`: click **+ add hotel watch** → city "Oslo", country "NO" →
  search → pick a hotel → fill dates/target → add.
- Hotel checks only (skip flights/Duffel), no real emails:
  ```python
  import check_prices as cp
  cp.send_hotel_alert = lambda *a, **k: True
  cp.send_hotel_slack_alert = lambda *a, **k: False
  cp.check_all_hotel_watches()
  ```

## Commit range
Hotel feature: `d51b026` (Session 10A schema) and `43350b1 … 0ead72d` (this session,
the LiteAPI build). To diff the net-new hotel files:
```
git diff 6bce8b4..HEAD -- hotel_prices.py templates/hotels.html \
  supabase/migrations/20260602000000_add_hotel_tables.sql \
  supabase/migrations/20260718000000_hotel_sent_alerts.sql \
  supabase/migrations/20260718010000_add_board_to_hotel_history.sql
```
For the hotel-specific hunks inside shared files, review `check_prices.py`,
`alerts.py`, `app.py`, and `templates/client.html` directly (hotel sections are
clearly delimited with `# ── Hotels ──` / `{# hotels #}` markers).
