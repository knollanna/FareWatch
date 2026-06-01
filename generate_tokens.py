"""
Run this once to backfill client_token for all existing watches.
Watches sharing the same client_email get the same token.
"""
import os
import uuid
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)

watches = supabase.table("watches").select("id, client_email, client_name, client_token").execute().data

# Group by client_email — one token per client
token_map = {}  # email -> token

# First pass: collect any tokens already assigned
for w in watches:
    if w.get("client_token") and w["client_email"] not in token_map:
        token_map[w["client_email"]] = w["client_token"]

# Second pass: generate tokens for emails that don't have one yet
for w in watches:
    if w["client_email"] not in token_map:
        token_map[w["client_email"]] = str(uuid.uuid4())

# Third pass: update all watches
updated = 0
for w in watches:
    token = token_map[w["client_email"]]
    if w.get("client_token") != token:
        supabase.table("watches").update({"client_token": token}).eq("id", w["id"]).execute()
        updated += 1

print(f"Done. Updated {updated} watch(es).")
print()
print("Client tokens:")
seen = set()
for w in watches:
    email = w["client_email"]
    if email not in seen:
        print(f"  {w['client_name']} ({email})")
        print(f"  → farewatch.annaknoll.com/client/{token_map[email]}")
        print()
        seen.add(email)
