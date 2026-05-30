import os
import datetime
from dotenv import load_dotenv
from supabase import create_client
from duffel import get_lowest_fare
from alerts import send_alert

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)


def already_alerted_today(watch_id):
    """Return True if we already sent an alert for this watch today."""
    today = datetime.date.today().isoformat()
    result = (
        supabase.table("sent_alerts")
        .select("id")
        .eq("watch_id", watch_id)
        .gte("sent_at", f"{today}T00:00:00")
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def check_all_watches():
    watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_active", True)
        .execute()
        .data
    )

    if not watches:
        print("No active watches found.")
        return

    print(f"Checking {len(watches)} active watch(es)...\n")

    for watch in watches:
        route = f"{watch['origin']} → {watch['destination']}"
        print(f"Checking {route} ({watch['date_from']} – {watch['date_to']}, {watch['passengers']} pax)...")

        price, currency, flight_details = get_lowest_fare(
            origin=watch["origin"],
            destination=watch["destination"],
            date_from=watch["date_from"],
            date_to=watch["date_to"],
            passengers=watch["passengers"],
        )

        if price is None:
            print(f"  No fares found for {route}.\n")
            continue

        # Save to price_history
        supabase.table("price_history").insert({
            "watch_id": watch["id"],
            "price": price,
            "currency": currency,
        }).execute()

        target = float(watch["target_price"])
        status = "TARGET MET ✓" if price <= target else "above target"
        print(f"  Lowest fare: {currency} {price:.2f} (target: {currency} {target:.2f}) — {status}")
        if flight_details:
            print(f"  Flight: {flight_details['flight_number']} | {flight_details['trip_type']} | Departs {flight_details['departing_at']}")

        # Send alert if target is met and we haven't already sent one today
        if price <= target:
            if already_alerted_today(watch["id"]):
                print(f"  [alert] Already sent an alert today for this watch — skipping.")
            else:
                success = send_alert(watch, price, flight_details)
                if success:
                    supabase.table("sent_alerts").insert({
                        "watch_id": watch["id"],
                        "price": price,
                    }).execute()

        print()

    print("Done.")


if __name__ == "__main__":
    check_all_watches()
