"""
Run this to backfill or regenerate client_token for all existing watches.
Watches sharing the same client_email get the same token.
Tokens are formatted as: firstname-lastname-xxxx (e.g. anna-knoll-a3f2)
"""
import os
import re
import uuid
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)


def make_token(client_name):
    slug = re.sub(r'[^a-z0-9]+', '-', client_name.lower()).strip('-')
    suffix = uuid.uuid4().hex[:4]
    return f"{slug}-{suffix}"


watches = supabase.table("watches").select("id, client_email, client_name, client_token").execute().data

# Build token map: one token per client_email
# Always regenerate so existing UUID tokens get replaced with readable ones
token_map = {}
name_map = {}

for w in watches:
    email = w["client_email"]
    if email not in token_map:
        token_map[email] = make_token(w["client_name"])
        name_map[email] = w["client_name"]

# Update all watches
updated = 0
for w in watches:
    token = token_map[w["client_email"]]
    supabase.table("watches").update({"client_token": token}).eq("id", w["id"]).execute()
    updated += 1

print(f"Done. Updated {updated} watch(es).")
print()
print("Client links:")
seen = set()
for w in watches:
    email = w["client_email"]
    if email not in seen:
        print(f"  {w['client_name']} ({email})")
        print(f"  → https://farewatch.annaknoll.com/client/{token_map[email]}")
        print()
        seen.add(email)
