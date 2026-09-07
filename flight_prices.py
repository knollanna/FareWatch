"""
LiteAPI flights integration — the counterpart to duffel.py.

Added to cover United and Delta, which duffel.py doesn't return even on their
own fortress-hub routes (EWR-ORD, JFK-ATL — confirmed 2026-09-01, see
HANDOFF-INFLIGHT.md). Alaska and Frontier turned out to already be on Duffel;
JetBlue and Southwest are absent from LiteAPI too, so this file doesn't try to
solve those.

⚠️ NOT WIRED INTO check_prices.py YET. LiteAPI's flights billing question is
still open — 2-hourly scheduled polling is billable by default past a
1,500:1 search-to-booking ratio, same shape as Duffel's own unresolved
mechanism (docs/project-context.md §9 / HANDOFF-INFLIGHT.md §1). Call
get_lowest_fare_liteapi directly, or via this file's __main__ smoke test,
until that's answered. Wiring into the cron is a separate, deliberate step.

⚠️ SANDBOX TRAP: environment is determined by which key you use, not the URL
— sandbox and production hit the same https://api.liteapi.travel/v3.0. The
sandbox key silently returns a fake carrier ("Nuitee Air") and logo URLs
under sandbox.nuitee.flights; production returns production.nuitee.flights.
Local .env holds the SANDBOX key on purpose, same pattern as hotel_prices.py
— don't assume LITEAPI_KEY here is the production one. _is_sandbox_response
refuses to return a result from a sandbox payload rather than risk trusting
it silently, since that's exactly what cost real time earlier this session.

Contract (from real production responses, verified 2026-09-01):
  * Base:  https://api.liteapi.travel/v3.0
  * Auth:  X-API-Key header.
  * Rates: POST /flights/rates — body: legs[] (one entry per direction:
           origin, destination, date), adults, currency.
  * Response: data[0].journeys[], each a distinct itinerary —
      .segments[]  — actual flight legs, each with .carrier
                     (marketingCode/marketingName), .direction
                     (OUTBOUND/INBOUND), .flight.marketingNumber
      .cheapestOffer.pricing.display.{total,currency} — this itinerary's
                     cheapest fare
      .offers[]    — every fare/bundle option for the itinerary (cheapestOffer
                     is the min of these)
    Stops = segments in one direction, minus 1 — mirrors duffel.py's slice
    model, so _worst_leg_stops here matches duffel._worst_leg_stops exactly:
    the MAX across directions, not the sum, so a 1-stop-each-way round trip
    reads as "1 stop", not "2+".
"""
import os
import time
import datetime
import json
import requests
from dotenv import load_dotenv

load_dotenv()

LITEAPI_BASE = "https://api.liteapi.travel/v3.0"
LITEAPI_KEY = os.environ.get("LITEAPI_KEY", "")
AUTH_HEADER = "X-API-Key"


def _headers():
    """Standard auth + JSON headers for every LiteAPI request."""
    return {
        AUTH_HEADER: LITEAPI_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _num(v):
    """Coerce a price-ish value to float, or None if it isn't a number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _retry_wait_seconds(response):
    """Seconds to wait before retrying a 429, from the Retry-After header.

    Same lesson as duffel.py and hotel_prices.py: never let a header parse
    collapse the backoff to ~0. Clamped to [0.5s, 65s].
    """
    ra = response.headers.get("Retry-After")
    wait = _num(ra)
    if wait is None:
        wait = 2.0
    return min(max(wait, 0.5), 65.0)


def _is_sandbox_response(payload):
    """True if this response came from LiteAPI's sandbox, not production.

    The tell, confirmed against real responses today: sandbox logo URLs point
    at sandbox.nuitee.flights and/or a fake carrier ("Nuitee Air") appears.
    Environment is determined by which key was used, not the endpoint — this
    just confirms which one actually happened, since a wrong/leftover key
    returns sandbox data with no error to signal it.
    """
    blob = json.dumps(payload)
    return "sandbox.nuitee.flights" in blob or "Nuitee Air" in blob


def _journey_stops(journey, direction):
    """Stops (segment count - 1) for one direction of a journey."""
    segs = [s for s in journey.get("segments", []) if s.get("direction") == direction]
    return max(len(segs) - 1, 0)


def _worst_leg_stops(journey):
    """Stops on the worst leg — mirrors duffel._worst_leg_stops (max across
    directions present, not the sum)."""
    directions = {s.get("direction") for s in journey.get("segments", [])}
    if not directions:
        return 0
    return max(_journey_stops(journey, d) for d in directions)


def _journey_price(journey):
    """(total_amount, currency) for a journey's cheapest offer, or (None, None)."""
    offer = journey.get("cheapestOffer") or {}
    display = (offer.get("pricing") or {}).get("display") or {}
    return _num(display.get("total")), display.get("currency")


def _direction_segments(journey, direction):
    """Segments for one direction, sorted by departure time."""
    segs = [s for s in journey.get("segments", []) if s.get("direction") == direction]
    return sorted(segs, key=lambda s: s.get("departureTime") or "")


def _connection_airports(segs):
    """Connection airports string for a sorted list of same-direction segments."""
    if len(segs) <= 1:
        return ""
    return ", ".join(s["destinationCode"] for s in segs[:-1])


def _extract_flight_details(journey):
    """Pull the fields we care about from a LiteAPI journey.

    Mirrors duffel._extract_flight_details's output shape (same keys) so the
    two sources are drop-in comparable once merge logic gets built.
    """
    try:
        out_segs = _direction_segments(journey, "OUTBOUND")
        if not out_segs:
            return None
        first = out_segs[0]
        carrier = first.get("carrier") or {}
        airline = carrier.get("marketingName")
        iata = carrier.get("marketingCode")
        fn_num = (first.get("flight") or {}).get("marketingNumber")
        flight_number = f"{iata} {fn_num}" if fn_num else iata
        departing_at = first.get("departureTime")
        arriving_at = out_segs[-1].get("arrivalTime")

        in_segs = _direction_segments(journey, "INBOUND")
        trip_type = "Round-trip" if in_segs else "One-way"
        returning_at = in_segs[0].get("departureTime") if in_segs else None
        return_flight_number = None
        if in_segs:
            rcarrier = in_segs[0].get("carrier") or {}
            rfn = (in_segs[0].get("flight") or {}).get("marketingNumber")
            rcode = rcarrier.get("marketingCode")
            return_flight_number = f"{rcode} {rfn}" if rfn else rcode

        return {
            "airline": airline,
            "flight_number": flight_number,
            "departing_at": departing_at,
            "arriving_at": arriving_at,
            "returning_at": returning_at,
            "return_flight_number": return_flight_number,
            "trip_type": trip_type,
            "stops_outbound": max(len(out_segs) - 1, 0),
            "stops_inbound": max(len(in_segs) - 1, 0) if in_segs else None,
            "connection_airports": ", ".join(filter(None, [
                _connection_airports(out_segs), _connection_airports(in_segs),
            ])) or None,
        }
    except (KeyError, IndexError, TypeError):
        return None


def _search_single_date(origin, destination, departure_date, passengers, return_date=None):
    """
    Search one specific date, retrying on rate limit.
    Returns (price, currency, flight_details, error, tiers) — same contract
    as duffel._search_single_date, so callers can treat both sources the
    same way once merge logic exists.
    """
    if not LITEAPI_KEY:
        return None, None, None, "LITEAPI_KEY is not set", None

    legs = [{"origin": origin, "destination": destination, "date": departure_date}]
    if return_date:
        legs.append({"origin": destination, "destination": origin, "date": return_date})

    body = {"legs": legs, "adults": passengers, "currency": "USD"}

    max_retries = 5
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(f"{LITEAPI_BASE}/flights/rates", headers=_headers(),
                                  json=body, timeout=30)
        except requests.exceptions.Timeout:
            return None, None, None, "LiteAPI request timed out", None
        except requests.exceptions.RequestException as e:
            return None, None, None, f"Network error reaching LiteAPI: {e}", None

        if resp.status_code == 429:
            if attempt < max_retries:
                wait = _retry_wait_seconds(resp)
                print(f"  [liteapi-flights] Rate limited on {departure_date}, waiting "
                      f"{wait:.1f}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            return None, None, None, "LiteAPI rate limit exceeded after retries", None

        if resp.status_code != 200:
            try:
                err = resp.json().get("error") or resp.json()
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                msg = f"HTTP {resp.status_code}"
            return None, None, None, f"LiteAPI error: {msg}", None

        try:
            payload = resp.json()
        except ValueError:
            return None, None, None, "LiteAPI returned non-JSON", None

        if _is_sandbox_response(payload):
            return None, None, None, "LiteAPI returned SANDBOX data — check LITEAPI_KEY", None

        journeys = ((payload.get("data") or [{}])[0]).get("journeys") or []
        if not journeys:
            return None, None, None, None, None  # genuinely no flights for this date

        # Cheapest JOURNEY per stop tier (0 = nonstop, 1 = 1 stop, 2 = 2+).
        tiers = {}
        for j in journeys:
            amt, cur = _journey_price(j)
            if amt is None:
                continue
            bucket = min(_worst_leg_stops(j), 2)
            if bucket not in tiers or amt < tiers[bucket]["price"]:
                tiers[bucket] = {"price": amt, "journey": j, "currency": cur}

        if not tiers:
            return None, None, None, None, None

        best_bucket = min(tiers, key=lambda b: tiers[b]["price"])
        best = tiers[best_bucket]
        details = _extract_flight_details(best["journey"])
        return best["price"], best["currency"], details, None, tiers

    return None, None, None, "LiteAPI rate limit exceeded after retries", None


def get_lowest_fare_liteapi(origin, destination, date_from, date_to, passengers,
                             trip_type="one_way", return_date_from=None, return_date_to=None):
    """
    LiteAPI counterpart to duffel.get_lowest_fare — same signature, same
    return contract: (price, currency, flight_details, error, stop_tiers,
    date_prices). NOT wired into check_prices.py yet (see module docstring);
    call directly or via this file's __main__ smoke test.
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
            return (None, None, None,
                    f"Invalid return dates: {return_date_from} / {return_date_to}",
                    empty_tiers, {})

    lowest_price = None
    lowest_currency = None
    lowest_details = None
    last_error = None
    agg_tiers = {}
    date_prices = {}

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
                ds = str(current)
                if ds not in date_prices or price < date_prices[ds]:
                    date_prices[ds] = price
                if lowest_price is None or price < lowest_price:
                    lowest_price = price
                    lowest_currency = currency
                    lowest_details = details
            for bucket, entry in (date_tiers or {}).items():
                if bucket not in agg_tiers or entry["price"] < agg_tiers[bucket]["price"]:
                    agg_tiers[bucket] = entry
            # LiteAPI prod is 250 req/s — generous — but match duffel.py's
            # spacing rather than assume headroom means "go fast".
            time.sleep(0.6)
        current += datetime.timedelta(days=1)

    def _tier_detail(entry):
        if not entry:
            return None
        d = _extract_flight_details(entry["journey"]) or {}
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

    if lowest_price is None:
        return None, None, None, last_error, stop_tiers, date_prices
    return lowest_price, lowest_currency, lowest_details, None, stop_tiers, date_prices


if __name__ == "__main__":
    # Smoke test against PRODUCTION — set LITEAPI_KEY to the production key
    # (not the sandbox key that lives in local .env) before running:
    #   LITEAPI_KEY=<prod key> python flight_prices.py
    # A sandbox response is refused rather than trusted (_is_sandbox_response).
    if not LITEAPI_KEY:
        print("Set LITEAPI_KEY to your PRODUCTION key to run this smoke test.")
        print("(Local .env's key is the sandbox key on purpose.)")
        raise SystemExit(0)

    test_date = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    print(f"→ get_lowest_fare_liteapi(EWR, ORD, {test_date}, 1 pax) — Duffel gap, UA-heavy")
    price, currency, details, err, tiers, date_prices = get_lowest_fare_liteapi(
        "EWR", "ORD", test_date, test_date, 1)
    print("  error:", err)
    print("  price:", price, currency)
    print("  details:", json.dumps(details, indent=2))
    print("  stop_tiers:", json.dumps(
        {k: v for k, v in tiers.items() if k != "details"}, indent=2))
