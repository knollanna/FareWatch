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


def _extract_flight_details(offer):
    """Pull the fields we care about from a Duffel offer object."""
    try:
        segment = offer["slices"][0]["segments"][0]
        airline = offer["owner"]["name"]
        iata = offer["owner"]["iata_code"]
        flight_number = f"{iata} {segment['operating_carrier_flight_number']}"
        departing_at = segment["departing_at"]   # e.g. "2026-06-12T09:45:00"
        arriving_at = segment["arriving_at"]
        trip_type = "One-way" if len(offer["slices"]) == 1 else "Round-trip"
        return {
            "airline": airline,
            "flight_number": flight_number,
            "departing_at": departing_at,
            "arriving_at": arriving_at,
            "trip_type": trip_type,
        }
    except (KeyError, IndexError):
        return None


def get_lowest_fare(origin, destination, date_from, date_to, passengers):
    """
    Search for flights across a date range.
    Returns (price, currency, flight_details) or (None, None, None) if nothing found.
    flight_details is a dict with airline, flight_number, departing_at, arriving_at, trip_type.
    """
    try:
        start = datetime.date.fromisoformat(date_from)
        end = datetime.date.fromisoformat(date_to)
    except ValueError:
        print(f"  [duffel] Invalid dates: {date_from} / {date_to}")
        return None, None, None

    lowest_price = None
    lowest_currency = None
    lowest_details = None

    current = start
    while current <= end:
        price, currency, details = _search_single_date(origin, destination, str(current), passengers)
        if price is not None:
            if lowest_price is None or price < lowest_price:
                lowest_price = price
                lowest_currency = currency
                lowest_details = details
        current += datetime.timedelta(days=1)

    return lowest_price, lowest_currency, lowest_details


def _search_single_date(origin, destination, departure_date, passengers):
    """Search one specific date. Returns (price, currency, flight_details) or (None, None, None)."""
    payload = {
        "data": {
            "slices": [
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": departure_date,
                }
            ],
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
