"""
Service-usage data for the /usage admin page.

Each `get_*_usage()` function calls one external service's API and returns a
small dict of metrics + a status colour (green/amber/red) for that card:

  * SendGrid — emails sent today / this month vs the 100/day free limit.
  * Duffel   — whether the API token is working, and balance if exposed.
  * Supabase — row counts per table (size vs the 500 MB free tier).
  * Render   — static free-tier reminders, plus live service status + last
               deploy if a RENDER_API_KEY is configured.

Everything is best-effort with a short timeout: any failing call returns
"unavailable" rather than raising, so a slow/down service never breaks the page.
"""
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
RENDER_API_BASE = "https://api.render.com/v1"
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_NAME = os.environ.get("RENDER_SERVICE_NAME", "farewatch")


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


# ── Render ─────────────────────────────────────────────────────────────────────

def _format_render_time(ts):
    if not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y, %H:%M")
    except Exception:
        return ts[:16] if isinstance(ts, str) else None


def _render_status_meta(deploy_status, suspended):
    """Map a Render deploy status (+ suspended flag) to (label, colour)."""
    if suspended == "suspended":
        return "suspended", "red"
    s = (deploy_status or "").lower()
    if s == "live":
        return "live", "green"
    if "in_progress" in s or s == "created":
        return "deploying", "amber"
    if "failed" in s:
        return "deploy failed", "red"
    if s == "canceled":
        return "canceled", "amber"
    if s == "deactivated":
        return "inactive", "gray"
    return (deploy_status or "unknown"), "gray"


def get_render_usage():
    # Static info that's always shown; live fields filled in if an API key is set.
    base = {
        "web_hours_limit": 750,
        "dashboard_url": "https://dashboard.render.com",
        "available": True,        # the card always renders
        "api_available": False,   # whether live status/deploy data was fetched
        "status_label": None,
        "status_color": "green",
        "last_deploy_at": None,
        "commit_message": None,
        "commit_id": None,
        "service_name": RENDER_SERVICE_NAME,
    }
    if not RENDER_API_KEY:
        return base

    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    try:
        r = requests.get(
            f"{RENDER_API_BASE}/services",
            headers=headers,
            params={"name": RENDER_SERVICE_NAME, "limit": 20},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return base

        # Response items may be {"service": {...}} or the service object directly
        services = []
        for it in r.json():
            svc = it.get("service", it) if isinstance(it, dict) else None
            if svc:
                services.append(svc)

        svc = next(
            (s for s in services
             if s.get("name") == RENDER_SERVICE_NAME and s.get("type") == "web_service"),
            services[0] if services else None,
        )
        if not svc:
            return base

        service_id = svc.get("id")
        suspended = svc.get("suspended")
        if svc.get("dashboardUrl"):
            base["dashboard_url"] = svc["dashboardUrl"]

        # Latest deploy
        deploy = None
        dr = requests.get(
            f"{RENDER_API_BASE}/services/{service_id}/deploys",
            headers=headers, params={"limit": 1}, timeout=TIMEOUT,
        )
        if dr.status_code == 200 and dr.json():
            d0 = dr.json()[0]
            deploy = d0.get("deploy", d0) if isinstance(d0, dict) else None

        deploy_status = deploy.get("status") if deploy else None
        label, color = _render_status_meta(deploy_status, suspended)
        base["api_available"] = True
        base["status_label"] = label
        base["status_color"] = color

        if deploy:
            base["last_deploy_at"] = _format_render_time(
                deploy.get("finishedAt") or deploy.get("createdAt")
            )
            commit = deploy.get("commit") or {}
            msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else None
            if msg and len(msg) > 60:
                msg = msg[:57] + "…"
            base["commit_message"] = msg
            cid = commit.get("id")
            base["commit_id"] = cid[:7] if cid else None

        return base
    except requests.exceptions.RequestException:
        return base


# ── All together ───────────────────────────────────────────────────────────────

def get_all_usage():
    return {
        "sendgrid": get_sendgrid_usage(),
        "duffel": get_duffel_usage(),
        "supabase": get_supabase_usage(),
        "render": get_render_usage(),
        "refreshed_at": datetime.datetime.now().strftime("%d %b %Y at %H:%M:%S"),
    }
