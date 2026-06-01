import os
import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from supabase import Client, create_client
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)

APP_PASSWORD = os.environ["APP_PASSWORD"]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


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


def _attach_watch_extras(watches):
    """Attach latest_price and alert_sent_today to each watch dict."""
    today = datetime.date.today().isoformat()
    for watch in watches:
        latest = (
            supabase.table("price_history")
            .select("price, currency, checked_at, stops_outbound, stops_inbound, connection_airports, airline, flight_number, departing_at, returning_at, return_flight_number")
            .eq("watch_id", watch["id"])
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
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Wrong password — try again.")
    return render_template("login.html")


@app.route("/logout")
def logout():
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

    # Order: My Watches first, then alphabetical by name (case-insensitive)
    ordered = sorted(
        groups.values(),
        key=lambda g: (0 if g["key"] == "__mine__" else 1, g["name"].lower())
    )
    return ordered


@app.route("/")
@login_required
def index():
    active_watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_active", True)
        .eq("is_paused", False)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    paused_watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_paused", True)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    all_watches = active_watches + paused_watches

    _attach_watch_extras(active_watches)
    _attach_watch_extras(paused_watches)

    groups = _group_by_client(active_watches, paused_watches)
    metrics = _get_metrics(active_watches, all_watches)
    recent_alerts = _get_recent_alerts()

    return render_template("index.html",
                           groups=groups,
                           metrics=metrics,
                           recent_alerts=recent_alerts)


def _get_or_create_token(client_email, client_name=""):
    """Return existing token for this client email, or generate a new readable one."""
    import uuid, re
    existing = (
        supabase.table("watches")
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


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_watch():
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


@app.route("/edit/<watch_id>", methods=["POST"])
@login_required
def edit_watch(watch_id):
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
    supabase.table("watches").update(updates).eq("id", watch_id).execute()
    flash("Watch updated.")
    return redirect(url_for("index"))


@app.route("/book/<watch_id>", methods=["POST"])
@login_required
def book_watch(watch_id):
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
    supabase.table("watches").update({"is_active": False, "is_paused": True}).eq("id", watch_id).execute()
    flash("Watch paused.")
    return redirect(url_for("index"))


@app.route("/resume/<watch_id>", methods=["POST"])
@login_required
def resume_watch(watch_id):
    supabase.table("watches").update({"is_active": True, "is_paused": False}).eq("id", watch_id).execute()
    flash("Watch resumed.")
    return redirect(url_for("index"))


@app.route("/delete/<watch_id>", methods=["POST"])
@login_required
def delete_watch(watch_id):
    supabase.table("watches").delete().eq("id", watch_id).execute()
    flash("Watch deleted.")
    return redirect(url_for("index"))


@app.route("/deactivate/<watch_id>", methods=["POST"])
@login_required
def deactivate_watch(watch_id):
    supabase.table("watches").update({"is_active": False, "is_paused": True}).eq("id", watch_id).execute()
    flash("Watch paused.")
    return redirect(url_for("index"))


@app.route("/history/<watch_id>")
@login_required
def watch_history(watch_id):
    rows = (
        supabase.table("price_history")
        .select("price, currency, checked_at")
        .eq("watch_id", watch_id)
        .order("checked_at", desc=False)
        .execute()
        .data
    )
    return jsonify(rows)


@app.route("/client/<token>")
def client_page(token):
    watches = (
        supabase.table("watches")
        .select("*")
        .eq("client_token", token)
        .eq("is_active", True)
        .eq("is_paused", False)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    if not watches:
        return render_template("client_not_found.html"), 404

    # Attach latest price to each watch
    for watch in watches:
        latest = (
            supabase.table("price_history")
            .select("price, currency, checked_at, stops_outbound, stops_inbound, connection_airports, airline, flight_number, departing_at, returning_at, return_flight_number")
            .eq("watch_id", watch["id"])
            .order("checked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        watch["latest_price"] = latest[0] if latest else None
        if watch["latest_price"]:
            watch["latest_price"]["stops_text"] = _stops_text(watch["latest_price"], watch.get("trip_type"))

    client_name = watches[0]["client_name"]
    updated_at = datetime.datetime.now().strftime("%-d %B %Y at %-I:%M %p")
    return render_template("client.html",
                           watches=watches,
                           client_name=client_name,
                           token=token,
                           updated_at=updated_at)


@app.route("/usage")
@login_required
def usage():
    from usage import get_all_usage
    data = get_all_usage()
    return render_template("usage.html", usage=data)


if __name__ == "__main__":
    app.run(debug=False)
