import os
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
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
    watches = (
        supabase.table("watches")
        .select("*")
        .eq("is_active", True)
        .order("created_at", desc=True)
        .execute()
        .data
    )

    import datetime
    today = datetime.date.today().isoformat()

    for watch in watches:
        # Most recent price check
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

        # Was an alert sent today?
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

    return render_template("index.html", watches=watches)


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_watch():
    if request.method == "POST":
        watch = {
            "origin": request.form["origin"].strip().upper(),
            "destination": request.form["destination"].strip().upper(),
            "date_from": request.form["date_from"],
            "date_to": request.form["date_to"],
            "passengers": int(request.form["passengers"]),
            "target_price": float(request.form["target_price"]),
            "client_name": request.form["client_name"].strip(),
            "client_email": request.form["client_email"].strip(),
            "is_active": True,
        }
        supabase.table("watches").insert(watch).execute()
        flash(f"Watch added for {watch['origin']} → {watch['destination']}.")
        return redirect(url_for("index"))
    return render_template("add_watch.html")


@app.route("/deactivate/<watch_id>", methods=["POST"])
@login_required
def deactivate_watch(watch_id):
    supabase.table("watches").update({"is_active": False}).eq("id", watch_id).execute()
    flash("Watch deactivated.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=False)
