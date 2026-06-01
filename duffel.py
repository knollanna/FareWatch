import os
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_API_TOKEN = os.environ.get("DUFFEL_API_TOKEN", "")
DUFFEL_API_VERSION = "v2"


def _headers():
    return {
        "Authorization": f"Bearer {DUFFEL_API_TOKEN}",
        "Duffel-Version": DUFFEL_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_slice_stops(slice_data):
    """Return (stops count, connection airports string) for a slice."""
    segments = slice_data.get("segments", [])
    stops = len(segments) - 1
    if stops > 0:
        # Connection airports are the destination of each segment except the last
        connections = [seg["destination"]["iata_code"] for seg in segments[:-1]]
        connection_str = ", ".join(connections)
    else:
        connection_str = ""
    return stops, connection_str


def _format_stops(stops, connection_airports):
    """Return a human-readable stops string e.g. 'Direct' or '1 stop · IAD'."""
    if stops == 0:
        return "Direct"
    if connection_airports:
        return f"{stops} stop{'s' if stops > 1 else ''} · {connection_airports}"
    return f"{stops} stop{'s' if stops > 1 else ''}"


def _extract_flight_details(offer):
    """Pull the fields we care about from a Duffel offer object."""
    try:
        outbound_slice = offer["slices"][0]
        segment = outbound_slice["segments"][0]
        airline = offer["owner"]["name"]
        iata = offer["owner"]["iata_code"]
        flight_number = f"{iata} {segment['operating_carrier_flight_number']}"
        departing_at = segment["departing_at"]
        arriving_at = outbound_slice["segments"][-1]["arriving_at"]
        trip_type = "One-way" if len(offer["slices"]) == 1 else "Round-trip"

        stops_outbound, connections_outbound = _extract_slice_stops(outbound_slice)

        stops_inbound = None
        connections_inbound = ""
        if len(offer["slices"]) > 1:
            stops_inbound, connections_inbound = _extract_slice_stops(offer["slices"][1])

        # Combined connection airports string for storage
        all_connections = ", ".join(filter(None, [connections_outbound, connections_inbound]))

        return {
            "airline": airline,
            "flight_number": flight_number,
            "departing_at": departing_at,
            "arriving_at": arriving_at,
            "trip_type": trip_type,
            "stops_outbound": stops_outbound,
            "stops_inbound": stops_inbound,
            "connection_airports": all_connections or None,
            "stops_label": _format_stops(stops_outbound, connections_outbound),
        }
    except (KeyError, IndexError):
        return None


def get_lowest_fare(origin, destination, date_from, date_to, passengers,
                    trip_type="one_way", return_date_from=None, return_date_to=None):
    """
    Search for flights across a date range.
    For round-trip, also loops over return dates to find the best combination.
    Returns (price, currency, flight_details) or (None, None, None) if nothing found.
    """
    try:
        start = datetime.date.fromisoformat(date_from)
        end = datetime.date.fromisoformat(date_to)
    except ValueError:
        print(f"  [duffel] Invalid dates: {date_from} / {date_to}")
        return None, None, None

    # Build list of return dates for round-trip
    return_dates = []
    if trip_type == "round_trip" and return_date_from and return_date_to:
        try:
            r_start = datetime.date.fromisoformat(return_date_from)
            r_end = datetime.date.fromisoformat(return_date_to)
            r = r_start
            while r <= r_end:
                return_dates.append(str(r))
                r += datetime.timedelta(days=1)
        except ValueError:
            print(f"  [duffel] Invalid return dates: {return_date_from} / {return_date_to}")

    lowest_price = None
    lowest_currency = None
    lowest_details = None

    current = start
    while current <= end:
        if trip_type == "round_trip" and return_dates:
            for return_date in return_dates:
                price, currency, details = _search_single_date(
                    origin, destination, str(current), passengers, return_date=return_date
                )
                if price is not None and (lowest_price is None or price < lowest_price):
                    lowest_price = price
                    lowest_currency = currency
                    lowest_details = details
        else:
            price, currency, details = _search_single_date(origin, destination, str(current), passengers)
            if price is not None and (lowest_price is None or price < lowest_price):
                lowest_price = price
                lowest_currency = currency
                lowest_details = details
        current += datetime.timedelta(days=1)

    return lowest_price, lowest_currency, lowest_details


def _search_single_date(origin, destination, departure_date, passengers, return_date=None):
    """Search one specific date. Returns (price, currency, flight_details) or (None, None, None)."""
    slices = [{"origin": origin, "destination": destination, "departure_date": departure_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})

    payload = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(passengers)],
            "cabin_class": "economy",
        }
    }

    try:
        response = requests.post(
            f"{DUFFEL_API_BASE}/air/offer_requests?return_offers=true",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    except requests.exceptions.Timeout:
        print(f"  [duffel] Timeout searching {origin}→{destination} on {departure_date}")
        return None, None, None
    except requests.exceptions.RequestException as e:
        print(f"  [duffel] Request error: {e}")
        return None, None, None

    if response.status_code != 201:
        errors = response.json().get("errors", [{}])
        if errors:
            print(f"  [duffel] API error on {departure_date}: {errors[0].get('message', response.status_code)}")
        return None, None, None

    offers = response.json().get("data", {}).get("offers", [])
    if not offers:
        return None, None, None

    best = min(offers, key=lambda o: float(o["total_amount"]))
    details = _extract_flight_details(best)
    return float(best["total_amount"]), best["total_currency"], details
