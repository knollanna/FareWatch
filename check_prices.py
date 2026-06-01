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
    supabase.table("watches").update({"last_error": message}).eq("id", watch_id).execute()


def clear_error(watch_id):
    supabase.table("watches").update({"last_error": None}).eq("id", watch_id).execute()


def check_all_watches():
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

        price, currency, flight_details = get_lowest_fare(
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
            msg = f"No fares found for {route} ({watch['date_from']} – {watch['date_to']})"
            print(f"  ⚠️  ERROR: {msg}\n")
            set_error(watch["id"], msg)
            errors.append(f"{route}: {msg}")
            continue

        # Successful check — clear any previous error
        clear_error(watch["id"])

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
        supabase.table("price_history").insert(history_row).execute()

        target = float(watch["target_price"])
        status = "TARGET MET ✓" if price <= target else "above target"
        print(f"  Lowest fare: {currency} {price:.2f} (target: {currency} {target:.2f}) — {status}")
        if flight_details:
            print(f"  Flight: {flight_details['flight_number']} | {flight_details['trip_type']} | Departs {flight_details['departing_at']}")

        # Send alert if price is at/below target AND is a new lowest price
        if price <= target:
            previous_lowest = get_previous_lowest(watch["id"])
            is_new_low = previous_lowest is None or price < previous_lowest
            if is_new_low:
                print(f"  New lowest price found — sending alert.")
                success = send_alert(watch, price, flight_details)
                if success:
                    supabase.table("sent_alerts").insert({
                        "watch_id": watch["id"],
                        "price": price,
                    }).execute()
            else:
                print(f"  [alert] Price is at target but not a new low (previous low: {currency} {previous_lowest:.2f}) — skipping.")

        print()

    print("Done.")
    if errors:
        print(f"\n⚠️  {len(errors)} watch(es) had errors:")
        for e in errors:
            print(f"   • {e}")


if __name__ == "__main__":
    check_all_watches()
