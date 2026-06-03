"""
Notifications — how FareWatch tells you (and clients) about fares.

Three public functions, all called from check_prices.py:
  * send_alert(...)        — the client-facing fare EMAIL (via SendGrid).
                             Goes to the client and copies you (SENDER_EMAIL).
  * send_slack_alert(...)  — a Slack Block Kit message to your alerts channel
                             (via SLACK_WEBHOOK_URL). Skips silently if unset.
  * send_error_alert(...)  — an internal email to you only, when a watch errors.

send_alert / send_slack_alert take a list of "improved tiers" — each stop level
(nonstop / 1-stop / 2+) that just hit a NEW LOW at/below the target on this check.
One notification names all the tiers that improved.

Everything degrades gracefully: a missing key or a failed request returns False
instead of raising, so one channel breaking never blocks the others.

Prices passed in are TOTALS (all passengers); messages show the per-person figure
since that's what the target is set in.
"""
import os
import urllib.parse
import requests
from dotenv import load_dotenv

load_dotenv()

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
# Base URL of the deployed app, used to build client-dashboard links in emails.
BASE_URL = os.environ.get("BASE_URL", "https://farewatch.annaknoll.com")
# Slack incoming webhook; if empty, Slack notifications are skipped silently.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _google_flights_url(origin, destination, date_from, passengers, exact_date=None):
    departure = exact_date[:10] if exact_date else date_from
    q = f"{origin} to {destination} {departure}"
    return f"https://www.google.com/travel/flights?q={urllib.parse.quote(q)}&curr=USD"


AIRLINE_WEBSITES = {
    "AA": ("American Airlines", "https://www.aa.com"),
    "BA": ("British Airways", "https://www.britishairways.com"),
    "DL": ("Delta", "https://www.delta.com"),
    "UA": ("United Airlines", "https://www.united.com"),
    "LH": ("Lufthansa", "https://www.lufthansa.com"),
    "AF": ("Air France", "https://www.airfrance.com"),
    "KL": ("KLM", "https://www.klm.com"),
    "IB": ("Iberia", "https://www.iberia.com"),
    "VS": ("Virgin Atlantic", "https://www.virginatlantic.com"),
    "EK": ("Emirates", "https://www.emirates.com"),
    "QR": ("Qatar Airways", "https://www.qatarairways.com"),
    "TK": ("Turkish Airlines", "https://www.turkishairlines.com"),
    "B6": ("JetBlue", "https://www.jetblue.com"),
    "WN": ("Southwest", "https://www.southwest.com"),
    "AS": ("Alaska Airlines", "https://www.alaskaair.com"),
    "AC": ("Air Canada", "https://www.aircanada.com"),
    "LX": ("Swiss", "https://www.swiss.com"),
    "OS": ("Austrian", "https://www.austrian.com"),
    "SN": ("Brussels Airlines", "https://www.brusselsairlines.com"),
    "AZ": ("ITA Airways", "https://www.itaspa.com"),
    "SK": ("SAS", "https://www.flysas.com"),
    "AY": ("Finnair", "https://www.finnair.com"),
}


# ── Formatting helpers ──────────────────────────────────────────────────────────

def _fmt_short_date(d):
    """'2026-07-12' -> 'Jul 12'."""
    try:
        from datetime import date
        return date.fromisoformat(d).strftime("%b %-d")
    except Exception:
        return d


def _fmt_flight_dt(iso):
    """'2026-08-09T06:50:00' -> 'Aug 9, 6:50am'."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        h12 = dt.hour % 12 or 12
        ap = "am" if dt.hour < 12 else "pm"
        t = f"{h12}:{dt.minute:02d}{ap}" if dt.minute else f"{h12}{ap}"
        return f"{dt.strftime('%b')} {dt.day}, {t}"
    except Exception:
        return iso


def _tier_flight_line(detail):
    """One-line flight summary for a tier: airline + flight number(s) + dates."""
    if not detail:
        return ""
    airline = detail.get("airline") or ""
    fn = detail.get("flight_number") or ""
    dep = _fmt_flight_dt(detail.get("departing_at"))
    if detail.get("returning_at"):
        rfn = detail.get("return_flight_number") or ""
        ret = _fmt_flight_dt(detail.get("returning_at"))
        core = f"out {fn} {dep} · return {rfn} {ret}"
    elif fn:
        core = f"{fn} · departs {dep}"
    else:
        core = f"departs {dep}" if dep else ""
    return f"{airline} · {core}" if (airline and core) else (airline or core)


def _tier_stops_line(detail):
    """Stops summary for a tier: 'nonstop' / '1 stop · via KEF' / '2 stops · via …'."""
    if not detail:
        return ""
    tot = (detail.get("stops_outbound") or 0) + (detail.get("stops_inbound") or 0)
    base = "nonstop" if tot == 0 else f"{tot} stop" + ("s" if tot > 1 else "")
    conns = detail.get("connection_airports")
    return f"{base} · via {conns}" if (conns and tot) else base


# ── Email (SendGrid) ────────────────────────────────────────────────────────────

def _build_tier_alert_html(watch, improved, passengers, currency):
    origin = watch["origin"]
    destination = watch["destination"]
    client_name = watch["client_name"]
    target_pp = float(watch["target_price"]) / passengers
    pax_label = f"{passengers} passenger{'s' if passengers > 1 else ''}"

    blocks = ""
    for t in improved:
        pp = t["price"] / passengers
        if t["previous_low"] is not None:
            note = (f'<span style="color:#2a7a2a;font-weight:normal;font-size:13px;">'
                    f' ▼ down from {currency} {float(t["previous_low"]) / passengers:,.0f}/person</span>')
        else:
            note = '<span style="color:#2a7a2a;font-weight:normal;font-size:13px;"> (new!)</span>'
        flight = _tier_flight_line(t["detail"])
        stops = _tier_stops_line(t["detail"])
        flight_html = f'<div style="font-size:13px;color:#555;margin-top:3px;">{flight}</div>' if flight else ""
        stops_html = f'<div style="font-size:12px;color:#888;">{stops}</div>' if stops else ""
        blocks += f"""
  <div style="border-left:3px solid #1D9E75;background:#f6fbf9;padding:8px 14px;margin:12px 0;">
    <div style="font-size:16px;font-weight:bold;color:#1D9E75;">{t['label']} — {currency} {pp:,.0f}/person{note}</div>
    {flight_html}
    {stops_html}
  </div>"""

    best = min(improved, key=lambda t: t["price"])
    exact_date = (best.get("detail") or {}).get("departing_at")
    google_url = _google_flights_url(origin, destination, watch["date_from"], passengers, exact_date)

    client_token = watch.get("client_token")
    dash = ""
    if client_token:
        dash = (f'<p style="margin:20px 0 8px;"><a href="{BASE_URL}/client/{client_token}" '
                f'style="background:#1D9E75;color:white;padding:12px 24px;text-decoration:none;'
                f'border-radius:4px;font-weight:bold;">View your fare dashboard →</a></p>')

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:560px;margin:0 auto;padding:24px;">
  <p>Hi {client_name},</p>
  <p>Good news — a better fare just turned up for your <strong>{origin} → {destination}</strong> trip:</p>
  {blocks}
  <table style="border-collapse:collapse;width:100%;margin:18px 0;font-size:14px;color:#555;">
    <tr><td style="padding:4px 0;">Travel window</td><td style="padding:4px 0;text-align:right;">{watch['date_from']} – {watch['date_to']}</td></tr>
    <tr><td style="padding:4px 0;">Passengers</td><td style="padding:4px 0;text-align:right;">{pax_label}</td></tr>
    <tr><td style="padding:4px 0;">Your target</td><td style="padding:4px 0;text-align:right;">{currency} {target_pp:,.0f}/person</td></tr>
  </table>
  <p style="margin:16px 0 4px;"><a href="{google_url}" style="background:#1a73e8;color:white;padding:12px 24px;text-decoration:none;border-radius:4px;font-weight:bold;">Search on Google Flights →</a></p>
  <p>Fares at this price can disappear quickly — <strong>reply to this email</strong> and I'll get your booking sorted right away.</p>
  {dash}
  <p style="margin-top:28px;padding-top:16px;border-top:1px solid #eee;font-size:13px;color:#888;">
    Anna Knoll &nbsp;·&nbsp; Independent Travel Advisor &nbsp;·&nbsp; Fora Travel<br>
    <a href="mailto:{SENDER_EMAIL}" style="color:#1D9E75;">{SENDER_EMAIL}</a>
  </p>
</body>
</html>"""


def _build_tier_alert_text(watch, improved, passengers, currency):
    origin = watch["origin"]
    destination = watch["destination"]
    target_pp = float(watch["target_price"]) / passengers

    lines = [
        f"Hi {watch['client_name']},", "",
        f"Good news — a better fare just turned up for your {origin} → {destination} trip:", "",
    ]
    for t in improved:
        pp = t["price"] / passengers
        if t["previous_low"] is not None:
            note = f" (down from {currency} {float(t['previous_low']) / passengers:,.0f}/person)"
        else:
            note = " (new!)"
        lines.append(f"{t['label']}: {currency} {pp:,.0f}/person{note}")
        fl = _tier_flight_line(t["detail"])
        st = _tier_stops_line(t["detail"])
        if fl:
            lines.append(f"  {fl}")
        if st:
            lines.append(f"  {st}")
        lines.append("")

    best = min(improved, key=lambda t: t["price"])
    exact_date = (best.get("detail") or {}).get("departing_at")
    google_url = _google_flights_url(origin, destination, watch["date_from"], passengers, exact_date)

    lines += [
        f"Travel window: {watch['date_from']} – {watch['date_to']}",
        f"Passengers: {passengers}",
        f"Your target: {currency} {target_pp:,.0f}/person",
        "",
        f"Search Google Flights: {google_url}",
        "",
        "Fares at this price can disappear quickly — reply to this email and I'll get your booking sorted right away.",
    ]
    client_token = watch.get("client_token")
    if client_token:
        lines.append(f"View your fare dashboard: {BASE_URL}/client/{client_token}")
    lines += ["", "Anna Knoll · Independent Travel Advisor · Fora Travel", SENDER_EMAIL]
    return "\n".join(lines)


def send_alert(watch, improved, passengers, currency="USD"):
    """Email the client (cc Anna) about stop tier(s) hitting a new low under target.

    `improved` is a list of {label, price (total), previous_low (total|None),
    detail}. Returns True on success, False otherwise (never raises).
    """
    if not improved:
        return False

    origin = watch["origin"]
    destination = watch["destination"]
    client_email = watch.get("client_email", "").strip()
    client_name = watch["client_name"]

    if len(improved) == 1:
        t = improved[0]
        subject = (f"Fare alert: {origin} → {destination} — "
                   f"{t['label'].lower()} {currency} {t['price'] / passengers:,.0f}/person")
    else:
        subject = f"Fare alert: {origin} → {destination} — {len(improved)} new fare lows"

    html_body = _build_tier_alert_html(watch, improved, passengers, currency)
    text_body = _build_tier_alert_text(watch, improved, passengers, currency)

    to_list = []
    if client_email and "@" in client_email and client_email != SENDER_EMAIL:
        to_list.append({"email": client_email, "name": client_name})
    cc_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]
    if not to_list:
        to_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]
        cc_list = []

    payload = {
        "personalizations": [{
            "to": to_list,
            **({"cc": cc_list} if cc_list else {}),
            "subject": subject,
        }],
        "from": {"email": SENDER_EMAIL, "name": "Anna | FareWatch"},
        "reply_to": {"email": SENDER_EMAIL, "name": "Anna"},
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(SENDGRID_API_URL, json=payload, headers=headers, timeout=15)
        if response.status_code == 202:
            return True
        print(f"  [alert] SendGrid error {response.status_code}: {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [alert] Failed to send email: {e}")
        return False


# ── Slack ───────────────────────────────────────────────────────────────────────

def send_slack_alert(watch, improved, passengers, currency="USD"):
    """Post a Block Kit alert naming the stop tier(s) that hit a new low under target.

    Skips silently (returns False) if SLACK_WEBHOOK_URL is unset or `improved` empty.
    """
    if not SLACK_WEBHOOK_URL or not improved:
        return False

    origin = watch["origin"]
    destination = watch["destination"]
    client_name = watch.get("client_name") or "—"
    target_pp = float(watch["target_price"]) / passengers
    dates = f"{_fmt_short_date(watch['date_from'])} – {_fmt_short_date(watch['date_to'])}"
    pax_label = f"{passengers} passenger{'s' if passengers > 1 else ''}"

    lines = [f"🎯 *New low: {origin} → {destination}*", f"*Client:* {client_name}"]
    for t in improved:
        pp = t["price"] / passengers
        if t["previous_low"] is not None:
            note = f" ⬇ from ${float(t['previous_low']) / passengers:,.0f}"
        else:
            note = " _(new!)_"
        fl = _tier_flight_line(t["detail"])
        extra = f" · {fl}" if fl else ""
        lines.append(f"*{t['label']}:* ${pp:,.0f}/person{note}{extra}")
    lines.append(f"*Dates:* {dates} · {pax_label}")
    lines.append(f"*Target:* ${target_pp:,.0f}/person")

    best = min(improved, key=lambda t: t["price"])
    exact_date = (best.get("detail") or {}).get("departing_at")
    google_url = _google_flights_url(origin, destination, watch["date_from"], passengers, exact_date)

    payload = {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View on Google Flights →", "emoji": True},
                "url": google_url,
            }]},
        ]
    }
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        print(f"  [slack] error {r.status_code}: {r.text[:120]}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [slack] failed to send: {e}")
        return False


# ── Internal error notice (to Anna only) ────────────────────────────────────────

def send_error_alert(watch, error_message):
    """Notify Anna (SENDER_EMAIL only — never the client) when a watch errors."""
    origin = watch["origin"]
    destination = watch["destination"]
    route = f"{origin} → {destination}"
    client_name = watch.get("client_name", "—")
    trip_type = "round-trip" if watch.get("trip_type") == "round_trip" else "one-way"
    subject = f"⚠ FareWatch error: {route}"

    html_body = f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:560px;margin:0 auto;padding:24px;">
  <p style="font-size:16px;"><strong>⚠ A fare watch hit an error</strong></p>
  <p>FareWatch couldn't complete a price check for the watch below. It's still active and will be retried on the next run.</p>

  <table style="border-collapse:collapse;width:100%;margin:20px 0;">
    <tr style="background:#f5f5f5;"><td style="padding:10px 14px;font-weight:bold;">Route</td><td style="padding:10px 14px;">{route}</td></tr>
    <tr><td style="padding:10px 14px;font-weight:bold;">Client</td><td style="padding:10px 14px;">{client_name}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:10px 14px;font-weight:bold;">Travel window</td><td style="padding:10px 14px;">{watch['date_from']} – {watch['date_to']}</td></tr>
    <tr><td style="padding:10px 14px;font-weight:bold;">Trip type</td><td style="padding:10px 14px;">{trip_type}</td></tr>
    <tr style="background:#fdecea;"><td style="padding:10px 14px;font-weight:bold;color:#c0392b;">Error</td><td style="padding:10px 14px;color:#c0392b;">{error_message}</td></tr>
  </table>

  <p style="font-size:13px;color:#888;">Common causes: an invalid airport code, no flights available for the route/dates, or a temporary Duffel API issue.</p>
  <p style="font-size:13px;color:#888;">FareWatch · automated system notification</p>
</body>
</html>
"""
    text_body = (
        f"A fare watch hit an error.\n\n"
        f"Route: {route}\nClient: {client_name}\n"
        f"Travel window: {watch['date_from']} - {watch['date_to']}\n"
        f"Trip type: {trip_type}\n\n"
        f"Error: {error_message}\n\n"
        f"The watch is still active and will be retried on the next run.\n"
        f"FareWatch · automated system notification"
    )

    payload = {
        "personalizations": [{"to": [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}], "subject": subject}],
        "from": {"email": SENDER_EMAIL, "name": "FareWatch Alerts"},
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(SENDGRID_API_URL, json=payload, headers=headers, timeout=15)
        if response.status_code == 202:
            print(f"  [error-alert] Error email sent to {SENDER_EMAIL}")
            return True
        print(f"  [error-alert] SendGrid error {response.status_code}: {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [error-alert] Failed to send: {e}")
        return False
