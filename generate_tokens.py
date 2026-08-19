"""
Run this to backfill or regenerate client_token for all existing watches.
Watches sharing the same client_email get the same token.
Tokens are formatted as: firstname-<22 random chars> (e.g. anna-vK7pQ2mRxN9wLd4Tf8Bz3s)

RUNNING THIS INVALIDATES EVERY EXISTING CLIENT LINK. Each client needs the new
URL printed at the end before their old bookmark stops working. That is the
point when replacing the old short tokens: the old ones were 4 hex characters
behind a guessable name (65,536 possibilities, walkable by a script in minutes),
and they stay guessable until they are gone.

IMPORTANT: this covers BOTH `watches` (flights) and `hotel_watches`. A client's
token is shared across the two tables (see app._get_or_create_token) and
/client/<token> looks the client up in both — so regenerating only `watches`
would silently split them: the new link would show flights but no hotels, while
the client's old link kept showing only the hotels.
"""
import os
import re
import secrets
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)


def make_token(client_name):
    """Mirrors app._make_token — keep the two in step. First-name prefix so the
    surname doesn't ride along in history and bookmarks, plus 128 bits of
    randomness because the token is the only access control on /client/<token>."""
    first = re.sub(r'[^a-z0-9]+', '-', client_name.lower()).strip('-').split('-')[0] or "client"
    return f"{first}-{secrets.token_urlsafe(16)}"


cols = "id, client_email, client_name, client_token"
watches = supabase.table("watches").select(cols).execute().data
hotel_watches = supabase.table("hotel_watches").select(cols).execute().data

# Build token map: one token per client_email, across BOTH tables, so a client
# with flights and hotels keeps a single working /client/<token> link.
# Always regenerate so short/legacy tokens get replaced with high-entropy ones
token_map = {}
name_map = {}

for w in watches + hotel_watches:
    email = w["client_email"]
    if not email:
        continue
    if email not in token_map:
        token_map[email] = make_token(w["client_name"] or "client")
        name_map[email] = w["client_name"]

# Update both tables
updated = 0
for table, rows in (("watches", watches), ("hotel_watches", hotel_watches)):
    for w in rows:
        if not w["client_email"]:
            continue
        token = token_map[w["client_email"]]
        supabase.table(table).update({"client_token": token}).eq("id", w["id"]).execute()
        updated += 1

print(f"Done. Updated {updated} watch(es) "
      f"({len(watches)} flight, {len(hotel_watches)} hotel).")
print()
print("Client links:")
seen = set()
for w in watches + hotel_watches:
    email = w["client_email"]
    if email and email not in seen:
        print(f"  {w['client_name']} ({email})")
        print(f"  → https://farewatch.annaknoll.com/client/{token_map[email]}")
        print()
        seen.add(email)
