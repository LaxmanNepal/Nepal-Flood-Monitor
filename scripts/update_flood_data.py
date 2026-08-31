#!/usr/bin/env python3
"""Collect and normalize Nepal flood observations from DHM.

Primary source: DHM Real Time Stream Flow. The collector is intentionally
conservative: it never invents warning/danger classifications and it keeps the
previous valid snapshot when the source cannot be parsed safely.
"""
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "flood-data.json"
HEALTH = DATA / "source-health.json"
URL = "https://www.dhm.gov.np/hydrology/realtime-stream"


def fetch(url):
    req = Request(url, headers={"User-Agent": "Nepal-Flood-Monitor/1.2 (+GitHub Actions)"})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace"), response.headers.get("Content-Type", "")


def clean(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def number(value):
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else None


def tables(html):
    return re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)


def rows_from_table(table):
    rows = []
    for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
        cells = [clean(x) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw, re.I | re.S)]
        if cells:
            rows.append(cells)
    return rows


def find_stream_rows(html):
    for table in tables(html):
        rows = rows_from_table(table)
        if not rows:
            continue
        header = [x.lower() for x in rows[0]]
        if any("station" in x for x in header) and any("water" in x for x in header):
            return rows
    return []


def load_meta():
    path = DATA / "stations.json"
    if not path.exists():
        return {}
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        return {str(x.get("station_id", "")): x for x in items if x.get("station_id")}
    except Exception:
        return {}


def normalize(rows, meta):
    header = [x.lower() for x in rows[0]]
    def col(*names):
        for name in names:
            for i, value in enumerate(header):
                if name in value:
                    return i
        return None

    station_i = col("station index", "station no", "station")
    name_i = col("station name", "name")
    water_i = col("water lvl", "water level", "water")
    basin_i = col("basin")
    district_i = col("district")
    discharge_i = col("discharge", "flow")
    if station_i is None or name_i is None or water_i is None:
        return []

    result = []
    for cells in rows[1:]:
        if max(station_i, name_i, water_i) >= len(cells):
            continue
        station_id = cells[station_i]
        name = cells[name_i]
        water = number(cells[water_i])
        if not station_id or not name or water is None:
            continue
        extra = meta.get(station_id, {})
        item = {
            "station_id": station_id,
            "name": name,
            "basin": cells[basin_i] if basin_i is not None and basin_i < len(cells) else extra.get("basin"),
            "district": cells[district_i] if district_i is not None and district_i < len(cells) else extra.get("district"),
            "water_level": water,
            "discharge": number(cells[discharge_i]) if discharge_i is not None and discharge_i < len(cells) else None,
            "warning_level": None,
            "danger_level": None,
            "trend": None,
            "status": "observation",
            "latitude": extra.get("latitude"),
            "longitude": extra.get("longitude"),
        }
        result.append(item)
    return result


def write_health(status, now, message, count=0):
    HEALTH.write_text(json.dumps({
        "schema_version": 1,
        "checked_at": now,
        "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal",
        "source_url": URL,
        "status": status,
        "stations_found": count,
        "message": message,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    now = datetime.now(timezone.utc).isoformat()
    try:
        html, content_type = fetch(URL)
        rows = find_stream_rows(html)
        meta = load_meta()
        stations = normalize(rows, meta) if rows else []
        if len(stations) < 5:
            write_health("STALE", now, "DHM Real Time Stream Flow did not yield a validated station set; previous snapshot preserved.")
            print("No validated DHM dataset; existing snapshot preserved.")
            return

        payload = {
            "schema_version": 2,
            "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal",
            "source_url": URL,
            "updated_at": now,
            "data_status": "LIVE",
            "note": "Water-level observations are not independently classified as warning/danger without authoritative thresholds.",
            "stations": stations,
        }
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_health("LIVE", now, f"Validated {len(stations)} DHM stream-flow stations.", len(stations))
        print(json.dumps({"status": "LIVE", "stations": len(stations), "content_type": content_type}))
    except Exception as exc:
        write_health("ERROR", now, f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
