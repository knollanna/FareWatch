import os
import urllib.parse
import requests
from dotenv import load_dotenv

load_dotenv()

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
BASE_URL = os.environ.get("BASE_URL", "https://farewatch.annaknoll.com")
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




def _format_datetime(dt_str):
    """Turn '2026-06-12T09:45:00' into 'June 12, 2026 at 9:45am'."""
    if not dt_str:
        return dt_str
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%-d %B %Y at %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        return dt_str


def _build_email_html(watch, price, flight_details):
    origin = watch["origin"]
    destination = watch["destination"]
    date_from = watch["date_from"]
    date_to = watch["date_to"]
    passengers = watch["passengers"]
    target_price = float(watch["target_price"])
    client_name = watch["client_name"]
    currency = "USD"

    pax_label = f"{passengers} passenger{'s' if passengers > 1 else ''}"
    if passengers > 1:
        price_note = f" total ({currency} {price / passengers:.2f} per person)"
        target_note = f" total ({currency} {target_price / passengers:.2f} per person)"
    else:
        price_note = ""
        target_note = ""
    exact_date = flight_details["departing_at"] if flight_details else None
    google_url = _google_flights_url(origin, destination, date_from, passengers, exact_date)

    client_token = watch.get("client_token")
    if client_token:
        client_url = f"{BASE_URL}/client/{client_token}"
        client_page_section = f"""
  <p style="margin:20px 0 8px;">
    <a href="{client_url}"
       style="background:#1D9E75;color:white;padding:12px 24px;text-decoration:none;border-radius:4px;font-weight:bold;">
      View your fare dashboard →
    </a>
  </p>"""
    else:
        client_page_section = ""

    # Flight details rows (only shown if we have them)
    flight_rows = ""
    if flight_details:
        dep = _format_datetime(flight_details["departing_at"])
        arr = _format_datetime(flight_details["arriving_at"])
        stops_label = flight_details.get("stops_label", "")
        flight_rows = f"""
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Flight</td>
      <td style="padding:10px 14px;">{flight_details['flight_number']} ({flight_details['airline']})</td>
    </tr>
    <tr>
      <td style="padding:10px 14px;font-weight:bold;">Stops</td>
      <td style="padding:10px 14px;">{stops_label}</td>
    </tr>
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Trip type</td>
      <td style="padding:10px 14px;">{flight_details['trip_type']}</td>
    </tr>
    <tr>
      <td style="padding:10px 14px;font-weight:bold;">Departs</td>
      <td style="padding:10px 14px;">{dep}</td>
    </tr>
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Arrives</td>
      <td style="padding:10px 14px;">{arr}</td>
    </tr>"""
        iata = flight_details["flight_number"].split()[0].upper()
        airline_info = AIRLINE_WEBSITES.get(iata)
        airline_link = (
            f' or go directly to <a href="{airline_info[1]}">{airline_info[0]}</a>'
            if airline_info else ""
        )
        link_section = f"""
  <p style="margin:20px 0 8px;">
    <a href="{google_url}"
       style="background:#1a73e8;color:white;padding:12px 24px;text-decoration:none;border-radius:4px;font-weight:bold;">
      Search this route on Google Flights →
    </a>
  </p>
  <p style="margin:0 0 20px;font-size:13px;color:#555;">
    Search for flight <strong>{flight_details['flight_number']}</strong> departing {dep}{airline_link}.
  </p>"""
    else:
        dep = None
        link_section = f"""
  <p style="margin:20px 0;">
    <a href="{google_url}"
       style="background:#1a73e8;color:white;padding:12px 24px;text-decoration:none;border-radius:4px;font-weight:bold;">
      Search this route on Google Flights →
    </a>
  </p>"""

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:560px;margin:0 auto;padding:24px;">

  <p>Hi {client_name},</p>
  <p>Great news — a fare has dropped to your target price for your trip!</p>

  <table style="border-collapse:collapse;width:100%;margin:20px 0;">
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Route</td>
      <td style="padding:10px 14px;">{origin} → {destination}</td>
    </tr>
    <tr>
      <td style="padding:10px 14px;font-weight:bold;">Travel window</td>
      <td style="padding:10px 14px;">{date_from} – {date_to}</td>
    </tr>
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Passengers</td>
      <td style="padding:10px 14px;">{pax_label}</td>
    </tr>{flight_rows}
    <tr>
      <td style="padding:10px 14px;font-weight:bold;">Current price</td>
      <td style="padding:10px 14px;color:#2a7a2a;font-weight:bold;">{currency} {price:.2f}{price_note}</td>
    </tr>
    <tr style="background:#f5f5f5;">
      <td style="padding:10px 14px;font-weight:bold;">Your target</td>
      <td style="padding:10px 14px;">{currency} {target_price:.2f}{target_note}</td>
    </tr>
  </table>

  {link_section}

  <p>Fares at this price can disappear quickly — <strong>reply to this email</strong> and I'll get your booking sorted right away.</p>

  {client_page_section}

  <p style="margin-top:28px;padding-top:16px;border-top:1px solid #eee;font-size:13px;color:#888;">
    Anna Knoll &nbsp;·&nbsp; Independent Travel Advisor &nbsp;·&nbsp; Fora Travel<br>
    <a href="mailto:{SENDER_EMAIL}" style="color:#1D9E75;">{SENDER_EMAIL}</a>
  </p>

</body>
</html>
"""


def _build_email_text(watch, price, flight_details):
    origin = watch["origin"]
    destination = watch["destination"]
    date_from = watch["date_from"]
    date_to = watch["date_to"]
    passengers = watch["passengers"]
    target_price = float(watch["target_price"])
    client_name = watch["client_name"]
    currency = "USD"
    exact_date = flight_details["departing_at"] if flight_details else None
    google_url = _google_flights_url(origin, destination, date_from, passengers, exact_date)
    client_token = watch.get("client_token")
    client_url = f"{BASE_URL}/client/{client_token}" if client_token else None

    flight_lines = ""
    if flight_details:
        dep = _format_datetime(flight_details["departing_at"])
        arr = _format_datetime(flight_details["arriving_at"])
        stops_label = flight_details.get("stops_label", "")
        flight_lines = (
            f"Flight: {flight_details['flight_number']} ({flight_details['airline']})\n"
            f"Stops: {stops_label}\n"
            f"Trip type: {flight_details['trip_type']}\n"
            f"Departs: {dep}\n"
            f"Arrives: {arr}\n"
        )
        iata = flight_details["flight_number"].split()[0].upper()
        airline_info = AIRLINE_WEBSITES.get(iata)
        airline_line = f"\n{airline_info[0]} website: {airline_info[1]}" if airline_info else ""
        search_tip = f"Search Google Flights: {google_url}\nLook for flight {flight_details['flight_number']} departing {dep}.{airline_line}"
    else:
        search_tip = f"Search Google Flights: {google_url}"

    return f"""Hi {client_name},

Great news — a fare has dropped to your target price for your trip!

Route: {origin} → {destination}
Travel window: {date_from} – {date_to}
Passengers: {passengers}
{flight_lines}Current price: {currency} {price:.2f}{f" total ({currency} {price / passengers:.2f} per person)" if passengers > 1 else ""}
Your target: {currency} {target_price:.2f}{f" total ({currency} {target_price / passengers:.2f} per person)" if passengers > 1 else ""}

{search_tip}

Fares at this price can disappear quickly — reply to this email and I'll get your booking sorted right away.
{f"View your fare dashboard: {client_url}" if client_url else ""}

Anna Knoll · Independent Travel Advisor · Fora Travel

Warm regards,
Anna
{SENDER_EMAIL}
"""


def send_alert(watch, price, flight_details=None):
    """Send a fare alert email to the client (and a copy to SENDER_EMAIL)."""
    client_email = watch.get("client_email", "").strip()
    client_name = watch["client_name"]
    origin = watch["origin"]
    destination = watch["destination"]
    subject = f"Fare alert: {origin} → {destination} — USD {price:.2f}"

    html_body = _build_email_html(watch, price, flight_details)
    text_body = _build_email_text(watch, price, flight_details)

    to_list = []
    if client_email and "@" in client_email and client_email != SENDER_EMAIL:
        to_list.append({"email": client_email, "name": client_name})

    cc_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]

    if not to_list:
        to_list = [{"email": SENDER_EMAIL, "name": "Anna (FareWatch)"}]
        cc_list = []

    payload = {
        "personalizations": [
            {
                "to": to_list,
                **({"cc": cc_list} if cc_list else {}),
                "subject": subject,
            }
        ],
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


def _fmt_short_date(d):
    """'2026-07-12' -> 'Jul 12'."""
    try:
        from datetime import date
        return date.fromisoformat(d).strftime("%b %-d")
    except Exception:
        return d


def send_slack_alert(watch, price, stops=None, previous_low=None):
    """
    Post a Block Kit alert to the Slack incoming webhook.
    Skips silently (returns False) if SLACK_WEBHOOK_URL is not configured.
    `previous_low` adds a "down from $X" context line when this fare beats it.
    """
    if not SLACK_WEBHOOK_URL:
        return False

    origin = watch["origin"]
    destination = watch["destination"]
    passengers = watch["passengers"]
    target = float(watch["target_price"])
    client_name = watch.get("client_name") or "—"
    date_from = watch["date_from"]
    date_to = watch["date_to"]

    below_target = price <= target
    emoji = "🎯" if below_target else "📈"
    headline = "Target hit" if below_target else "Price increase"

    pax_label = f"{passengers} passenger{'s' if passengers > 1 else ''}"
    dates = f"{_fmt_short_date(date_from)} – {_fmt_short_date(date_to)}"
    google_url = _google_flights_url(origin, destination, date_from, passengers)

    # Fare line, with "down from previous low" context when applicable
    if previous_low is not None and price < float(previous_low):
        fare_line = (f"*Fare:* ${price:,.0f} ⬇ down from ${float(previous_low):,.0f} "
                     f"_(new low!)_ · target ${target:,.0f}")
    else:
        fare_line = f"*Fare:* ${price:,.0f} (target: ${target:,.0f})"

    lines = [
        f"{emoji} *{headline}: {origin} → {destination}*",
        f"*Client:* {client_name}",
        fare_line,
        f"*Dates:* {dates} · {pax_label}",
    ]
    if stops:
        lines.append(f"*Flight:* {stops}")

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
