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
from email.utils import parsedate_to_datetime
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


def _retry_wait_seconds(reset_header):
    """Seconds to wait before retrying after a 429, from Duffel's `ratelimit-reset`.

    Duffel sends ratelimit-reset as an RFC 2616 HTTP date (e.g.
    'Wed, 09 Jun 2026 12:00:00 GMT') — NOT a number — and asks you to retry after
    that time. We parse it as a date and wait until then (+1s buffer). If that
    fails we fall back to a numeric reading (Unix epoch if large, else
    delta-seconds) and finally a small default, so a header-format change can
    never collapse the backoff to ~0 and make us hammer the API (the old bug).
    Clamped to [0.5s, 65s] — the search limit window is 120 requests / 60s.
    """
    if not reset_header:
        return 1.0
    wait = None
    try:  # primary: HTTP date string
        reset_dt = parsedate_to_datetime(reset_header)
        if reset_dt is not None:
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            wait = (reset_dt - now).total_seconds() + 1.0  # small buffer past reset
    except (TypeError, ValueError):
        wait = None
    if wait is None:  # fallback: numeric (Unix epoch if large, else delta-seconds)
        try:
            val = float(reset_header)
            wait = (val - time.time()) if val > 1e6 else val
        except (TypeError, ValueError):
            wait = 1.0
    return min(max(wait, 0.5), 65.0)


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


def _worst_leg_stops(offer):
    """Stops on the worst leg of the itinerary — the MAX across slices, not the sum.

    A round trip with one stop each way is a "1 stop" trip. That is how a
    traveller describes it, how the tier labels read, and how the stored
    stops_outbound / stops_inbound already display it.

    Summing instead (the original behaviour) put every such round trip in the 2+
    bucket, so the 1-stop tier only ever filled when exactly one leg happened to
    be nonstop. One-ways are unaffected: with a single slice, max == sum.
    """
    return max(
        (max(len(s.get("segments", [])) - 1, 0) for s in offer.get("slices", [])),
        default=0,
    )


def get_lowest_fare(origin, destination, date_from, date_to, passengers,
                    trip_type="one_way", return_date_from=None, return_date_to=None):
    """
    Search for flights across a date range.
    For round-trip, also loops over return dates to find the best combination.

    Returns (price, currency, flight_details, error, stop_tiers, date_prices):
      * price/currency/flight_details — the overall cheapest fare (unchanged).
      * error — None on success/genuinely-no-flights, or a message on API failure.
      * stop_tiers — the cheapest fare at each stop level found anywhere in the
        window: flat prices ("price_nonstop", "price_1_stop", "price_2_plus_stops",
        each float or None) plus "details" — a dict keyed "nonstop"/"1_stop"/
        "2_plus" with that tier's winning flight details (airline, flight numbers,
        dates, stops, connections) or None.
      * date_prices — {outbound_date: cheapest_total} for every departure date in
        the window (for round-trips, cheapest across return dates).
    """
    empty_tiers = {
        "price_nonstop": None, "price_1_stop": None, "price_2_plus_stops": None,
        "details": {"nonstop": None, "1_stop": None, "2_plus": None},
    }
    try:
        start = datetime.date.fromisoformat(date_from)
        end = datetime.date.fromisoformat(date_to)
    except ValueError:
        return None, None, None, f"Invalid dates: {date_from} / {date_to}", empty_tiers, {}

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
            return None, None, None, f"Invalid return dates: {return_date_from} / {return_date_to}", empty_tiers, {}

    lowest_price = None
    lowest_currency = None
    lowest_details = None
    last_error = None
    agg_tiers = {}    # stop bucket (0/1/2) -> cheapest price seen across the window
    date_prices = {}  # outbound date (str) -> cheapest fare departing that day

    current = start
    while current <= end:
        targets = return_dates if (trip_type == "round_trip" and return_dates) else [None]
        for return_date in targets:
            price, currency, details, err, date_tiers = _search_single_date(
                origin, destination, str(current), passengers, return_date=return_date
            )
            if err:
                last_error = err
            if price is not None:
                # Cheapest fare departing on this outbound date (across return dates)
                ds = str(current)
                if ds not in date_prices or price < date_prices[ds]:
                    date_prices[ds] = price
                if lowest_price is None or price < lowest_price:
                    lowest_price = price
                    lowest_currency = currency
                    lowest_details = details
            # Merge this date's per-tier cheapest into the running aggregate
            for bucket, entry in (date_tiers or {}).items():
                if bucket not in agg_tiers or entry["price"] < agg_tiers[bucket]["price"]:
                    agg_tiers[bucket] = entry
            # Spacing between calls to stay under Duffel's search limit of
            # 120 requests / 60s. 0.6s ≈ 100/min, leaving headroom; the older
            # 0.3s (~200/min) overran the limit and tripped 429s mid-run.
            time.sleep(0.6)
        current += datetime.timedelta(days=1)

    def _tier_detail(entry):
        """Slim flight-detail dict for a tier's winning offer (for storage/UI)."""
        if not entry:
            return None
        d = _extract_flight_details(entry["offer"]) or {}
        return {
            "airline": d.get("airline"),
            "flight_number": d.get("flight_number"),
            "departing_at": d.get("departing_at"),
            "arriving_at": d.get("arriving_at"),
            "returning_at": d.get("returning_at"),
            "return_flight_number": d.get("return_flight_number"),
            "stops_outbound": d.get("stops_outbound"),
            "stops_inbound": d.get("stops_inbound"),
            "connection_airports": d.get("connection_airports"),
        }

    stop_tiers = {
        "price_nonstop": agg_tiers[0]["price"] if 0 in agg_tiers else None,
        "price_1_stop": agg_tiers[1]["price"] if 1 in agg_tiers else None,
        "price_2_plus_stops": agg_tiers[2]["price"] if 2 in agg_tiers else None,
        "details": {
            "nonstop": _tier_detail(agg_tiers.get(0)),
            "1_stop": _tier_detail(agg_tiers.get(1)),
            "2_plus": _tier_detail(agg_tiers.get(2)),
        },
    }

    # If we found nothing AND hit an API error, surface the error.
    # If we found nothing with no errors, that's a genuine "no flights".
    if lowest_price is None:
        return None, None, None, last_error, stop_tiers, date_prices

    return lowest_price, lowest_currency, lowest_details, None, stop_tiers, date_prices


def _search_single_date(origin, destination, departure_date, passengers, return_date=None):
    """
    Search one specific date, retrying on rate limit.
    Returns (price, currency, flight_details, error, tiers):
      * the overall cheapest offer's price/currency/details (or Nones),
      * error — None on success or genuinely-no-offers; a string on API failure,
      * tiers — {stop_bucket: {"price", "offer"}} for this date's cheapest offer
        at each level, bucket 0/1/2 (2 = 2+), or None on no-offers / error.
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

        # Rate limited — wait until the reset time Duffel gives us, then retry
        if response.status_code == 429:
            wait = _retry_wait_seconds(response.headers.get("ratelimit-reset"))
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

        # Cheapest OFFER per stop tier (0 = nonstop, 1 = 1 stop, 2 = 2 or more).
        # Keep the whole offer so the caller can extract its details for the
        # winning fare at each tier.
        tiers = {}
        for o in offers:
            try:
                amt = float(o["total_amount"])
            except (KeyError, ValueError, TypeError):
                continue
            bucket = min(_worst_leg_stops(o), 2)
            if bucket not in tiers or amt < tiers[bucket]["price"]:
                tiers[bucket] = {"price": amt, "offer": o}

        best = min(offers, key=lambda o: float(o["total_amount"]))
        details = _extract_flight_details(best)
        return float(best["total_amount"]), best["total_currency"], details, None, tiers

    return None, None, None, "Duffel rate limit exceeded after retries", None
