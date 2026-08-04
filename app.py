"""
FareWatch web application (Flask).

This is the admin dashboard and the public client pages. It does NOT check
prices itself — that's the job of the `check_prices.py` cron script. This file
only reads/writes the Supabase database and renders pages.

Routes fall into three groups:
  * Admin (login-protected): the watch dashboard, add/edit/pause/resume/delete,
    price history JSON, and the service-usage page.
  * Public (no login): the per-client status page at /client/<token>, where the
    token in the URL is the only access control.
  * Auth: /login and /logout.

Data lives in Supabase (Postgres). We talk to it through the `supabase` client
using the anon key; the app gates access itself via a single shared password
(APP_PASSWORD), so every table has an "allow all" RLS policy.

Configuration comes entirely from environment variables (see .env.example).
"""
import os
import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from supabase import Client, create_client
from werkzeug.middleware.proxy_fix import ProxyFix

from hotel_prices import find_hotels
from route_stats import route_price_stats

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
# Render (and most hosts) put the app behind a reverse proxy. ProxyFix makes
# Flask trust the X-Forwarded-* headers so redirects/cookies use https + the
# real host instead of the internal one.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Single shared Supabase client for the whole app (anon key; access is gated by
# the app's own password login, not by Supabase auth).
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)

APP_PASSWORD = os.environ["APP_PASSWORD"]


@app.errorhandler(405)
def method_not_allowed(_e):
    """The action routes (/edit, /pause, /archive, /delete, /book, /resume,
    /unarchive) are POST-only. A GET to one — a refresh/back-button/bookmark onto
    an /edit/<id> URL, or a stale cached tab submitting against old page state —
    would otherwise show Werkzeug's raw 'Method Not Allowed' page. Quietly send
    the user back to the dashboard instead (which itself redirects to /login if
    they're not authenticated)."""
    return redirect(url_for("index"))


def login_required(f):
    """Decorator: redirect to /login unless the session is authenticated.

    Used on every admin route. The public client page deliberately does NOT
    use it (its access control is the unguessable token in the URL).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.template_filter("fmtdt")
def fmtdt(iso):
    """Format an ISO datetime string compactly: 'Aug 1, 9am' / 'Aug 31, 5:45pm'."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    time_str = f"{hour12}:{dt.minute:02d}{ampm}" if dt.minute else f"{hour12}{ampm}"
    return f"{dt.strftime('%b')} {dt.day}, {time_str}"


def _stops_text(lp, trip_type):
    """Human-readable stops summary, splitting connections per leg.

    connection_airports is stored as outbound connections then inbound
    connections (combined), so we split it at the outbound stop count.
    """
    if not lp or lp.get("stops_outbound") is None:
        return None
    conns = [c.strip() for c in (lp.get("connection_airports") or "").split(",") if c.strip()]
    so = lp.get("stops_outbound") or 0
    si = lp.get("stops_inbound")
    out_conns = conns[:so]

    def leg(stops, c):
        if not stops:
            return "direct"
        label = f"{stops} stop{'s' if stops > 1 else ''}"
        return f"{label} ({', '.join(c)})" if c else label

    # One-way (or missing inbound info): keep the simple original style
    if trip_type != "round_trip" or si is None:
        if so == 0:
            return "Direct"
        return f"{so} stop{'s' if so > 1 else ''}" + (f" · {', '.join(out_conns)}" if out_conns else "")

    # Round-trip: show each leg separately
    in_conns = conns[so:so + si]
    return f"out {leg(so, out_conns)} · ret {leg(si, in_conns)}"


# Fields that define which trip a watch tracks. Editing any of these starts a
# fresh history epoch (params_changed_at) so old-trip prices don't pollute the
# chart/Trends/alert baseline — see the add_params_changed_at migration.
MATERIAL_WATCH_FIELDS = (
    "origin", "destination", "date_from", "date_to",
    "return_date_from", "return_date_to", "passengers",
)


def _since_params_change(query, params_changed_at):
    """Restrict a price_history query to the current trip epoch (rows recorded at
    or after the last material edit). Watches never edited have NULL → no filter."""
    if params_changed_at:
        query = query.gte("checked_at", params_changed_at)
    return query


def _attach_watch_extras(watches):
    """Attach latest_price and alert_sent_today to each watch dict."""
    today = datetime.date.today().isoformat()
    for watch in watches:
        latest_q = (
            supabase.table("price_history")
            .select("price, currency, checked_at, stops_outbound, stops_inbound, connection_airports, airline, flight_number, departing_at, returning_at, return_flight_number, price_nonstop, price_1_stop, price_2_plus_stops, stop_tier_details")
            .eq("watch_id", watch["id"])
        )
        latest = (
            _since_params_change(latest_q, watch.get("params_changed_at"))
            .order("checked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        watch["latest_price"] = latest[0] if latest else None
        if watch["latest_price"]:
            watch["latest_price"]["stops_text"] = _stops_text(watch["latest_price"], watch.get("trip_type"))

        alert_today = (
            supabase.table("sent_alerts")
            .select("id")
            .eq("watch_id", watch["id"])
            .gte("sent_at", f"{today}T00:00:00")
            .limit(1)
            .execute()
            .data
        )
        watch["alert_sent_today"] = len(alert_today) > 0
    return watches


def _get_metrics(active_watches, all_watches):
    """Compute the four dashboard metrics."""
    # Active watch count
    active_count = len(active_watches)

    # Total alerts sent
    try:
        alerts_result = supabase.table("sent_alerts").select("id", count="exact").execute()
        alerts_count = alerts_result.count or 0
    except Exception:
        alerts_count = 0

    # Average price drop (target - current) for watches where target is met
    drops = []
    for w in active_watches:
        lp = w.get("latest_price")
        if lp and float(lp["price"]) <= float(w["target_price"]):
            drops.append(float(w["target_price"]) - float(lp["price"]))
    avg_drop = round(sum(drops) / len(drops), 0) if drops else None

    # Unique clients across all watches
    emails = set(w["client_email"] for w in all_watches if w.get("client_email"))
    clients_count = len(emails)

    return {
        "active_count": active_count,
        "alerts_count": alerts_count,
        "avg_drop": avg_drop,
        "clients_count": clients_count,
    }


def _get_recent_alerts():
    """Fetch last 8 sent alerts with watch details."""
    try:
        rows = (
            supabase.table("sent_alerts")
            .select("*, watches(origin, destination, client_name, client_email)")
            .order("sent_at", desc=True)
            .limit(8)
            .execute()
            .data
        )
        return rows
    except Exception:
        return []


@app.route("/login", methods=["GET", "POST"])
def login():
    """Show the login form (GET) and check the shared password (POST)."""
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Wrong password — try again.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    """Clear the session and return to the login page."""
    session.clear()
    return redirect(url_for("login"))


def _group_by_client(active_watches, paused_watches):
    """
    Group watches by client into an ordered list of groups.
    'My Watches' (client_name empty or 'mine') first, then clients A→Z.
    Each group: {key, name, email, active: [...], paused: [...]}.
    """
    groups = {}  # key -> group dict

    def group_key(w):
        name = (w.get("client_name") or "").strip()
        if not name or name.lower() == "mine":
            return "__mine__"
        return name

    for w in active_watches:
        key = group_key(w)
        g = groups.setdefault(key, {"key": key, "active": [], "paused": [],
                                    "name": None, "email": ""})
        g["active"].append(w)
    for w in paused_watches:
        key = group_key(w)
        g = groups.setdefault(key, {"key": key, "active": [], "paused": [],
                                    "name": None, "email": ""})
        g["paused"].append(w)

    # Fill display name + a representative email per group
    for key, g in groups.items():
        sample = (g["active"] + g["paused"])[0]
        if key == "__mine__":
            g["name"] = "My Watches"
            g["email"] = sample.get("client_email", "") if (sample.get("client_name") or "").lower() == "mine" else ""
        else:
            g["name"] = key
            g["email"] = sample.get("client_email", "")

    # Group order: My Watches first, then clients alphabetically. Watches inside
    # each group are already soonest-first (the queries order by date_from).
    return sorted(
        groups.values(),
        key=lambda g: (0 if g["key"] == "__mine__" else 1, g["name"].lower())
    )


@app.route("/")
@login_required
def index():
    """Admin dashboard: metrics row, watches grouped by client, recent alerts."""
    active_watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_active", True)
        .eq("is_paused", False)
        .eq("is_archived", False)
        .order("date_from", desc=False)
        .execute()
        .data
    )
    paused_watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_paused", True)
        .eq("is_archived", False)
        .order("date_from", desc=False)
        .execute()
        .data
    )
    past_watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_archived", True)
        .order("date_from", desc=True)
        .execute()
        .data
    )
    all_watches = active_watches + paused_watches

    _attach_watch_extras(active_watches)
    _attach_watch_extras(paused_watches)
    _attach_watch_extras(past_watches)

    groups = _group_by_client(active_watches, paused_watches)
    metrics = _get_metrics(active_watches, all_watches)
    recent_alerts = _get_recent_alerts()

    return render_template("index.html",
                           groups=groups,
                           past_watches=past_watches,
                           today=datetime.date.today().isoformat(),
                           metrics=metrics,
                           recent_alerts=recent_alerts)


def _get_or_create_token(client_email, client_name=""):
    """Return this client's existing token (shared across flight AND hotel watches
    by email), or generate a new readable one."""
    import uuid, re
    for table in ("watches", "hotel_watches"):
        existing = (
            supabase.table(table)
            .select("client_token")
            .eq("client_email", client_email)
            .not_.is_("client_token", "null")
            .limit(1)
            .execute()
            .data
        )
        if existing and existing[0].get("client_token"):
            return existing[0]["client_token"]
    slug = re.sub(r'[^a-z0-9]+', '-', client_name.lower()).strip('-') or "client"
    suffix = uuid.uuid4().hex[:4]
    return f"{slug}-{suffix}"


@app.route("/api/route-stats")
@login_required
def api_route_stats():
    """Historical price context for a route, used by the add-watch form.
    Returns {"stats": {...}} or {"stats": null} when there's no history yet."""
    stats = route_price_stats(
        supabase,
        request.args.get("origin", ""),
        request.args.get("destination", ""),
    )
    return jsonify({"stats": stats})


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_watch():
    """Create a new flight watch. Target price is entered per-person and stored
    as the total (per-person × passengers). Reuses the client's existing token
    if one exists for that email, otherwise generates one."""
    if request.method == "POST":
        trip_type = request.form.get("trip_type", "one_way")
        client_email = request.form["client_email"].strip()
        watch = {
            "origin": request.form["origin"].strip().upper(),
            "destination": request.form["destination"].strip().upper(),
            "date_from": request.form["date_from"],
            "date_to": request.form["date_to"],
            "trip_type": trip_type,
            "return_date_from": request.form.get("return_date_from") or None,
            "return_date_to": request.form.get("return_date_to") or None,
            "passengers": int(request.form["passengers"]),
            # Form input is per-person; store the total (per-person × passengers)
            "target_price": round(float(request.form["target_price"]) * int(request.form["passengers"]), 2),
            "client_name": request.form["client_name"].strip(),
            "client_email": client_email,
            "client_token": _get_or_create_token(client_email, request.form["client_name"].strip()),
            "is_active": True,
            "is_paused": False,
        }
        supabase.table("watches").insert(watch).execute()
        flash(f"Watch added for {watch['origin']} → {watch['destination']}.")
        return redirect(url_for("index"))
    return render_template("add_watch.html")


# ── Hotels (LiteAPI) ──────────────────────────────────────────────────────────

def _attach_hotel_extras(hotel_watches):
    """Attach the latest hotel_price_history row as `latest` on each hotel watch."""
    for hw in hotel_watches:
        latest = (
            supabase.table("hotel_price_history")
            .select("*")
            .eq("hotel_watch_id", hw["id"])
            .order("checked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        hw["latest"] = latest[0] if latest else None
    return hotel_watches


@app.route("/hotels")
@login_required
def hotels_page():
    """Hotel watches dashboard: active + paused, with an inline add form."""
    active = (
        supabase.table("hotel_watches").select("*")
        .eq("is_active", True).order("check_in", desc=False).execute().data
    )
    paused = (
        supabase.table("hotel_watches").select("*")
        .eq("is_active", False).order("check_in", desc=False).execute().data
    )
    _attach_hotel_extras(active)
    _attach_hotel_extras(paused)
    return render_template("hotels.html", active=active, paused=paused,
                           today=datetime.date.today().isoformat())


@app.route("/hotels/search")
@login_required
def hotels_search():
    """JSON hotel lookup for the add-watch picker: find_hotels(city, country)."""
    city = request.args.get("city", "").strip()
    country = request.args.get("country", "").strip().upper()
    if not city or not country:
        return jsonify({"error": "Enter a city and a 2-letter country code.", "hotels": []})
    hotels, err = find_hotels(city, country, limit=25)
    if err:
        return jsonify({"error": err, "hotels": []})
    return jsonify({"hotels": hotels})


@app.route("/add_hotel", methods=["POST"])
@login_required
def add_hotel():
    """Create a hotel watch. Target is per-night-per-person (stored as-is)."""
    client_email = request.form["client_email"].strip()
    client_name = request.form.get("client_name", "").strip()
    hw = {
        "accommodation_id": request.form["accommodation_id"].strip(),
        "accommodation_name": request.form["accommodation_name"].strip(),
        "accommodation_city": request.form.get("accommodation_city", "").strip() or None,
        "accommodation_country": request.form.get("accommodation_country", "").strip() or None,
        "check_in": request.form["check_in"],
        "check_out": request.form["check_out"],
        "guests": int(request.form.get("guests", 1)),
        "rooms": int(request.form.get("rooms", 1)),
        "refundable_only": request.form.get("refundable_only") == "on",
        "target_price_per_night_per_person": round(float(request.form["target_price_per_night_per_person"]), 2),
        "watch_mode": 1,
        "client_name": client_name,
        "client_email": client_email,
        "client_token": _get_or_create_token(client_email, client_name),
        "is_active": True,
    }
    supabase.table("hotel_watches").insert(hw).execute()
    flash(f"Hotel watch added for {hw['accommodation_name']}.")
    return redirect(url_for("hotels_page"))


@app.route("/hotel/pause/<hotel_id>", methods=["POST"])
@login_required
def pause_hotel(hotel_id):
    supabase.table("hotel_watches").update({"is_active": False}).eq("id", hotel_id).execute()
    flash("Hotel watch paused.")
    return redirect(url_for("hotels_page"))


@app.route("/hotel/resume/<hotel_id>", methods=["POST"])
@login_required
def resume_hotel(hotel_id):
    supabase.table("hotel_watches").update({"is_active": True}).eq("id", hotel_id).execute()
    flash("Hotel watch resumed.")
    return redirect(url_for("hotels_page"))


@app.route("/hotel/delete/<hotel_id>", methods=["POST"])
@login_required
def delete_hotel(hotel_id):
    """Permanently delete a hotel watch. hotel_price_history + sent_alerts cascade."""
    supabase.table("hotel_watches").delete().eq("id", hotel_id).execute()
    flash("Hotel watch deleted.")
    return redirect(url_for("hotels_page"))


@app.route("/edit/<watch_id>", methods=["POST"])
@login_required
def edit_watch(watch_id):
    """Update an existing watch from the inline edit form (per-person target →
    stored total, same as add_watch)."""
    passengers = int(request.form["passengers"])
    updates = {
        "origin": request.form["origin"].strip().upper(),
        "destination": request.form["destination"].strip().upper(),
        # Form input is per-person; store the total (per-person × passengers)
        "target_price": round(float(request.form["target_price"]) * passengers, 2),
        "date_from": request.form["date_from"],
        "date_to": request.form["date_to"],
        "passengers": passengers,
        "client_name": request.form["client_name"].strip(),
        "client_email": request.form["client_email"].strip(),
        "return_date_from": request.form.get("return_date_from") or None,
        "return_date_to": request.form.get("return_date_to") or None,
    }
    # If the trip itself changed (route/dates/passengers), start a new history
    # epoch so the old trip's prices don't blend into the new one's chart/alerts.
    current = (
        supabase.table("watches")
        .select(",".join(MATERIAL_WATCH_FIELDS))
        .eq("id", watch_id).limit(1).execute().data
    )
    if current and any(str(current[0].get(f)) != str(updates.get(f)) for f in MATERIAL_WATCH_FIELDS):
        updates["params_changed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    supabase.table("watches").update(updates).eq("id", watch_id).execute()
    flash("Watch updated.")
    return redirect(url_for("index"))


@app.route("/book/<watch_id>", methods=["POST"])
@login_required
def book_watch(watch_id):
    """Save a Duffel booking reference against a watch (manual entry after you
    book on Duffel). Does not place the booking itself."""
    import datetime as dt
    booking_reference = request.form.get("booking_reference", "").strip()
    if booking_reference:
        supabase.table("watches").update({
            "booking_reference": booking_reference,
            "booked_at": dt.datetime.utcnow().isoformat(),
        }).eq("id", watch_id).execute()
        flash(f"Booking reference {booking_reference} saved.")
    return redirect(url_for("index"))


@app.route("/pause/<watch_id>", methods=["POST"])
@login_required
def pause_watch(watch_id):
    """Pause a watch (kept in the DB, but skipped by the price checker)."""
    supabase.table("watches").update({"is_active": False, "is_paused": True}).eq("id", watch_id).execute()
    flash("Watch paused.")
    return redirect(url_for("index"))


@app.route("/resume/<watch_id>", methods=["POST"])
@login_required
def resume_watch(watch_id):
    """Reactivate a paused watch."""
    supabase.table("watches").update({"is_active": True, "is_paused": False}).eq("id", watch_id).execute()
    flash("Watch resumed.")
    return redirect(url_for("index"))


@app.route("/archive/<watch_id>", methods=["POST"])
@login_required
def archive_watch(watch_id):
    """Close a watch (move to Past watches). Keeps its history; stops checking it."""
    supabase.table("watches").update(
        {"is_archived": True, "is_active": False, "is_paused": False}
    ).eq("id", watch_id).execute()
    flash("Watch closed and moved to past watches.")
    return redirect(url_for("index"))


@app.route("/unarchive/<watch_id>", methods=["POST"])
@login_required
def unarchive_watch(watch_id):
    """Reopen a closed watch as active."""
    supabase.table("watches").update(
        {"is_archived": False, "is_active": True, "is_paused": False}
    ).eq("id", watch_id).execute()
    flash("Watch reopened.")
    return redirect(url_for("index"))


@app.route("/delete/<watch_id>", methods=["POST"])
@login_required
def delete_watch(watch_id):
    """Permanently delete a watch. price_history and sent_alerts rows cascade."""
    supabase.table("watches").delete().eq("id", watch_id).execute()
    flash("Watch deleted.")
    return redirect(url_for("index"))


@app.route("/deactivate/<watch_id>", methods=["POST"])
@login_required
def deactivate_watch(watch_id):
    """Legacy alias for pausing a watch (kept so old links/forms still work)."""
    supabase.table("watches").update({"is_active": False, "is_paused": True}).eq("id", watch_id).execute()
    flash("Watch paused.")
    return redirect(url_for("index"))


@app.route("/history/<watch_id>")
def watch_history(watch_id):
    """Return this watch's price history as JSON (oldest→newest) for the inline
    Chart.js price chart — limited to the current trip epoch (see
    params_changed_at) so an edited watch doesn't blend two trips on one line."""
    w = (
        supabase.table("watches")
        .select("params_changed_at")
        .eq("id", watch_id).limit(1).execute().data
    )
    pca = w[0]["params_changed_at"] if w else None
    q = (
        supabase.table("price_history")
        .select("price, currency, checked_at, stops_outbound, stops_inbound")
        .eq("watch_id", watch_id)
    )
    rows = (
        _since_params_change(q, pca)
        .order("checked_at", desc=False)
        .execute()
        .data
    )
    return jsonify(rows)


@app.route("/hotel_history/<hotel_id>")
@login_required
def hotel_history(hotel_id):
    """Admin-only JSON of a hotel watch's price history (oldest→newest) for the
    chart. Kept off the client page — hotel history is visible to Anna only."""
    rows = (
        supabase.table("hotel_price_history")
        .select("per_night_per_person_amount, per_night_amount, total_amount, "
                "currency, checked_at, nights, refundable, rate_name")
        .eq("hotel_watch_id", hotel_id)
        .order("checked_at", desc=False)
        .execute()
        .data
    )
    return jsonify(rows)


@app.route("/client/<token>")
def client_page(token):
    """Public, read-only status page for one client. No login — the token in
    the URL is the access control. Shows that client's flight AND hotel watches.
    Returns a 404 page if the token matches nothing."""
    watches = (
        supabase.table("watches")
        .select("*")
        .eq("client_token", token)
        .order("date_from", desc=False)
        .execute()
        .data
    )
    hotel_watches = (
        supabase.table("hotel_watches")
        .select("*")
        .eq("client_token", token)
        .order("check_in", desc=False)
        .execute()
        .data
    )
    if not watches and not hotel_watches:
        return render_template("client_not_found.html"), 404

    _attach_hotel_extras(hotel_watches)

    # Attach latest price to each watch
    for watch in watches:
        latest_q = (
            supabase.table("price_history")
            .select("price, currency, checked_at, stops_outbound, stops_inbound, connection_airports, airline, flight_number, departing_at, returning_at, return_flight_number, price_nonstop, price_1_stop, price_2_plus_stops, stop_tier_details")
            .eq("watch_id", watch["id"])
        )
        latest = (
            _since_params_change(latest_q, watch.get("params_changed_at"))
            .order("checked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        watch["latest_price"] = latest[0] if latest else None
        if watch["latest_price"]:
            watch["latest_price"]["stops_text"] = _stops_text(watch["latest_price"], watch.get("trip_type"))

    client_name = (watches or hotel_watches)[0].get("client_name")
    updated_at = datetime.datetime.now().strftime("%-d %B %Y at %-I:%M %p")
    return render_template("client.html",
                           watches=watches,
                           hotel_watches=hotel_watches,
                           client_name=client_name,
                           token=token,
                           updated_at=updated_at)


@app.route("/usage")
@login_required
def usage():
    """Service-usage dashboard: live consumption/limits for SendGrid, Duffel,
    Supabase, and Render (see usage.py)."""
    from usage import get_all_usage
    data = get_all_usage()
    return render_template("usage.html", usage=data)


@app.route("/trends")
@login_required
def trends():
    """Per-watch price trends. Pick a watch (?watch=<id>) and see its price
    history plus how the cheapest fare varies by time of day and day of week.
    Aggregation is done client-side; prices are shown per-person."""
    watches = (
        supabase.table("watches")
        .select("id, origin, destination, client_name, date_from, date_to, passengers, params_changed_at")
        .eq("is_archived", False)
        .order("date_from", desc=False)
        .execute()
        .data
    )
    selected_id = request.args.get("watch") or (watches[0]["id"] if watches else None)
    selected_watch = next((w for w in watches if w["id"] == selected_id), None)

    history = []
    if selected_id:
        history_q = (
            supabase.table("price_history")
            .select("checked_at, price, currency, price_nonstop, price_1_stop, price_2_plus_stops, date_prices")
            .eq("watch_id", selected_id)
        )
        history = (
            _since_params_change(history_q, selected_watch and selected_watch.get("params_changed_at"))
            .order("checked_at", desc=False)
            .execute()
            .data
        )

    return render_template("trends.html",
                           watches=watches,
                           selected_id=selected_id,
                           selected_watch=selected_watch,
                           history=history)


if __name__ == "__main__":
    # Local dev server only. In production, gunicorn runs `app:app` (see Procfile).
    # Honor $PORT so the local preview can assign a free port (5000 is often taken
    # by macOS AirPlay/ControlCenter).
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
