"""
One-time script: download the OurAirports dataset, filter to medium/large
airports that have an IATA code, and write static/airports.json.

Run again only if you want to refresh the data:
    python prepare_airports.py
"""
import csv
import io
import json
import os
import requests

SOURCE_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
OUTPUT_PATH = os.path.join("static", "airports.json")
KEEP_TYPES = {"medium_airport", "large_airport"}


def main():
    print(f"Downloading {SOURCE_URL} ...")
    resp = requests.get(SOURCE_URL, timeout=60)
    resp.raise_for_status()
    print(f"Downloaded {len(resp.content) // 1024} KB.")

    reader = csv.DictReader(io.StringIO(resp.text))
    airports = []
    for row in reader:
        if row.get("type") not in KEEP_TYPES:
            continue
        iata = (row.get("iata_code") or "").strip().upper()
        if not iata or len(iata) != 3:
            continue
        airports.append({
            "iata_code": iata,
            "name": (row.get("name") or "").strip(),
            "municipality": (row.get("municipality") or "").strip(),
            "country": (row.get("iso_country") or "").strip(),
            "type": row.get("type"),
        })

    # Sort large airports first (more likely to be searched), then alpha by code
    airports.sort(key=lambda a: (0 if a["type"] == "large_airport" else 1, a["iata_code"]))

    os.makedirs("static", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(airports, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(OUTPUT_PATH) // 1024
    print(f"Wrote {len(airports)} airports to {OUTPUT_PATH} ({size_kb} KB).")


if __name__ == "__main__":
    main()
