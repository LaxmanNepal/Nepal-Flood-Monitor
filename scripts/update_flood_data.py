#!/usr/bin/env python3
"""Fetch the public DHM River Watch page and update flood-data.json.

The DHM page is currently a JavaScript-driven interface and may change its
internal data endpoint. This updater deliberately fails closed: if no complete
station table can be extracted, the existing JSON is preserved rather than
publishing fabricated or partial live data.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "flood-data.json"
URL = "https://www.dhm.gov.np/hydrology/river-watch"


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Nepal-Flood-Monitor/1.0 (+GitHub Actions)"})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def extract_rows(html: str):
    # Conservative parser for a server-rendered table. If DHM changes its
    # markup or serves the rows only through an API, return [] and keep the
    # last known dataset.
    table_match = re.search(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    if not table_match:
        return []
    rows = []
    for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.I | re.S):
        cells = [clean(x) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw_row, re.I | re.S)]
        if len(cells) >= 9 and cells[0].lower() not in {"station no.", "station no"}:
            rows.append(cells)
    return rows


def parse_number(value):
    m = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(m.group()) if m else None


def main():
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"schema_version": 1, "stations": []}
    html = fetch(URL)
    rows = extract_rows(html)
    if not rows:
        print("DHM River Watch did not expose a server-rendered station table; preserving existing data.")
        return

    stations = []
    for cells in rows:
        # DHM columns: station, basin, station name, district, water, warning,
        # danger, trend, status. Coordinates are maintained separately.
        stations.append({
            "station_id": cells[0],
            "basin": cells[1],
            "name": cells[2],
            "district": cells[3],
            "water_level": parse_number(cells[4]),
            "warning_level": parse_number(cells[5]),
            "danger_level": parse_number(cells[6]),
            "trend": cells[7],
            "status": cells[8],
        })

    # Merge known coordinates from stations.json.
    meta = {x["station_id"]: x for x in json.loads((ROOT / "data/stations.json").read_text(encoding="utf-8"))}
    for station in stations:
        if station["station_id"] in meta:
            station.update({k: meta[station["station_id"]][k] for k in ("latitude", "longitude")})
        else:
            station["latitude"] = None
            station["longitude"] = None

    payload = {
        "schema_version": 1,
        "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal",
        "source_url": URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stations": stations,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {len(stations)} stations")


if __name__ == "__main__":
    main()
