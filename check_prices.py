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
import datetime
from dotenv import load_dotenv
from supabase import create_client
from duffel import get_lowest_fare
from alerts import send_alert, send_error_alert, send_slack_alert

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

        price, currency, flight_details, fetch_error = get_lowest_fare(
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

        # Capture the previous lowest BEFORE inserting this check's row, so the
        # new row isn't counted as its own "previous low" (which would make
        # is_new_low always false and suppress every alert).
        previous_lowest = get_previous_lowest(watch["id"])

        # Save to price_history
        history_row = {
            "watch_id": watch["id"],
            "price": price,
            "currency": currency,
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

        # Send alerts if price is at/below target AND is a new lowest price
        if price <= target:
            is_new_low = previous_lowest is None or price < previous_lowest
            if is_new_low:
                print(f"  New lowest price found — sending alerts.")
                stops = flight_details.get("stops_label") if flight_details else None
                email_ok = False
                slack_ok = False

                # Email (failure here must not block Slack)
                try:
                    email_ok = send_alert(watch, price, flight_details, previous_lowest)
                    if email_ok:
                        print(f"  ✉️  Email sent to {watch['client_email']}")
                except Exception as e:
                    print(f"  ✉️  Email failed: {e}")

                # Slack (failure here must not block anything else)
                try:
                    slack_ok = send_slack_alert(watch, price, stops, previous_lowest)
                    if slack_ok:
                        print(f"  💬 Slack notification sent")
                except Exception as e:
                    print(f"  💬 Slack failed: {e}")

                # Record the alert event if either channel fired
                if email_ok or slack_ok:
                    supabase.table("sent_alerts").insert({
                        "watch_id": watch["id"],
                        "price": price,
                    }).execute()
            else:
                prev_txt = f"{currency} {previous_lowest:.2f}" if previous_lowest is not None else "n/a"
                print(f"  skipping — not a new low (previous low {prev_txt})")

        print()

    print("Done.")
    if errors:
        print(f"\n⚠️  {len(errors)} watch(es) had errors:")
        for e in errors:
            print(f"   • {e}")


if __name__ == "__main__":
    check_all_watches()
