# Hotels (LiteAPI) — go-live review: fixes applied & open items

> ## 🗄️ HISTORICAL — superseded
>
> Hotels went **live in production on 2026-08-21**, and much of what this document
> describes has since changed: prices are tracked **per night** (not per person),
> `rooms` is retired, refundable and cheapest rates are tracked separately with
> **alerts on the refundable one**, and the picker is built on `/data/places`.
>
> **For current behaviour read `docs/project-context.md` §7 (decisions/gotchas)
> and §9 (status).** This file is kept for the reasoning and the incident history,
> not as a description of how the system works today.

> Outcome of the pre-production review of the hotel-monitoring feature
> (2026-08-04), run against `docs/hotel-go-live-review.md`. Everything below was
> committed and is live; §1 was later **retracted** as wrong.
>
> Companion docs: `docs/hotel-go-live-review.md` (what to review) and
> `docs/project-context.md` §7 (gotchas) / §9 (go-live checklist).

## Verdict

All seven invariants in the review brief hold. Nine defects were found and
fixed; four items are left open and listed at the bottom. One of the fixes is a
**go-live blocker** (#1) — without it, step 1 of the checklist fails.

---

## Invariants — confirmed by execution

Not just read; each was demonstrated.

| # | Invariant | How it was confirmed |
|---|---|---|
| 1 | Swallow-safe alerts (dedup baseline from `sent_alerts`, not `hotel_price_history`) | Code path traced; `sent_alerts` row written only if email **or** Slack returns truthy |
| 2 | Tracks net `retailRate.total`, not `suggestedSellingPrice` | Fed a payload carrying both (600 vs 720) — parser picked **600** |
| 3 | `refundable_only` → native `refundableRatesOnly`, never with `maxRatesPerHotel` | Inspected the outgoing request body: flag present, cap absent |
| 4 | Compliance: admin-only history, minimal fields, `offerId` never persisted | `/hotel_history` returns 302→`/login` when logged out, 200 when authed; the history INSERT omits `rate_code`/`offer_id`; `client.html` has no chart. **⚠️ See "Blocker" below — the safeguards work, but the right to retain the series at all is unresolved.** |
| 5 | Shared `sent_alerts` CHECK still satisfied by flights | Against the live local DB: both-set and neither-set inserts **rejected**; flight-shaped and hotel-shaped rows **accepted** |
| 6 | `board_name` captured for comparability | Present in the parser output and the history INSERT |
| 7 | Test/prod isolation | Sandbox key local only; hotel migrations applied locally, not pushed |

Also verified: the two dedup baselines don't leak into each other —
`get_alerted_tier_lows` (flights) and `get_hotel_alerted_low` (hotels) each saw
only their own rows with both present in `sent_alerts`.

---

## Fixes applied

### 1. ~~Migration ordering — go-live blocker~~ — **RETRACTED 2026-08-21, this was wrong**
`docs/project-context.md` §9, `docs/hotel-go-live-review.md`

**The original claim (wrong):** that `20260602000000_add_hotel_tables.sql` sorts
before five migrations already applied in prod, so a plain `supabase db push`
would refuse it and `--include-all` was required.

**What was actually true.** `supabase migration list --linked` on 2026-08-21
showed **two of the three hotel migrations were already applied in prod**:

| Migration | Remote state on 2026-08-21 |
|---|---|
| `20260602000000_add_hotel_tables` | already applied (2026-06-02) |
| `20260718000000_hotel_sent_alerts` | already applied (2026-07-18) |
| `20260718010000_add_board_to_hotel_history` | pending — the only one |

There was never an ordering problem: `20260602000000` was applied *in sequence*
back on 2026-06-02, so it sits in the correct position in the remote history and
nothing sorts before the remote head. `supabase db push --linked` pushed the one
pending migration cleanly. **`--include-all` was not needed and should not be
used here.**

**Root cause of the error:** prod's state was inferred from §9's claim that hotel
migrations were "deliberately NOT pushed to prod" — which was itself stale —
combined with reasoning from the *filenames*. The remote history table was never
listed. **Check `supabase migration list --linked` before reasoning about what
prod has;** filename ordering tells you nothing about what was applied when.

**Silver lining worth keeping:** because `20260718000000` has been live since
2026-07-18, the shared `sent_alerts` CHECK has been holding in **production** for
a month of real flight alerts — stronger evidence for invariant #5 than the local
test that was run for this review.

### 2. Hotel alerts corrupted the flight dashboard's "Recent alerts"
`app.py:211` — `_get_recent_alerts`

`sent_alerts` is shared. A hotel alert row has `hotel_watch_id` set and
`watch_id` NULL, so the embedded `watches(...)` join returned nothing and the
row rendered as `? → ? — fare dropped to $180.00` in a route-shaped feed.

Now filtered with `.not_.is_("watch_id", "null")`. **Verified end-to-end**: with
one flight alert and one hotel alert in the local DB, the feed returned only the
flight row, join populated.

The "alerts sent" metric (`app.py:182`) still counts **both** — the tile isn't
route-labelled and a business-wide total is what you want there. Now commented
as deliberate so it doesn't read as drift.

### 3. `guests` < `rooms` inflated per-night-per-person
`hotel_prices.py:179, 242`

`_build_occupancies` puts at least one adult in every room, so a 1-guest/2-room
watch was **priced for 2 adults** — but the per-person figure divided by the
requested `guests` (1). That figure is the alert threshold, so the watch would
never fire.

Now divides by `priced_guests`, the adults LiteAPI actually quotes.

| guests / rooms | before | after |
|---|---|---|
| 1 / 2 | 200.00 | **100.00** |
| 2 / 1 | 100.00 | 100.00 |
| 4 / 2 | 50.00 | 50.00 |
| 3 / 1 | 66.67 | 66.67 |

### 4. No date validation on `/add_hotel`
`app.py:462`, `templates/hotels.html:214`

A reversed or same-day stay was stored, then failed on **every** cron run
forever ("check_out must be after check_in") with an error email on the first —
a silent, self-inflicted permanent error.

Now rejected server-side with a flash, plus a client-side guard. **Verified**:
a POST with reversed dates that bypasses the JS creates zero rows and flashes
"Check-out must be after check-in — hotel watch not added."; a valid POST is
accepted.

### 5. `/hotels` rendered unstyled
`templates/hotels.html:64, 148, 158`, `static/style.css:755`

`hotels.html` was the **only authenticated page that never wrapped its content
in `<div class="page">`** — the wrapper supplying `max-width: 1100px` and
padding on `/`, `/trends`, `/usage`. On top of that, five classes it used were
defined nowhere: `.card-grid` (the real one is `.cards-grid`), `.page-head`,
`.card-price`, `.card-error`, `.muted`.

The page was full-bleed edge-to-edge, cards stacked instead of gridded, errors
plain black text. Fixed by adding the `.page` wrapper, correcting
`.card-grid` → `.cards-grid`, and defining the four genuinely-new classes.
`.add-watch-btn` is `width: 100%` by default (a full-width bar on `/`), so it's
scoped back to `width: auto` inside `.page-head` or it squeezes the title.

**This has been broken on `main` the whole time** and only shows when a hotel
watch actually exists — which is why sandbox testing didn't surface it.
Confirmed fixed visually: two-column grid, contained layout, styled error
banner, admin history chart drawing correctly.

### 6. `generate_tokens.py` would silently split client links
`generate_tokens.py`

It regenerated `client_token` for `watches` only — but `_get_or_create_token`
deliberately shares one token per client **across both tables**, and
`/client/<token>` looks the client up in both. Running it would have handed the
client a new link showing flights but **no hotels**, while their old link kept
showing only hotels. Hotel alert emails embed the token too, so those would
have gone stale.

Now builds one token map across both tables and updates both.

### 7. 405 handler sent hotel actions to the wrong page
`app.py:53`

A GET to `/hotel/pause/<id>` (stale tab, back button — the scenario §7
documents) bounced to the flight dashboard. Now `/hotel*` returns to `/hotels`.
**Verified**: `/hotel/pause/abc` → `/hotels`, `/pause/abc` → `/` unchanged.

### 8. Slack alert hardcoded `$`
`alerts.py:568`

The email and stored history label prices with `rate['currency']`; Slack used a
literal `$` on all four price lines. Same string today (LiteAPI is asked for
USD), wrong the moment a watch is priced in anything else. Now uses the rate's
own currency throughout.

### 9. Rate-limit spacing skipped on the error path
`check_prices.py:319`

The `time.sleep(0.5)` sat at the **end** of the loop body, and every error path
`continue`d past it — so a run of failing watches hammered LiteAPI with no
spacing at all. Moved to the top of the loop, guarded by `if i:`.

**Verified**: three consecutively-failing watches now take 1.01s (two 0.5s
gaps); previously ~0s. Also drops the pointless trailing sleep after the last
watch.

### 10. City field force-uppercased
`templates/hotels.html:79`

`.form-field input[type="text"]` uppercases for airport codes; the city input
inherited it and displayed "OSLO". The client-name field already carried the
override, city didn't.

### 11. Doc drift — client-facing history charts
`docs/project-context.md` §9

§9 claimed hotels shipped "client-facing cards + history charts", contradicting
the compliance decision in §7 of the same doc (chart is admin-only). Now states
the restriction **and why**, so it doesn't get "restored" as a missing feature.

---

## Open items — not fixed

| Item | Where | Why it was left |
|---|---|---|
| `/usage` blind to hotel storage | `usage.py` `get_supabase_usage()` | Counts `watches` / `price_history` / `sent_alerts` only, so the 500 MB runway gauge and the deferred-downsampling trigger will understate growth. Displaying the new counts needs a `usage.html` change too — more scope than an audit fix. Small job. |
| `rate_code` falls back to `offerId` | `hotel_prices.py` `_extract_rates` | Harmless today (`rate_code` isn't persisted), but `hotel_watches.preferred_rate_code` already exists in the schema for the deferred `watch_mode=2`. Implementing that as-is would persist an `offerId` and break invariant #4. Worth a guard comment before that work starts. |
| One subsystem can crash the other | `check_prices.py` `__main__` | An unhandled exception in `check_all_watches()` skips hotels entirely. Current order is the safe one (hotels last), but separate try/excepts would isolate them properly. |
| Past-dated hotel watches check forever | `check_all_hotel_watches` | No date filter and no `is_archived` (deferred by design). A watch whose stay has passed keeps consuming calls and showing an error indefinitely. Related to the existing "auto-close past-date watches" pending item. |

---

## Blocker opened 2026-08-21 — ToS storage question

Reading the **full** Nuitée ToS (<https://liteapi.travel/terms/>) reopened the
storage question this review had treated as settled. §3 prohibits "Using Nuitee
Connect data for competitive analysis, benchmarking, or **derivative works**",
and the ToS is silent on caching/retention. A retained `hotel_price_history`
series plus a chart over it is a plausible derivative work.

The prior clearance came from LiteAPI's **support chatbot** (2026-07-18), which
does not vary a contract and contradicts the written §3. Treat it as void.

**Do not set a production `LITEAPI_KEY` until a human at LiteAPI confirms in
writing.** Full detail, quoted clauses, what is *not* in tension, and the
no-persistence fallback design are in `docs/project-context.md` §7.

Nothing above needs undoing — every fix in this document stands on its own, and
hotels are already dormant in prod, so holding costs nothing.

## Notes for go-live day

- **History accumulates only from the first cron run after you enable hotels.**
  Charts need two points, so a freshly added watch shows "Not enough data yet"
  until the second run — 4 hours at the every-2-hours schedule. Same behavior
  §8 notes for tier/per-date flight data.
- **What's stored vs. not:** `hotel_watches` (the watch) and
  `hotel_price_history` (one row per watch per run) are the history. What the
  compliance decision drops is the **raw payload** and the **`offerId`** — not
  the time series. In prod today *nothing* is stored because the tables don't
  exist yet; that's the dormant state, resolved by checklist step 1.
- **`python app.py` runs without debug, so Jinja caches templates in memory.**
  Template edits need a server restart; static CSS updates on a plain refresh.
  This cost two confusing rounds during the review.
- Port 5000 is occupied locally, so the dev server lands on a random port each
  start.

## Verification environment

Local Supabase stack (all hotel migrations applied) + the Flask dev server. All
test rows seeded during the review — hotel watches, price history, and both
alert shapes — were deleted afterwards; the pre-existing local flight watch was
untouched. Final state: `hotel_watches` 0, `hotel_price_history` 0,
`sent_alerts` 0, `watches` 1.
