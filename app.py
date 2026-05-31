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


def _attach_watch_extras(watches):
    """Attach latest_price and alert_sent_today to each watch dict."""
    today = datetime.date.today().isoformat()
    for watch in watches:
        latest = (
            supabase.table("price_history")
            .select("price, currency, checked_at")
            .eq("watch_id", watch["id"])
            .order("checked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        watch["latest_price"] = latest[0] if latest else None

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

    _attach_watch_extras(active_watches)
    _attach_watch_extras(paused_watches)

    return render_template("index.html",
                           watches=active_watches,
                           paused_watches=paused_watches)


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_watch():
    if request.method == "POST":
        trip_type = request.form.get("trip_type", "one_way")
        watch = {
            "origin": request.form["origin"].strip().upper(),
            "destination": request.form["destination"].strip().upper(),
            "date_from": request.form["date_from"],
            "date_to": request.form["date_to"],
            "trip_type": trip_type,
            "return_date_from": request.form.get("return_date_from") or None,
            "return_date_to": request.form.get("return_date_to") or None,
            "passengers": int(request.form["passengers"]),
            "target_price": float(request.form["target_price"]),
            "client_name": request.form["client_name"].strip(),
            "client_email": request.form["client_email"].strip(),
            "is_active": True,
            "is_paused": False,
        }
        supabase.table("watches").insert(watch).execute()
        flash(f"Watch added for {watch['origin']} → {watch['destination']}.")
        return redirect(url_for("index"))
    return render_template("add_watch.html")


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
    # price_history and sent_alerts cascade-delete automatically
    supabase.table("watches").delete().eq("id", watch_id).execute()
    flash("Watch deleted.")
    return redirect(url_for("index"))


@app.route("/deactivate/<watch_id>", methods=["POST"])
@login_required
def deactivate_watch(watch_id):
    # Kept for backward compatibility — now just pauses
    supabase.table("watches").update({"is_active": False, "is_paused": True}).eq("id", watch_id).execute()
    flash("Watch paused.")
    return redirect(url_for("index"))


@app.route("/history/<watch_id>")
@login_required
def watch_history(watch_id):
    """Return price history as JSON for the chart."""
    rows = (
        supabase.table("price_history")
        .select("price, currency, checked_at")
        .eq("watch_id", watch_id)
        .order("checked_at", desc=False)
        .execute()
        .data
    )
    return jsonify(rows)


if __name__ == "__main__":
    app.run(debug=False)
