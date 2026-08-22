"""
Price-checking cron job — the heart of FareWatch's automation.

Run on a schedule by Render's cron service (every 2 hours), and runnable by hand
with `python check_prices.py`. For every active, non-paused watch it:

  1. Asks Duffel for the lowest current fare (duffel.get_lowest_fare).
  2. Saves that price (+ flight details) to the price_history table — this runs
     on EVERY check, which is what builds the long-term price trend data.
  3. Decides whether to alert. An alert fires only when the fare is at/below the
     target AND is a new all-time low for that watch (so you're not spammed with
     repeat notifications for the same price).
  4. Sends email (SendGrid) and Slack notifications, independently, and records
     the alert in sent_alerts.
  5. On a Duffel failure, stores the reason in watches.last_error and emails a
     one-off error notice (deduped so the same error doesn't email repeatedly).

This script never serves web traffic; app.py never checks prices. They share
only the Supabase database.
"""
import os
import time
import datetime
from dotenv import load_dotenv
from supabase import create_client
from duffel import get_lowest_fare
from hotel_prices import get_lowest_hotel_rate
from alerts import (
    send_alert, send_error_alert, send_slack_alert,
    send_hotel_alert, send_hotel_slack_alert, send_hotel_error_alert,
)

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)


def get_previous_lowest(watch_id):
    """Return the lowest price ever recorded for this watch, or None if no history."""
    result = (
        supabase.table("price_history")
        .select("price")
        .eq("watch_id", watch_id)
        .order("price", desc=False)
        .limit(1)
        .execute()
    )
    if result.data:
        return float(result.data[0]["price"])
    return None


def get_alerted_tier_lows(watch_id, params_changed_at=None):
    """Lowest price at each stop tier we have SUCCESSFULLY ALERTED for this watch.

    Reads sent_alerts, whose rows are written ONLY when an alert actually goes
    out (email or Slack). Basing the dedup baseline here — rather than on
    price_history, which records every check — means a failed send never advances
    the baseline, so the alert is retried on the next check instead of being
    silently swallowed.

    If params_changed_at is set (the watch's trip was materially edited), only
    alerts sent at/after that point count, so the new trip starts with a fresh
    baseline instead of inheriting the old trip's lows (which may not even be
    comparable, e.g. after a passenger-count change).

    Returns {"nonstop", "1_stop", "2_plus"} -> float or None.
    """
    cols = {
        "nonstop": "alerted_price_nonstop",
        "1_stop": "alerted_price_1_stop",
        "2_plus": "alerted_price_2_plus_stops",
    }

    def tier_min(col):
        q = (
            supabase.table("sent_alerts")
            .select(col)
            .eq("watch_id", watch_id)
            .not_.is_(col, "null")
        )
        if params_changed_at:
            q = q.gte("sent_at", params_changed_at)
        r = q.order(col, desc=False).limit(1).execute().data
        return float(r[0][col]) if r else None

    return {k: tier_min(c) for k, c in cols.items()}


def set_error(watch_id, message):
    """Store a human-readable failure reason on the watch (shown in the UI)."""
    supabase.table("watches").update({"last_error": message}).eq("id", watch_id).execute()


def clear_error(watch_id):
    """Clear a watch's stored error after a successful check."""
    supabase.table("watches").update({"last_error": None}).eq("id", watch_id).execute()


def check_all_watches():
    """Check every active watch once: fetch fare, store it, alert if warranted.

    See the module docstring for the full flow. Prints a per-watch summary and a
    final error roll-up to the console (visible in the Render cron logs).
    """
    watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_active", True)
        .eq("is_paused", False)
        .eq("is_archived", False)
        .execute()
        .data
    )

    if not watches:
        print("No active watches found.")
        return

    print(f"Checking {len(watches)} active watch(es)...\n")
    errors = []

    for watch in watches:
        route = f"{watch['origin']} → {watch['destination']}"
        trip_label = "round-trip" if watch.get("trip_type") == "round_trip" else "one-way"
        print(f"Checking {route} ({watch['date_from']} – {watch['date_to']}, {watch['passengers']} pax, {trip_label})...")

        price, currency, flight_details, fetch_error, stop_tiers, date_prices = get_lowest_fare(
            origin=watch["origin"],
            destination=watch["destination"],
            date_from=watch["date_from"],
            date_to=watch["date_to"],
            passengers=watch["passengers"],
            trip_type=watch.get("trip_type", "one_way"),
            return_date_from=watch.get("return_date_from"),
            return_date_to=watch.get("return_date_to"),
        )

        if price is None:
            if fetch_error:
                msg = f"{fetch_error} — {route} ({watch['date_from']} – {watch['date_to']})"
            else:
                msg = f"No fares found for {route} ({watch['date_from']} – {watch['date_to']})"
            print(f"  ⚠️  ERROR: {msg}")
            # Only email if this is a new or changed error (avoid repeat spam)
            if watch.get("last_error") != msg:
                send_error_alert(watch, msg)
            else:
                print(f"  [error-alert] Same error as last check — not re-sending email.")
            set_error(watch["id"], msg)
            errors.append(f"{route}: {msg}")
            print()
            continue

        # Successful check — clear any previous error
        clear_error(watch["id"])

        # Baseline = the lowest price we've actually ALERTED at each tier (from
        # sent_alerts). A check that fails to send never updates this, so the
        # alert is retried next run rather than swallowed.
        prev_tier_lows = get_alerted_tier_lows(watch["id"], watch.get("params_changed_at"))

        # Save to price_history
        history_row = {
            "watch_id": watch["id"],
            "price": price,
            "currency": currency,
            # Cheapest fare at each stop level this check (any may be None)
            "price_nonstop": stop_tiers.get("price_nonstop"),
            "price_1_stop": stop_tiers.get("price_1_stop"),
            "price_2_plus_stops": stop_tiers.get("price_2_plus_stops"),
            # Winning flight details at each tier (airline/dates/stops), for the
            # expandable tier table in the UI
            "stop_tier_details": stop_tiers.get("details"),
            # Cheapest fare per departure date in the window (trend distribution)
            "date_prices": date_prices or None,
        }
        if flight_details:
            history_row["stops_outbound"] = flight_details.get("stops_outbound")
            history_row["stops_inbound"] = flight_details.get("stops_inbound")
            history_row["connection_airports"] = flight_details.get("connection_airports")
            history_row["airline"] = flight_details.get("airline")
            history_row["flight_number"] = flight_details.get("flight_number")
            history_row["departing_at"] = flight_details.get("departing_at")
            history_row["returning_at"] = flight_details.get("returning_at")
            history_row["return_flight_number"] = flight_details.get("return_flight_number")
        supabase.table("price_history").insert(history_row).execute()

        target = float(watch["target_price"])
        status = "TARGET MET ✓" if price <= target else "above target"
        print(f"  Lowest fare: {currency} {price:.2f} (target: {currency} {target:.2f}) — {status}")
        if flight_details:
            print(f"  Flight: {flight_details['flight_number']} | {flight_details['trip_type']} | Departs {flight_details['departing_at']}")

        # Determine which stop tiers just hit a NEW LOW at/below target.
        # A tier qualifies if its fare this check is <= target AND lower than
        # that tier has ever been before. The overall-cheapest fare is just
        # whichever tier is lowest, so this naturally covers it.
        tier_specs = [
            ("Nonstop", "price_nonstop", "nonstop"),
            ("1 stop", "price_1_stop", "1_stop"),
            ("2+ stops", "price_2_plus_stops", "2_plus"),
        ]
        tier_details = stop_tiers.get("details") or {}
        improved = []
        for label, price_key, detail_key in tier_specs:
            cur = stop_tiers.get(price_key)
            if cur is None or cur > target:
                continue
            prev = prev_tier_lows.get(detail_key)
            if prev is None or cur < prev:
                improved.append({
                    "label": label,
                    "price": cur,
                    "previous_low": prev,
                    "detail": tier_details.get(detail_key),
                    # Column on sent_alerts to record this tier's alerted price.
                    "alerted_col": f"alerted_{price_key}",
                })

        if improved:
            names = ", ".join(t["label"].lower() for t in improved)
            print(f"  New tier low(s) under target: {names} — sending alerts.")
            email_ok = False
            slack_ok = False

            # Email (failure here must not block Slack)
            try:
                email_ok = send_alert(watch, improved, watch["passengers"], currency)
                if email_ok:
                    print(f"  ✉️  Email sent to {watch['client_email']}")
            except Exception as e:
                print(f"  ✉️  Email failed: {e}")

            # Slack (failure here must not block anything else)
            try:
                slack_ok = send_slack_alert(watch, improved, watch["passengers"], currency)
                if slack_ok:
                    print(f"  💬 Slack notification sent")
            except Exception as e:
                print(f"  💬 Slack failed: {e}")

            # Record the alert event if either channel fired. Storing the per-tier
            # alerted prices is what advances the dedup baseline (see
            # get_alerted_tier_lows) — so a fully-failed send records nothing and
            # the alert is retried next run.
            if email_ok or slack_ok:
                alert_row = {"watch_id": watch["id"], "price": price}
                for t in improved:
                    alert_row[t["alerted_col"]] = t["price"]
                supabase.table("sent_alerts").insert(alert_row).execute()
        elif price <= target:
            print(f"  at/below target but no tier hit a new low — skipping.")

        print()

    print("Done.")
    if errors:
        print(f"\n⚠️  {len(errors)} watch(es) had errors:")
        for e in errors:
            print(f"   • {e}")


# ── Hotels (LiteAPI) ──────────────────────────────────────────────────────────

# An alert has to be worth an email. Hotel rates wobble by pennies between checks
# — and the "cheapest" rate can flip between room types — so "any new low" fired
# on a 7-cent drop (2026-08-21: "USD 401/night down from USD 401", twice in one
# evening). A drop must now beat the last alerted low by BOTH of these to count.
#
# Tune here. The percentage is what scales sensibly across price points; the
# absolute floor stops trivial amounts. NOTE the floor binds hardest on cheap
# rooms — at $80/night it demands ~19% — so lower it if budget properties ever
# get watched.
MIN_DROP_PCT = 0.05      # 5% off the previous alerted low
MIN_DROP_ABS = 15.00     # ...and at least this many currency units per night


def _is_meaningful_drop(new_low, previous_low):
    """True if `new_low` beats `previous_low` by enough to be worth an alert.

    No previous low means nothing has been alerted for this watch yet, so the
    first time it comes in under target always fires.
    """
    if previous_low is None:
        return True
    required = max(previous_low * MIN_DROP_PCT, MIN_DROP_ABS)
    return (previous_low - new_low) >= required
# Phase 1: check active hotel watches, store each check in hotel_price_history.
# Alerts + hotel error emails come in Phase 2 (this only stores + logs).

def set_hotel_error(hotel_watch_id, message):
    """Store a human-readable failure reason on a hotel watch (shown in the UI)."""
    supabase.table("hotel_watches").update({"last_error": message}).eq("id", hotel_watch_id).execute()


def clear_hotel_error(hotel_watch_id):
    """Clear a hotel watch's stored error after a successful check."""
    supabase.table("hotel_watches").update({"last_error": None}).eq("id", hotel_watch_id).execute()


def get_hotel_alerted_low(hotel_watch_id):
    """Lowest per-night-per-person price we have SUCCESSFULLY ALERTED for this hotel
    watch (from sent_alerts, written only on a successful send). Same swallow-safe
    baseline as flights: a failed send never advances it, so the alert is retried
    rather than lost. Returns float or None.
    """
    r = (
        supabase.table("sent_alerts")
        .select("price")
        .eq("hotel_watch_id", hotel_watch_id)
        .order("price", desc=False)
        .limit(1)
        .execute()
        .data
    )
    return float(r[0]["price"]) if r else None


def check_all_hotel_watches():
    """Check every active hotel watch once: fetch the cheapest net rate and store
    it in hotel_price_history. Prices tracked are net TOTAL, plus derived per-night
    and per-night-per-person (the unit the target is set in). No alerts yet.
    """
    hotels = (
        supabase.table("hotel_watches")
        .select("*")
        .eq("is_active", True)
        .execute()
        .data
    )

    if not hotels:
        print("No active hotel watches found.")
        return

    print(f"Checking {len(hotels)} active hotel watch(es)...\n")
    errors = []
    skipped_past = 0
    today = datetime.date.today().isoformat()   # dates are stored as ISO strings

    for i, hw in enumerate(hotels):
        # Gentle spacing between LiteAPI calls to stay well under their fair-use
        # limit. Done BEFORE the call rather than at the end of the loop body, so
        # the error paths — which `continue` — can't skip it. A run of failing
        # watches used to fire back-to-back with no spacing at all.
        if i:
            time.sleep(0.5)

        name = hw["accommodation_name"]
        loc = hw.get("accommodation_city") or hw.get("accommodation_country") or ""
        refundable_only = bool(hw.get("refundable_only"))

        # A stay that has already ended has nothing left to price. Left running,
        # it would call LiteAPI 12x a day forever, get "no rooms" every time, and
        # leave a permanent error on the card. Skip it and say so — there is no
        # is_archived on hotel watches yet, so this is the whole guard.
        if hw["check_out"] < today:
            print(f"Skipping {name} — stay ended {hw['check_out']}.")
            skipped_past += 1
            print()
            continue
        print(f"Checking {name}{f' ({loc})' if loc else ''} "
              f"{hw['check_in']} → {hw['check_out']}, {hw['guests']} guest(s)...")

        rate, err = get_lowest_hotel_rate(
            hotel_id=hw["accommodation_id"],
            check_in=hw["check_in"],
            check_out=hw["check_out"],
            guests=hw["guests"],
            refundable_only=refundable_only,
        )

        if rate is None:
            if err:
                msg = f"{err} — {name} ({hw['check_in']} → {hw['check_out']})"
            else:
                # Genuine "no rooms". Distinguish no-refundable from no-availability
                # so the UI isn't misleading, and bump the not-found counter.
                what = "refundable rooms" if refundable_only else "rooms"
                msg = f"No {what} for {name} ({hw['check_in']} → {hw['check_out']})"
                cnt = (hw.get("consecutive_room_not_found") or 0) + 1
                supabase.table("hotel_watches").update(
                    {"consecutive_room_not_found": cnt}
                ).eq("id", hw["id"]).execute()
            print(f"  ⚠️  {msg}")
            # Email Anna only on a new/changed error (avoid repeat spam), same as flights.
            if hw.get("last_error") != msg:
                send_hotel_error_alert(hw, msg)
            else:
                print(f"  [hotel-error-alert] Same error as last check — not re-sending.")
            set_hotel_error(hw["id"], msg)
            errors.append(f"{name}: {msg}")
            print()
            continue

        # Success — clear any prior error and reset the not-found counter.
        clear_hotel_error(hw["id"])
        if hw.get("consecutive_room_not_found"):
            supabase.table("hotel_watches").update(
                {"consecutive_room_not_found": 0}
            ).eq("id", hw["id"]).execute()

        supabase.table("hotel_price_history").insert({
            "hotel_watch_id": hw["id"],
            "total_amount": rate["total_amount"],
            "per_night_amount": rate["per_night_amount"],
            "currency": rate["currency"],
            "nights": rate["nights"],
            "rate_name": rate["rate_name"],
            "board_name": rate.get("board_name"),
            "board_type": rate.get("board_type"),
            "refundable": rate["refundable"],
            # Charged at the property, on top of total_amount. 0.0 = none.
            "excluded_fees_amount": rate.get("excluded_fees_amount"),
        }).execute()

        # Tracked unit is the nightly ROOM rate — hotels sell room-nights.
        target = float(hw["target_price_per_night"])
        nightly = rate["per_night_amount"]
        status = "TARGET MET ✓" if nightly <= target else "above target"
        refund_label = ("refundable" if rate["refundable"] else
                        "non-refundable" if rate["refundable"] is False else "refundable n/a")
        print(f"  Lowest: {rate['currency']} {nightly:.2f}/night "
              f"(target {rate['currency']} {target:.2f}) — {status}")
        fees = rate.get("excluded_fees_amount") or 0
        fee_note = (f" + {rate['currency']} {fees:.2f} fees at property" if fees else "")
        board = f" · {rate['board_name']}" if rate.get("board_name") else ""
        print(f"  {rate['rate_name']}{board} · {rate['nights']} night(s) · "
              f"total {rate['currency']} {rate['total_amount']:.2f}{fee_note} · {refund_label}")

        # ── Alert: fire on a new per-night low at/below target ────────────────
        # Baseline from sent_alerts (successful sends only) → swallow-safe, and a
        # failed send is retried next run instead of being lost.
        alerted_low = get_hotel_alerted_low(hw["id"])
        if nightly <= target and _is_meaningful_drop(nightly, alerted_low):
            print(f"  New low under target — sending hotel alert(s).")
            email_ok = slack_ok = False
            try:
                email_ok = send_hotel_alert(hw, rate, previous_low=alerted_low)
                if email_ok:
                    print(f"  ✉️  Hotel email sent")
            except Exception as e:
                print(f"  ✉️  Hotel email failed: {e}")
            try:
                slack_ok = send_hotel_slack_alert(hw, rate, previous_low=alerted_low)
                if slack_ok:
                    print(f"  💬 Hotel Slack sent")
            except Exception as e:
                print(f"  💬 Hotel Slack failed: {e}")
            if email_ok or slack_ok:
                supabase.table("sent_alerts").insert(
                    {"hotel_watch_id": hw["id"], "price": nightly}
                ).execute()
        elif nightly <= target and alerted_low is not None:
            need = max(alerted_low * MIN_DROP_PCT, MIN_DROP_ABS)
            print(f"  at/below target but only {alerted_low - nightly:+.2f} vs the last "
                  f"alerted {alerted_low:.2f} (need {need:.2f}) — skipping.")
        print()

    print("Hotel check done."
          + (f" ({skipped_past} past-dated watch(es) skipped)" if skipped_past else ""))
    if errors:
        print(f"\n⚠️  {len(errors)} hotel watch(es) had errors:")
        for e in errors:
            print(f"   • {e}")


if __name__ == "__main__":
    import sys
    import traceback

    # Isolate the two subsystems: an unhandled crash in one must not skip the
    # other. Flights run first (established order); hotels always get their turn.
    # If either crashes we still exit non-zero so Render flags the cron run.
    crashed = False
    for label, run in (("Flight", check_all_watches), ("Hotel", check_all_hotel_watches)):
        try:
            run()
        except Exception:
            crashed = True
            print(f"\n‼️  {label} check crashed (the other still ran):")
            traceback.print_exc()
        print()
    if crashed:
        sys.exit(1)
