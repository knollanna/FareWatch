# FareWatch — Product Overview

*A fare-monitoring assistant for an independent travel advisor.*

---

## What it is

FareWatch watches flight prices on behalf of a travel advisor's clients. The
advisor sets up a **watch** — a specific route, set of dates, and a target price
for a client — and FareWatch quietly checks the fare around the clock. When a
price drops to (or below) the target, it sends the advisor and the client an
alert so the trip can be booked while the deal lasts.

It replaces the manual, easy-to-forget habit of re-checking flight prices by
hand, and it gives each client a personal, always-current view of the fares
being tracked for them.

Built for **Anna Knoll · Independent Travel Advisor · Fora Travel**.

---

## Who it's for

- **The advisor (primary user).** Manages all client watches from a single
  password-protected dashboard, and receives alerts the moment a fare is worth
  acting on.
- **The clients (indirect users).** Each client gets a private link to a clean
  status page showing the fares being watched for them — no login, no app to
  install. They reply to an alert email when they want to book; the advisor
  handles the rest.

---

## The problem it solves

Flight prices move constantly and unpredictably. For a travel advisor juggling
many clients and trips, manually re-checking fares is tedious and things slip
through the cracks — a fare dips for a few hours and the moment is missed.

FareWatch automates the watching and the watching only: it surfaces the right
moment to book, while keeping the advisor in control of the actual booking and
the client relationship.

---

## How it works (in plain terms)

1. **Set up a watch.** The advisor enters a route (with airport autocomplete),
   a departure window (and a return window for round-trips), the number of
   passengers, a per-person target price, and which client it's for.
2. **FareWatch checks the fare every couple of hours, day and night**, using
   live airline pricing (via the Duffel API). It records every price it sees.
3. **When a fare hits the target** — and it's a new low, so there's no repeat
   spam — FareWatch sends an **email** and a **Slack** message with the price,
   the flight details, and how far it dropped.
4. **The client books with the advisor.** The alert invites the client to simply
   reply; the advisor completes the booking and marks the watch as booked.

If a route ever errors (e.g. no flights for those dates), the advisor gets a
heads-up email so nothing fails silently.

---

## Key features

**Flight monitoring**
- One-way and round-trip watches, each with flexible date windows.
- Per-person target pricing (entered per person, compared correctly for the
  whole party).
- Real airline fares via Duffel, checked every 2 hours, 24/7.
- Captures the winning flight's airline, flight numbers, departure/return times,
  and number of stops with connecting airports.

**Smart alerts**
- Email (to the client, copying the advisor) **and** Slack, fired together.
- Only alerts on a genuine **new low at/below target** — never repetitive.
- Shows "down from $X (new low!)" context so the trend is obvious at a glance.
- A booking hand-off: route, exact date, and flight number ready to search on
  the airline/Duffel side.

**Client experience**
- A private, link-only status page per client (no login required).
- Live current price vs target, a progress bar, and a price-history chart.
- A clear "🎯 target reached" state, auto-refreshing, professionally branded.

**Advisor dashboard**
- All watches grouped by client, with at-a-glance metrics.
- Add / edit / pause / resume / delete watches inline.
- Price-history charts and a record of every alert sent.
- A service-usage page tracking the tools FareWatch depends on.

**History & trends**
- Every price check is stored, building a long-term dataset for spotting
  patterns over time (e.g. how a fare moves week to week).

---

## What it intentionally does *not* do

- **It doesn't replace the advisor.** FareWatch flags the moment to book; the
  advisor books and owns the client relationship.
- **It doesn't auto-purchase tickets.** Booking is a deliberate, human step.
- **It isn't a consumer app.** It's a private tool for one advisor and their
  clients.

---

## Status & roadmap

**Live today:** flight monitoring, email + Slack alerts, client status pages,
price history, and the advisor dashboard — running in production.

**In progress:** hotel price monitoring (the database is ready; the hotel
pricing integration is paused pending access to Duffel's Stays product).

**Ideas for later:** richer price-trend analysis, and a more direct booking
hand-off.

---

*For how FareWatch is built and run, see the main [README](../README.md).*
