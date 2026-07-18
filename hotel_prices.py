"""
LiteAPI (Nuitée) hotels integration — all hotel-rate lookups go through here.

This is the hotels counterpart to `duffel.py`. FareWatch's flight side uses
Duffel; Duffel Stays was never enabled (sales-gated — see docs/project-context
§9), so hotels run on LiteAPI instead, which is self-serve and — crucially for a
polling/monitoring tool — **does not charge per search** (they monetise the
booking, not the lookup). Searches are free within fair-use rate limits.

Two public functions:
  * get_lowest_hotel_rate(...) — cheapest current rate for ONE accommodation over
    a date range, shaped to drop straight into `hotel_price_history`.
  * find_hotels(...)           — look up LiteAPI hotel IDs by city/country, for
    populating a watch's accommodation_id/name when it's created.

Contract (from LiteAPI v3 docs):
  * Base:  https://api.liteapi.travel/v3.0
  * Auth:  X-API-Key header (a sandbox key and a prod key both use this header;
           the sandbox key just returns test content).
  * Rates: POST /hotels/rates  — body: hotelIds[], checkin, checkout, currency,
           guestNationality, occupancies[] (one entry per room).

⚠️ SCAFFOLD NOTE: the exact response nesting for /hotels/rates varies a little by
sub-version, so `_extract_rates` is deliberately tolerant, and running this file
directly (`python hotel_prices.py`) dumps a live sample so we can pin the parser
to what the sandbox actually returns before wiring it into the cron.
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
# LiteAPI passes the key in X-API-Key (their code samples' standard). A sandbox
# key and a live key are distinguished by the key itself, not by the URL/header.
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


def _build_occupancies(guests, rooms):
    """Spread `guests` adults across `rooms` rooms, at least one adult per room.

    LiteAPI wants `occupancies` as one object per room: {"adults": N, "children": []}.
    """
    rooms = max(int(rooms or 1), 1)
    guests = max(int(guests or 1), rooms)  # never fewer guests than rooms
    base, extra = divmod(guests, rooms)
    return [{"adults": base + (1 if i < extra else 0), "children": []} for i in range(rooms)]


def _retry_wait_seconds(response):
    """Seconds to wait before retrying a 429, from the Retry-After header.

    Retry-After is normally an integer number of seconds; fall back to a small
    default. Clamped to [0.5s, 65s] so a bad header can't stall or hammer us.
    (Same lesson as duffel.py: never let a header parse collapse the backoff.)
    """
    ra = response.headers.get("Retry-After")
    wait = _num(ra)
    if wait is None:
        wait = 2.0
    return min(max(wait, 0.5), 65.0)


# ── Rate extraction (tolerant — finalise against a real sandbox response) ─────────

def _rate_amount(rate):
    """(total_amount, currency) for a single rate object, across known shapes."""
    rr = rate.get("retailRate") or {}
    tot = rr.get("total")
    if isinstance(tot, list) and tot:
        return _num(tot[0].get("amount")), tot[0].get("currency")
    if isinstance(tot, dict):
        return _num(tot.get("amount")), tot.get("currency")
    # Fallbacks seen in older/other payloads
    for k in ("price", "amount", "totalRate", "net"):
        if k in rate:
            return _num(rate[k]), rate.get("currency")
    return None, None


def _rate_refundable(rate):
    """True/False if the rate's cancellation policy makes it clear; else None."""
    cp = rate.get("cancellationPolicies") or {}
    tag = (cp.get("refundableTag") or cp.get("refundable_tag") or "").upper()
    if tag in ("RFN", "REFUNDABLE"):
        return True
    if tag in ("NRFN", "NON_REFUNDABLE", "NRF"):
        return False
    return None


def _extract_rates(payload):
    """Flatten a /hotels/rates response into a list of candidate rate dicts.

    Tolerant of nesting differences: data[] → roomTypes[]/rooms[] → rates[].
    """
    rates = []
    for hotel in (payload.get("data") or []):
        hotel_id = hotel.get("hotelId") or hotel.get("id")
        room_types = hotel.get("roomTypes") or hotel.get("rooms") or []
        for rt in room_types:
            offer_id = rt.get("offerId") or rt.get("rateId")
            for r in (rt.get("rates") or [rt]):  # some shapes put rate fields on rt itself
                amt, cur = _rate_amount(r)
                if amt is None:
                    continue
                rates.append({
                    "hotel_id": hotel_id,
                    "total_amount": amt,
                    "currency": cur or "USD",
                    "rate_name": r.get("name") or r.get("boardName") or rt.get("name"),
                    "rate_code": r.get("rateId") or r.get("rateCode") or offer_id,
                    "refundable": _rate_refundable(r),
                    "offer_id": r.get("offerId") or offer_id,
                })
    return rates


# ── Public API ───────────────────────────────────────────────────────────────────

def get_lowest_hotel_rate(hotel_id, check_in, check_out, guests=1, rooms=1,
                          refundable_only=False, currency="USD",
                          guest_nationality="US"):
    """Cheapest current rate for one accommodation over [check_in, check_out).

    Returns (rate, error):
      * rate — dict ready for `hotel_price_history`, or None if no rate/failed:
          {total_amount, per_night_amount, per_night_per_person_amount, currency,
           nights, rate_name, rate_code, refundable, offer_id}
      * error — None on success or a genuine "no rooms"; a message string on
        API/validation failure. (Mirrors duffel.get_lowest_fare's error contract.)

    Prices are TOTALS for the whole stay/party; per-night and per-night-per-person
    are derived to match the target the watch is set in.
    """
    try:
        ci = datetime.date.fromisoformat(check_in)
        co = datetime.date.fromisoformat(check_out)
    except (TypeError, ValueError):
        return None, f"Invalid dates: {check_in} / {check_out}"
    nights = (co - ci).days
    if nights <= 0:
        return None, f"check_out must be after check_in ({check_in} → {check_out})"

    if not LITEAPI_KEY:
        return None, "LITEAPI_KEY is not set"

    body = {
        "hotelIds": [hotel_id],
        "checkin": check_in,
        "checkout": check_out,
        "currency": currency,
        "guestNationality": guest_nationality,
        "occupancies": _build_occupancies(guests, rooms),
    }

    max_retries = 5
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(f"{LITEAPI_BASE}/hotels/rates", headers=_headers(),
                                 json=body, timeout=30)
        except requests.exceptions.Timeout:
            return None, "LiteAPI request timed out"
        except requests.exceptions.RequestException as e:
            return None, f"Network error reaching LiteAPI: {e}"

        if resp.status_code == 429:
            if attempt < max_retries:
                wait = _retry_wait_seconds(resp)
                print(f"  [liteapi] Rate limited, waiting {wait:.1f}s "
                      f"(retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            return None, "LiteAPI rate limit exceeded after retries"

        if resp.status_code != 200:
            try:
                err = resp.json().get("error") or resp.json()
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                msg = f"HTTP {resp.status_code}"
            return None, f"LiteAPI error: {msg}"

        try:
            payload = resp.json()
        except ValueError:
            return None, "LiteAPI returned non-JSON"

        rates = _extract_rates(payload)
        if refundable_only:
            # Only drop rates we can positively confirm are non-refundable.
            rates = [r for r in rates if r["refundable"] is not False]
        if not rates:
            return None, None  # genuine "no rooms for these dates"

        best = min(rates, key=lambda r: r["total_amount"])
        total = best["total_amount"]
        rate = {
            "total_amount": round(total, 2),
            "per_night_amount": round(total / nights, 2),
            "per_night_per_person_amount": round(total / nights / max(guests, 1), 2),
            "currency": best["currency"],
            "nights": nights,
            "rate_name": best["rate_name"],
            "rate_code": best["rate_code"],
            "refundable": best["refundable"],
            "offer_id": best["offer_id"],
        }
        return rate, None

    return None, "LiteAPI rate limit exceeded after retries"


def find_hotels(city_name, country_code, limit=25):
    """Look up LiteAPI hotels by city + ISO-2 country, for picking a watch's
    accommodation_id/name. Returns (hotels, error) where each hotel is
    {id, name, city, country}. Content endpoint; not charged per call.
    """
    if not LITEAPI_KEY:
        return None, "LITEAPI_KEY is not set"
    params = {"countryCode": country_code, "cityName": city_name, "limit": limit}
    try:
        resp = requests.get(f"{LITEAPI_BASE}/data/hotels", headers=_headers(),
                            params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        return None, f"Network error reaching LiteAPI: {e}"
    if resp.status_code != 200:
        return None, f"LiteAPI error: HTTP {resp.status_code}"
    data = (resp.json() or {}).get("data") or []
    hotels = [{
        "id": h.get("id") or h.get("hotelId"),
        "name": h.get("name"),
        "city": h.get("city"),
        "country": h.get("country") or h.get("countryCode"),
    } for h in data]
    return hotels, None


if __name__ == "__main__":
    # Smoke test against the sandbox. Set LITEAPI_KEY in .env first, then run
    #   python hotel_prices.py
    # It dumps a raw rates sample so we can confirm the response shape and finalise
    # _extract_rates before wiring hotels into check_prices.py.
    if not LITEAPI_KEY:
        print("Set LITEAPI_KEY in .env to run the smoke test.")
        raise SystemExit(0)

    print("→ find_hotels('Oslo', 'NO')")
    hotels, err = find_hotels("Oslo", "NO", limit=3)
    print("  error:", err)
    print("  hotels:", json.dumps(hotels, indent=2)[:800])

    if hotels:
        hid = hotels[0]["id"]
        ci = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        co = (datetime.date.today() + datetime.timedelta(days=33)).isoformat()
        print(f"\n→ get_lowest_hotel_rate({hid}, {ci} → {co}, 2 guests)")
        rate, err = get_lowest_hotel_rate(hid, ci, co, guests=2)
        print("  error:", err)
        print("  rate:", json.dumps(rate, indent=2))

        print("\n→ raw /hotels/rates payload (first 1500 chars, to finalise the parser):")
        body = {
            "hotelIds": [hid], "checkin": ci, "checkout": co, "currency": "USD",
            "guestNationality": "US", "occupancies": [{"adults": 2, "children": []}],
        }
        r = requests.post(f"{LITEAPI_BASE}/hotels/rates", headers=_headers(), json=body, timeout=30)
        print("  status:", r.status_code)
        print(json.dumps(r.json(), indent=2)[:1500])
