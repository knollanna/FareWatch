import os
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 5
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
DUFFEL_API_TOKEN = os.environ.get("DUFFEL_API_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _status(value, limit):
    """Return 'green', 'amber', or 'red' based on value/limit ratio."""
    if limit is None or value is None:
        return "green"
    pct = value / limit
    if pct >= 0.90:
        return "red"
    if pct >= 0.75:
        return "amber"
    return "green"


# ── SendGrid ───────────────────────────────────────────────────────────────────

def get_sendgrid_usage():
    today = datetime.date.today()
    month_start = today.replace(day=1)

    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    def fetch_stats(start, end):
        try:
            r = requests.get(
                "https://api.sendgrid.com/v3/stats",
                headers=headers,
                params={"start_date": str(start), "end_date": str(end)},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            total = sum(
                day.get("stats", [{}])[0].get("metrics", {}).get("requests", 0)
                for day in data
                if day.get("stats")
            )
            return total
        except Exception:
            return None

    sent_today = fetch_stats(today, today)
    sent_month = fetch_stats(month_start, today)

    return {
        "sent_today": sent_today,
        "sent_month": sent_month,
        "daily_limit": 100,
        "status_today": _status(sent_today, 100),
        "dashboard_url": "https://app.sendgrid.com/statistics",
        "available": sent_today is not None,
    }


# ── Duffel ─────────────────────────────────────────────────────────────────────

def get_duffel_usage():
    headers = {
        "Authorization": f"Bearer {DUFFEL_API_TOKEN}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
    }

    balance = None
    currency = None
    try:
        r = requests.get(
            "https://api.duffel.com/payments/balance",
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            balance = data.get("amount")
            currency = data.get("currency")
    except Exception:
        pass

    # Verify the token works at all with a lightweight call
    connected = False
    try:
        r = requests.get(
            "https://api.duffel.com/air/airlines?limit=1",
            headers=headers,
            timeout=TIMEOUT,
        )
        connected = r.status_code == 200
    except Exception:
        pass

    return {
        "connected": connected,
        "balance": balance,
        "currency": currency,
        "dashboard_url": "https://app.duffel.com",
        "available": connected,
    }


# ── Supabase ───────────────────────────────────────────────────────────────────

def get_supabase_usage():
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Prefer": "count=exact",
    }
    base = SUPABASE_URL.rstrip("/") + "/rest/v1"

    def count_table(table):
        try:
            r = requests.head(
                f"{base}/{table}",
                headers=headers,
                timeout=TIMEOUT,
            )
            if r.status_code in (200, 206):
                return int(r.headers.get("content-range", "0/0").split("/")[-1])
            return None
        except Exception:
            return None

    watches_count = count_table("watches")
    history_count = count_table("price_history")
    alerts_count = count_table("sent_alerts")

    project_ref = SUPABASE_URL.split("//")[-1].split(".")[0]

    return {
        "watches_count": watches_count,
        "history_count": history_count,
        "alerts_count": alerts_count,
        "db_size_limit_mb": 500,
        "dashboard_url": f"https://supabase.com/dashboard/project/{project_ref}",
        "available": watches_count is not None,
    }


# ── Render (static) ────────────────────────────────────────────────────────────

def get_render_usage():
    return {
        "web_hours_limit": 750,
        "dashboard_url": "https://dashboard.render.com",
        "available": True,
    }


# ── All together ───────────────────────────────────────────────────────────────

def get_all_usage():
    return {
        "sendgrid": get_sendgrid_usage(),
        "duffel": get_duffel_usage(),
        "supabase": get_supabase_usage(),
        "render": get_render_usage(),
        "refreshed_at": datetime.datetime.now().strftime("%d %b %Y at %H:%M:%S"),
    }
