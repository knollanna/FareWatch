"""Route-level price aggregation.

The single home for stats computed across ALL watches on a given route
(origin → destination), including archived ones. When a watch is closed it
keeps its price_history (see /archive), and this module is what finally puts
that retained history to use: it lets the add-watch form show what a route has
historically cost before you pick a target price.

v1 is deliberately route-wide (no seasonality/date filtering) — simple, and it
always has something to show. Future trends (best booking lead time, day-of-week
patterns, per-season lows) belong here too: add a function, reuse
_route_watch_ids() + _prices_for_watches(), and keep app.py thin.
"""

import statistics


def _route_watch_ids(supabase, origin, destination):
    """All watch ids on this route, active OR archived. Empty list if none."""
    rows = (
        supabase.table("watches")
        .select("id")
        .eq("origin", origin)
        .eq("destination", destination)
        .execute()
        .data
    )
    return [r["id"] for r in rows]


def _prices_for_watches(supabase, watch_ids):
    """Every recorded (price, checked_at) across the given watches.

    Returns rows sorted oldest→newest. Rows with a null/zero price are dropped
    so a failed check never counts as a $0 low.
    """
    if not watch_ids:
        return []
    rows = (
        supabase.table("price_history")
        .select("price, currency, checked_at")
        .in_("watch_id", watch_ids)
        .order("checked_at", desc=False)
        .execute()
        .data
    )
    return [r for r in rows if r.get("price")]


def route_price_stats(supabase, origin, destination):
    """Historical price summary for a route, across all past & present watches.

    Returns None when there's no usable history yet. Otherwise a dict:
        observations  int    number of price checks the stats are built on
        watch_count   int    how many distinct watches contributed
        lowest        float  cheapest price ever recorded
        median        float  typical price (median of all checks)
        latest        float  most recent recorded price
        currency      str    currency of the figures (from the data)
        first_seen    str    ISO date of the earliest check
        last_seen     str    ISO date of the most recent check
    """
    origin = (origin or "").strip().upper()
    destination = (destination or "").strip().upper()
    if len(origin) != 3 or len(destination) != 3:
        return None

    watch_ids = _route_watch_ids(supabase, origin, destination)
    rows = _prices_for_watches(supabase, watch_ids)
    if not rows:
        return None

    prices = [float(r["price"]) for r in rows]
    return {
        "observations": len(prices),
        "watch_count": len(watch_ids),
        "lowest": round(min(prices), 2),
        "median": round(statistics.median(prices), 2),
        "latest": round(prices[-1], 2),
        "currency": rows[-1].get("currency") or "USD",
        "first_seen": rows[0]["checked_at"][:10],
        "last_seen": rows[-1]["checked_at"][:10],
    }
