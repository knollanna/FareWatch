import os
import time
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
        # Prefer marketing flight number; fall back to operating; then just the code
        fn = segment.get("marketing_carrier_flight_number") or segment.get("operating_carrier_flight_number")
        flight_number = f"{iata} {fn}" if fn else iata
        departing_at = segment["departing_at"]
        arriving_at = outbound_slice["segments"][-1]["arriving_at"]
        trip_type = "One-way" if len(offer["slices"]) == 1 else "Round-trip"

        stops_outbound, connections_outbound = _extract_slice_stops(outbound_slice)

        stops_inbound = None
        connections_inbound = ""
        returning_at = None
        return_flight_number = None
        if len(offer["slices"]) > 1:
            inbound_slice = offer["slices"][1]
            stops_inbound, connections_inbound = _extract_slice_stops(inbound_slice)
            rseg = inbound_slice["segments"][0]
            returning_at = rseg.get("departing_at")
            rfn = rseg.get("marketing_carrier_flight_number") or rseg.get("operating_carrier_flight_number")
            rcarrier = (rseg.get("marketing_carrier") or {}).get("iata_code") or iata
            return_flight_number = f"{rcarrier} {rfn}" if rfn else None

        # Combined connection airports string for storage
        all_connections = ", ".join(filter(None, [connections_outbound, connections_inbound]))

        return {
            "airline": airline,
            "flight_number": flight_number,
            "departing_at": departing_at,
            "arriving_at": arriving_at,
            "returning_at": returning_at,
            "return_flight_number": return_flight_number,
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
    Returns (price, currency, flight_details, error) where error is None on
    success/genuinely-no-flights, or a human-readable string on API failure.
    """
    try:
        start = datetime.date.fromisoformat(date_from)
        end = datetime.date.fromisoformat(date_to)
    except ValueError:
        return None, None, None, f"Invalid dates: {date_from} / {date_to}"

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
            return None, None, None, f"Invalid return dates: {return_date_from} / {return_date_to}"

    lowest_price = None
    lowest_currency = None
    lowest_details = None
    last_error = None

    current = start
    while current <= end:
        targets = return_dates if (trip_type == "round_trip" and return_dates) else [None]
        for return_date in targets:
            price, currency, details, err = _search_single_date(
                origin, destination, str(current), passengers, return_date=return_date
            )
            if err:
                last_error = err
            if price is not None and (lowest_price is None or price < lowest_price):
                lowest_price = price
                lowest_currency = currency
                lowest_details = details
            # Gentle spacing between calls to stay under Duffel's rate limit
            time.sleep(0.3)
        current += datetime.timedelta(days=1)

    # If we found nothing AND hit an API error, surface the error.
    # If we found nothing with no errors, that's a genuine "no flights".
    if lowest_price is None:
        return None, None, None, last_error

    return lowest_price, lowest_currency, lowest_details, None


def _search_single_date(origin, destination, departure_date, passengers, return_date=None):
    """
    Search one specific date, retrying on rate limit.
    Returns (price, currency, flight_details, error). error is None on success or
    genuinely-no-offers; a string when the API call failed.
    """
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

    max_retries = 6
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                f"{DUFFEL_API_BASE}/air/offer_requests?return_offers=true",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
        except requests.exceptions.Timeout:
            return None, None, None, "Duffel request timed out"
        except requests.exceptions.RequestException as e:
            return None, None, None, f"Network error reaching Duffel: {e}"

        # Rate limited — honor the reset header and retry
        if response.status_code == 429:
            reset = response.headers.get("ratelimit-reset", "1")
            try:
                wait = float(reset)
            except (TypeError, ValueError):
                wait = 1.0
            wait = min(max(wait, 0.5), 30.0)  # clamp to a sane range
            if attempt < max_retries:
                print(f"  [duffel] Rate limited on {departure_date}, waiting {wait:.1f}s "
                      f"(retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            return None, None, None, "Duffel rate limit exceeded after retries"

        if response.status_code != 201:
            try:
                errors = response.json().get("errors", [{}])
                msg = errors[0].get("message", f"HTTP {response.status_code}") if errors else f"HTTP {response.status_code}"
            except Exception:
                msg = f"HTTP {response.status_code}"
            return None, None, None, f"Duffel API error: {msg}"

        offers = response.json().get("data", {}).get("offers", [])
        if not offers:
            return None, None, None, None  # genuinely no flights for this date

        best = min(offers, key=lambda o: float(o["total_amount"]))
        details = _extract_flight_details(best)
        return float(best["total_amount"]), best["total_currency"], details, None

    return None, None, None, "Duffel rate limit exceeded after retries"
