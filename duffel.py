"""
Duffel flights integration — all flight-fare lookups go through here.

The one function the rest of the app uses is `get_lowest_fare(...)`. Given a
route, a departure-date window (and optionally a return window for round-trips),
and a passenger count, it searches Duffel and returns the cheapest fare it can
find plus the winning flight's details (airline, flight number, times, stops).

Implementation notes:
  * Duffel searches one specific departure date at a time, so we loop over every
    date in the window (and every outbound×return combination for round-trips)
    and keep the cheapest result.
  * Duffel rate-limits bursts of requests. We space calls out and retry on
    HTTP 429, honouring Duffel's `ratelimit-reset` header.
  * `total_amount` from Duffel is the price for ALL passengers; the caller treats
    every price as a total.

This module is flights-only. Hotels live in `duffel_stays.py` (separate file).
"""
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
    """Standard auth + version headers for every Duffel API request."""
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


def _total_stops(offer):
    """Total stops across the whole itinerary (sum of stops on every slice).

    For a one-way that's just the outbound stops; for a round-trip it's outbound
    + inbound. Used to bucket offers into stop tiers (0 / 1 / 2+).
    """
    return sum(max(len(s.get("segments", [])) - 1, 0) for s in offer.get("slices", []))


def get_lowest_fare(origin, destination, date_from, date_to, passengers,
                    trip_type="one_way", return_date_from=None, return_date_to=None):
    """
    Search for flights across a date range.
    For round-trip, also loops over return dates to find the best combination.

    Returns (price, currency, flight_details, error, stop_tiers):
      * price/currency/flight_details — the overall cheapest fare (unchanged).
      * error — None on success/genuinely-no-flights, or a message on API failure.
      * stop_tiers — dict with the cheapest TOTAL fare at each stop level found
        anywhere in the window: {"price_nonstop", "price_1_stop",
        "price_2_plus_stops"}, each a float or None. This is what lets us treat
        "same price, fewer stops" as a better deal later.
    """
    empty_tiers = {"price_nonstop": None, "price_1_stop": None, "price_2_plus_stops": None}
    try:
        start = datetime.date.fromisoformat(date_from)
        end = datetime.date.fromisoformat(date_to)
    except ValueError:
        return None, None, None, f"Invalid dates: {date_from} / {date_to}", empty_tiers

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
            return None, None, None, f"Invalid return dates: {return_date_from} / {return_date_to}", empty_tiers

    lowest_price = None
    lowest_currency = None
    lowest_details = None
    last_error = None
    agg_tiers = {}  # stop bucket (0/1/2) -> cheapest price seen across the window

    current = start
    while current <= end:
        targets = return_dates if (trip_type == "round_trip" and return_dates) else [None]
        for return_date in targets:
            price, currency, details, err, date_tiers = _search_single_date(
                origin, destination, str(current), passengers, return_date=return_date
            )
            if err:
                last_error = err
            if price is not None and (lowest_price is None or price < lowest_price):
                lowest_price = price
                lowest_currency = currency
                lowest_details = details
            # Merge this date's per-tier cheapest into the running aggregate
            for bucket, amt in (date_tiers or {}).items():
                if bucket not in agg_tiers or amt < agg_tiers[bucket]:
                    agg_tiers[bucket] = amt
            # Gentle spacing between calls to stay under Duffel's rate limit
            time.sleep(0.3)
        current += datetime.timedelta(days=1)

    stop_tiers = {
        "price_nonstop": agg_tiers.get(0),
        "price_1_stop": agg_tiers.get(1),
        "price_2_plus_stops": agg_tiers.get(2),
    }

    # If we found nothing AND hit an API error, surface the error.
    # If we found nothing with no errors, that's a genuine "no flights".
    if lowest_price is None:
        return None, None, None, last_error, stop_tiers

    return lowest_price, lowest_currency, lowest_details, None, stop_tiers


def _search_single_date(origin, destination, departure_date, passengers, return_date=None):
    """
    Search one specific date, retrying on rate limit.
    Returns (price, currency, flight_details, error, tiers):
      * the overall cheapest offer's price/currency/details (or Nones),
      * error — None on success or genuinely-no-offers; a string on API failure,
      * tiers — {stop_bucket: cheapest_price} for this date, bucket 0/1/2 (2 = 2+),
        or None if there were no offers / an error.
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
            return None, None, None, "Duffel request timed out", None
        except requests.exceptions.RequestException as e:
            return None, None, None, f"Network error reaching Duffel: {e}", None

        # Rate limited — honor the reset header and retry
        if response.status_code == 429:
            reset = response.headers.get("ratelimit-reset")
            try:
                # ratelimit-reset is a Unix timestamp, not a duration
                wait = max(float(reset) - time.time(), 0.5) if reset else 1.0
            except (TypeError, ValueError):
                wait = 1.0
            wait = min(wait, 30.0)  # clamp to a sane ceiling
            if attempt < max_retries:
                print(f"  [duffel] Rate limited on {departure_date}, waiting {wait:.1f}s "
                      f"(retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            return None, None, None, "Duffel rate limit exceeded after retries", None

        if response.status_code != 201:
            try:
                errors = response.json().get("errors", [{}])
                msg = errors[0].get("message", f"HTTP {response.status_code}") if errors else f"HTTP {response.status_code}"
            except Exception:
                msg = f"HTTP {response.status_code}"
            return None, None, None, f"Duffel API error: {msg}", None

        offers = response.json().get("data", {}).get("offers", [])
        if not offers:
            return None, None, None, None, None  # genuinely no flights for this date

        # Cheapest price per stop tier (0 = nonstop, 1 = 1 stop, 2 = 2 or more)
        tiers = {}
        for o in offers:
            try:
                amt = float(o["total_amount"])
            except (KeyError, ValueError, TypeError):
                continue
            bucket = min(_total_stops(o), 2)
            if bucket not in tiers or amt < tiers[bucket]:
                tiers[bucket] = amt

        best = min(offers, key=lambda o: float(o["total_amount"]))
        details = _extract_flight_details(best)
        return float(best["total_amount"]), best["total_currency"], details, None, tiers

    return None, None, None, "Duffel rate limit exceeded after retries"
