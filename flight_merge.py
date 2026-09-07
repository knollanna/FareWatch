"""
Merges a Duffel result and a LiteAPI result into one combined flight result.

duffel.get_lowest_fare and flight_prices.get_lowest_fare_liteapi return the
identical 6-tuple contract (price, currency, flight_details, error, stop_tiers,
date_prices) by design (see flight_prices.py's module docstring), so this file
only has to pick a winner per tier — never reshape either side.

Always compares both sources rather than treating LiteAPI as fallback-only:
its proven value is coverage (United/Delta absent from Duffel on their own
fortress-hub routes — HANDOFF-INFLIGHT.md), but comparing on price too catches
LiteAPI simply being cheaper, not just present where Duffel is absent.
"""


def _cheaper_source(d_price, d_currency, l_price, l_currency):
    """Which source wins this comparison: 'duffel', 'liteapi', or None if
    neither has a price.

    Only compares numerically when currencies match. LiteAPI is always
    queried in USD and every route this covers is domestic US, so a mismatch
    means something unexpected happened upstream — default to Duffel rather
    than risk comparing different currencies as if they were the same number.
    """
    if d_price is None and l_price is None:
        return None
    if d_price is None:
        return "liteapi"
    if l_price is None:
        return "duffel"
    if d_currency != l_currency:
        print(f"  [flight-merge] currency mismatch ({d_currency} vs {l_currency}) "
              f"— defaulting to Duffel rather than comparing across currencies.")
        return "duffel"
    return "duffel" if d_price <= l_price else "liteapi"


def _merge_tier(d_price, d_detail, l_price, l_detail, d_currency, l_currency):
    """Pick the cheaper of one stop tier's two entries, tagging the winning
    detail dict with its source. Returns (price, detail) — either may be None."""
    winner = _cheaper_source(d_price, d_currency, l_price, l_currency)
    if winner is None:
        return None, None
    price = d_price if winner == "duffel" else l_price
    detail = d_detail if winner == "duffel" else l_detail
    if detail is not None:
        detail = {**detail, "source": winner}
    return price, detail


def merge_flight_results(duffel_result, liteapi_result):
    """
    duffel_result / liteapi_result: each the 6-tuple (price, currency,
    flight_details, error, stop_tiers, date_prices) from duffel.get_lowest_fare
    / flight_prices.get_lowest_fare_liteapi.

    Returns (price, currency, flight_details, error, stop_tiers, date_prices,
    source) — the same shape check_prices.py already expects from a single
    provider, plus the winning overall source ('duffel' or 'liteapi', or None
    if neither source found a price).
    """
    d_price, d_currency, d_details, d_error, d_tiers, d_dates = duffel_result
    l_price, l_currency, l_details, l_error, l_tiers, l_dates = liteapi_result

    overall_source = _cheaper_source(d_price, d_currency, l_price, l_currency)

    if overall_source is None:
        # Neither found a price — combine whatever errors either side gave so
        # the stored/emailed message isn't silently just one provider's story.
        errors = [e for e in (d_error, l_error) if e]
        combined_error = " | ".join(errors) if errors else None
        empty_tiers = {
            "price_nonstop": None, "price_1_stop": None, "price_2_plus_stops": None,
            "details": {"nonstop": None, "1_stop": None, "2_plus": None},
        }
        return None, None, None, combined_error, empty_tiers, {}, None

    price = d_price if overall_source == "duffel" else l_price
    currency = d_currency if overall_source == "duffel" else l_currency
    details = d_details if overall_source == "duffel" else l_details

    tier_keys = [
        ("price_nonstop", "nonstop"),
        ("price_1_stop", "1_stop"),
        ("price_2_plus_stops", "2_plus"),
    ]
    stop_tiers = {"details": {}}
    for price_key, detail_key in tier_keys:
        dp = (d_tiers or {}).get(price_key)
        lp = (l_tiers or {}).get(price_key)
        dd = ((d_tiers or {}).get("details") or {}).get(detail_key)
        ld = ((l_tiers or {}).get("details") or {}).get(detail_key)
        p, d = _merge_tier(dp, dd, lp, ld, d_currency, l_currency)
        stop_tiers[price_key] = p
        stop_tiers["details"][detail_key] = d

    date_prices = dict(d_dates or {})
    for date, p in (l_dates or {}).items():
        if date not in date_prices or p < date_prices[date]:
            date_prices[date] = p

    return price, currency, details, None, stop_tiers, date_prices, overall_source
