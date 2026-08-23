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

Flight prices passed in are TOTALS (all passengers); messages show the per-person
figure since that's what a flight target is set in. Hotels are the opposite: they
sell room-nights, so hotel messages show the nightly ROOM rate and never divide by
guests.
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

# Shown when a nonstop-only watch falls back because the route has no direct
# service on these dates. One string so the three channels stay in step.
NONSTOP_NOTE = ("This route has no nonstop service on these dates, so this is the cheapest option rather than a direct flight.")


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

def _build_tier_alert_html(watch, improved, passengers, currency,
                           nonstop_unavailable=False):
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
  {f'<p style="font-size:13px;color:#a06010;margin:0 0 12px;">{NONSTOP_NOTE}</p>' if nonstop_unavailable else ''}
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


def _build_tier_alert_text(watch, improved, passengers, currency,
                           nonstop_unavailable=False):
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
    if nonstop_unavailable:
        lines += [NONSTOP_NOTE, ""]

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


def send_alert(watch, improved, passengers, currency="USD",
               nonstop_unavailable=False):
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

    html_body = _build_tier_alert_html(watch, improved, passengers, currency,
                                      nonstop_unavailable=nonstop_unavailable)
    text_body = _build_tier_alert_text(watch, improved, passengers, currency,
                                      nonstop_unavailable=nonstop_unavailable)

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

def send_slack_alert(watch, improved, passengers, currency="USD",
                     nonstop_unavailable=False):
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
    if nonstop_unavailable:
        lines.append(f"_{NONSTOP_NOTE}_")

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


# ── Hotels (LiteAPI) ──────────────────────────────────────────────────────────
# Hotel alerts mirror the flight ones but are simpler: a single tracked price
# (net per-night-per-person) rather than per-stop tiers. Prices passed in are the
# values from hotel_price_history; the alert is framed per-night-per-person since
# that's the unit the hotel target is set in.

def _google_hotel_url(name, city):
    q = f"{name} {city}".strip()
    return f"https://www.google.com/travel/search?q={urllib.parse.quote(q)}"


def _fees_note(rate, currency):
    """' + USD 43.61 in taxes/fees payable at the property' — or '' if none.

    retailRate.total only covers taxes flagged included:true, so an excluded
    resort/facility fee is real money the client pays on arrival. Saying nothing
    would quote them low.
    """
    fees = rate.get("excluded_fees_amount") or 0
    if not fees:
        return ""
    return f" + {currency} {fees:,.2f} in taxes/fees payable at the property"


def _refund_label(refundable):
    return "Refundable" if refundable else "Non-refundable" if refundable is False else ""


def _sendgrid_send(to_list, cc_list, subject, text_body, html_body,
                   from_name="Anna | FareWatch", log_tag="hotel-alert"):
    """Shared SendGrid POST. Returns True on 202, False otherwise (never raises)."""
    payload = {
        "personalizations": [{
            "to": to_list,
            **({"cc": cc_list} if cc_list else {}),
            "subject": subject,
        }],
        "from": {"email": SENDER_EMAIL, "name": from_name},
        "reply_to": {"email": SENDER_EMAIL, "name": "Anna"},
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(SENDGRID_API_URL, json=payload, headers=headers, timeout=15)
        if r.status_code == 202:
            return True
        print(f"  [{log_tag}] SendGrid error {r.status_code}: {r.text}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [{log_tag}] Failed to send email: {e}")
        return False


def _build_hotel_alert_html(hw, rate, previous_low, currency):
    name = hw["accommodation_name"]
    city = hw.get("accommodation_city") or ""
    client_name = hw.get("client_name") or "there"
    nightly = rate["per_night_amount"]
    target = float(hw["target_price_per_night"])
    nights = rate["nights"]
    guests = hw["guests"]
    refund = _refund_label(rate.get("refundable"))
    refund_html = f' · {refund}' if refund else ""
    board_html = f' · {rate.get("board_name")}' if rate.get("board_name") else ""

    if previous_low is not None:
        note = (f'<span style="color:#2a7a2a;font-weight:normal;font-size:13px;">'
                f' ▼ down from {currency} {previous_low:,.2f}/night</span>')
    else:
        note = '<span style="color:#2a7a2a;font-weight:normal;font-size:13px;"> (new!)</span>'

    google = _google_hotel_url(name, city)
    token = hw.get("client_token")
    dash = ""
    if token:
        dash = (f'<p style="margin:20px 0 8px;"><a href="{BASE_URL}/client/{token}" '
                f'style="background:#1D9E75;color:white;padding:12px 24px;text-decoration:none;'
                f'border-radius:4px;font-weight:bold;">View your dashboard →</a></p>')

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:560px;margin:0 auto;padding:24px;">
  <p>Hi {client_name},</p>
  <p>Good news — a better rate just turned up for <strong>{name}</strong>{f' in {city}' if city else ''}:</p>
  <div style="border-left:3px solid #1D9E75;background:#f6fbf9;padding:8px 14px;margin:12px 0;">
    <div style="font-size:16px;font-weight:bold;color:#1D9E75;">{currency} {nightly:,.2f}/night{note}</div>
    <div style="font-size:13px;color:#555;margin-top:3px;">{rate.get('rate_name') or 'Room'}{board_html} · total {currency} {rate['total_amount']:,.2f} for {nights} night{'s' if nights != 1 else ''}{refund_html}</div>
    {f'<div style="font-size:12px;color:#a06010;margin-top:3px;">{_fees_note(rate, currency).lstrip(" +")}</div>' if _fees_note(rate, currency) else ''}
  </div>
  <table style="border-collapse:collapse;width:100%;margin:18px 0;font-size:14px;color:#555;">
    <tr><td style="padding:4px 0;">Stay</td><td style="padding:4px 0;text-align:right;">{hw['check_in']} – {hw['check_out']} ({guests} guest{'s' if guests != 1 else ''})</td></tr>
    <tr><td style="padding:4px 0;">Your target</td><td style="padding:4px 0;text-align:right;">{currency} {target:,.2f}/night</td></tr>
  </table>
  <p style="margin:16px 0 4px;"><a href="{google}" style="background:#1a73e8;color:white;padding:12px 24px;text-decoration:none;border-radius:4px;font-weight:bold;">View hotel on Google →</a></p>
  <p style="font-size:12px;color:#888;">This is the lowest rate we observed on the last check — hotel rates are live and can change until booked.</p>
  <p>Rates can move quickly — <strong>reply to this email</strong> and I'll get it booked for you.</p>
  {dash}
  <p style="margin-top:28px;padding-top:16px;border-top:1px solid #eee;font-size:13px;color:#888;">
    Anna Knoll &nbsp;·&nbsp; Independent Travel Advisor &nbsp;·&nbsp; Fora Travel<br>
    <a href="mailto:{SENDER_EMAIL}" style="color:#1D9E75;">{SENDER_EMAIL}</a>
  </p>
</body>
</html>"""


def _build_hotel_alert_text(hw, rate, previous_low, currency):
    name = hw["accommodation_name"]
    city = hw.get("accommodation_city") or ""
    nightly = rate["per_night_amount"]
    target = float(hw["target_price_per_night"])
    nights = rate["nights"]
    note = (f" (down from {currency} {previous_low:,.2f}/night)"
            if previous_low is not None else " (new!)")
    refund = _refund_label(rate.get("refundable"))
    lines = [
        f"Hi {hw.get('client_name') or 'there'},", "",
        f"Good news — a better rate just turned up for {name}{f' in {city}' if city else ''}:", "",
        f"{currency} {nightly:,.2f}/night{note}",
        f"  {rate.get('rate_name') or 'Room'} · total {currency} {rate['total_amount']:,.2f} for {nights} night(s)"
        + _fees_note(rate, currency)
        + (f" · {rate.get('board_name')}" if rate.get('board_name') else "")
        + (f" · {refund}" if refund else ""),
        "",
        f"Stay: {hw['check_in']} – {hw['check_out']} ({hw['guests']} guest(s))",
        f"Your target: {currency} {target:,.2f}/night",
        "",
        f"View hotel: {_google_hotel_url(name, city)}",
        "",
        "This is the lowest rate we observed on the last check — hotel rates are live and can change until booked.",
        "Rates can move quickly — reply to this email and I'll get it booked for you.",
    ]
    if hw.get("client_token"):
        lines.append(f"Your dashboard: {BASE_URL}/client/{hw['client_token']}")
    lines += ["", "Anna Knoll · Independent Travel Advisor · Fora Travel", SENDER_EMAIL]
    return "\n".join(lines)


def send_hotel_alert(hotel_watch, rate, previous_low=None):
    """Email the client (cc Anna) when a hotel's per-night-per-person net rate hits
    a new low at/below target. Returns True on success, False otherwise (never raises).
    """
    currency = rate.get("currency") or "USD"
    name = hotel_watch["accommodation_name"]
    nightly = rate["per_night_amount"]
    client_email = (hotel_watch.get("client_email") or "").strip()
    client_name = hotel_watch.get("client_name") or ""

    subject = f"Hotel alert: {name} — {currency} {nightly:,.2f}/night"
    html_body = _build_hotel_alert_html(hotel_watch, rate, previous_low, currency)
    text_body = _build_hotel_alert_text(hotel_watch, rate, previous_low, currency)

    to_list = []
    if client_email and "@" in client_email and client_email != SENDER_EMAIL:
        to_list.append({"email": client_email, "name": client_name})
    cc_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]
    if not to_list:
        to_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]
        cc_list = []

    return _sendgrid_send(to_list, cc_list, subject, text_body, html_body, log_tag="hotel-alert")


def send_hotel_slack_alert(hotel_watch, rate, previous_low=None):
    """Post a Slack alert for a hotel rate hitting a new low under target.
    Skips silently (returns False) if SLACK_WEBHOOK_URL is unset."""
    if not SLACK_WEBHOOK_URL:
        return False
    currency = rate.get("currency") or "USD"
    name = hotel_watch["accommodation_name"]
    city = hotel_watch.get("accommodation_city") or "—"
    nightly = rate["per_night_amount"]
    target = float(hotel_watch["target_price_per_night"])
    # Label prices with the rate's own currency, like the email and the stored
    # history do — not a hardcoded "$". LiteAPI is asked for USD today, so this
    # is the same string in practice; it stops being so the moment a watch is
    # priced in anything else.
    note = (f" ⬇ from {currency} {previous_low:,.2f}" if previous_low is not None else " _(new!)_")
    refund = _refund_label(rate.get("refundable"))
    dates = f"{_fmt_short_date(hotel_watch['check_in'])} – {_fmt_short_date(hotel_watch['check_out'])}"

    lines = [
        f"🏨 *New hotel low: {name}*",
        f"*Client:* {hotel_watch.get('client_name') or '—'} · {city}",
        f"*Lowest observed:* {currency} {nightly:,.2f}/night{note}",
        f"*{rate.get('rate_name') or 'Room'}:* total {currency} {rate['total_amount']:,.2f} for {rate['nights']} night(s)"
        + _fees_note(rate, currency)
        + (f" · {rate.get('board_name')}" if rate.get('board_name') else "")
        + (f" · {refund}" if refund else ""),
        f"*Stay:* {dates} · {hotel_watch['guests']} guest(s)",
        f"*Target:* {currency} {target:,.2f}/night",
        "_Rates are live and can change until booked._",
    ]
    payload = {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View hotel on Google →", "emoji": True},
                "url": _google_hotel_url(name, city if city != "—" else ""),
            }]},
        ]
    }
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        print(f"  [hotel-slack] error {r.status_code}: {r.text[:120]}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [hotel-slack] failed to send: {e}")
        return False


def send_hotel_error_alert(hotel_watch, error_message):
    """Notify Anna (only) when a hotel watch errors. Mirrors send_error_alert."""
    name = hotel_watch["accommodation_name"]
    city = hotel_watch.get("accommodation_city") or "—"
    client_name = hotel_watch.get("client_name") or "—"
    subject = f"⚠ FareWatch hotel error: {name}"
    text_body = (
        f"A hotel watch hit an error.\n\n"
        f"Hotel: {name} ({city})\nClient: {client_name}\n"
        f"Stay: {hotel_watch['check_in']} - {hotel_watch['check_out']}\n\n"
        f"Error: {error_message}\n\n"
        f"The watch is still active and will be retried on the next run.\n"
        f"FareWatch · automated system notification"
    )
    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:560px;margin:0 auto;padding:24px;">
  <p style="font-size:16px;"><strong>⚠ A hotel watch hit an error</strong></p>
  <p>FareWatch couldn't complete a rate check for the hotel below. It's still active and will be retried on the next run.</p>
  <table style="border-collapse:collapse;width:100%;margin:20px 0;">
    <tr style="background:#f5f5f5;"><td style="padding:10px 14px;font-weight:bold;">Hotel</td><td style="padding:10px 14px;">{name} ({city})</td></tr>
    <tr><td style="padding:10px 14px;font-weight:bold;">Client</td><td style="padding:10px 14px;">{client_name}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:10px 14px;font-weight:bold;">Stay</td><td style="padding:10px 14px;">{hotel_watch['check_in']} – {hotel_watch['check_out']}</td></tr>
    <tr style="background:#fdecea;"><td style="padding:10px 14px;font-weight:bold;color:#c0392b;">Error</td><td style="padding:10px 14px;color:#c0392b;">{error_message}</td></tr>
  </table>
  <p style="font-size:13px;color:#888;">FareWatch · automated system notification</p>
</body>
</html>"""
    payload = {
        "personalizations": [{"to": [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}], "subject": subject}],
        "from": {"email": SENDER_EMAIL, "name": "FareWatch Alerts"},
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(SENDGRID_API_URL, json=payload, headers=headers, timeout=15)
        if r.status_code == 202:
            print(f"  [hotel-error-alert] Error email sent to {SENDER_EMAIL}")
            return True
        print(f"  [hotel-error-alert] SendGrid error {r.status_code}: {r.text}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [hotel-error-alert] Failed to send: {e}")
        return False
