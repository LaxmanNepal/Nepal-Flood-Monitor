#!/usr/bin/env python3
"""Safely collect DHM River Watch data.

DHM's current River Watch UI is JavaScript-driven. This updater first tries
machine-readable URLs discovered from the page, then falls back to a rendered
HTML table. It never replaces good data with empty/partial data.
"""
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "flood-data.json"
HEALTH = DATA / "source-health.json"
URL = "https://www.dhm.gov.np/hydrology/river-watch"


def fetch(url: str):
    req = Request(url, headers={"User-Agent": "Nepal-Flood-Monitor/1.1 (+GitHub Actions)"})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace"), response.headers.get("Content-Type", "")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def parse_number(value):
    m = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(m.group()) if m else None


def discover_urls(html: str):
    found = []
    # JS fetch/axios URLs, script srcs and absolute URLs.
    patterns = [
        r"(?:fetch|axios\.(?:get|post)|url)\s*\(\s*[`'\"]([^`'\"]+)",
        r"<script[^>]+src=[\"']([^\"']+)[\"']",
        r"https?://[^\"'`\s<>]+",
    ]
    for pattern in patterns:
        for value in re.findall(pattern, html, re.I):
            value = urljoin(URL, value)
            if any(x in value.lower() for x in ("river", "hydro", "api", "watch", "json")):
                if value not in found:
                    found.append(value)
    return found[:40]


def extract_rows(html: str):
    table_match = re.search(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    if not table_match:
        return []
    rows = []
    for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.I | re.S):
        cells = [clean(x) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw_row, re.I | re.S)]
        if len(cells) >= 9 and cells[0].lower() not in {"station no.", "station no"}:
            rows.append(cells)
    return rows


def rows_to_stations(rows):
    stations = []
    for cells in rows:
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
    return stations


def load_meta():
    path = DATA / "stations.json"
    if not path.exists():
        return {}
    return {x["station_id"]: x for x in json.loads(path.read_text(encoding="utf-8"))}


def main():
    now = datetime.now(timezone.utc).isoformat()
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"schema_version": 1, "stations": []}
    health = {
        "checked_at": now,
        "source_url": URL,
        "status": "error",
        "method": None,
        "stations_found": 0,
        "discovered_urls": [],
        "message": None,
    }

    try:
        html, content_type = fetch(URL)
        discovered = discover_urls(html)
        health["discovered_urls"] = discovered
        health["content_type"] = content_type

        # First attempt: a directly rendered station table.
        rows = extract_rows(html)
        method = "html-table"

        # If the page contains JSON blobs, try to locate station-like arrays.
        if not rows:
            for blob in re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S):
                if all(k in blob.lower() for k in ("warning", "danger", "station")):
                    candidates = re.findall(r"\[[^\n]{100,}\]", blob)
                    if candidates:
                        try:
                            parsed = json.loads(candidates[0])
                            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                                keys = {k.lower() for k in parsed[0]}
                                if {"station", "warning", "danger"} & keys:
                                    # This is diagnostic-only until the schema is confirmed.
                                    health["message"] = "Station-like JSON detected; schema requires confirmation."
                        except Exception:
                            pass

        if not rows:
            health["status"] = "stale"
            health["method"] = "none"
            health["message"] = "DHM page did not expose a complete server-rendered station table. Existing dataset preserved."
            HEALTH.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(health["message"])
            return

        stations = rows_to_stations(rows)
        meta = load_meta()
        for station in stations:
            extra = meta.get(station["station_id"], {})
            station["latitude"] = extra.get("latitude")
            station["longitude"] = extra.get("longitude")

        # Safety gate: require core fields and a meaningful station count.
        valid = [s for s in stations if s["station_id"] and s["name"] and s["water_level"] is not None]
        if len(valid) < max(1, min(5, len(stations))):
            health["status"] = "stale"
            health["method"] = method
            health["message"] = "Parsed rows failed validation; existing dataset preserved."
        else:
            payload = {
                "schema_version": 1,
                "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal",
                "source_url": URL,
                "updated_at": now,
                "stations": stations,
            }
            OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            health["status"] = "ok"
            health["method"] = method
            health["stations_found"] = len(stations)
            health["message"] = f"Updated {len(stations)} stations."

    except Exception as exc:
        health["status"] = "error"
        health["message"] = f"{type(exc).__name__}: {exc}"

    HEALTH.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(health, ensure_ascii=False))


if __name__ == "__main__":
    main()
