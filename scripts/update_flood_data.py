#!/usr/bin/env python3
"""Fetch the complete DHM River Watch table, with real-time stream fallback."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "flood-data.json"
HEALTH = DATA / "source-health.json"
RIVER_WATCH = "https://www.dhm.gov.np/hydrology/river-watch"
STREAM = "https://www.dhm.gov.np/hydrology/realtime-stream"


def number(value):
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def load_meta():
    try:
        items = json.loads((DATA / "stations.json").read_text(encoding="utf-8"))
        return {str(x.get("station_id")): x for x in items if x.get("station_id")}
    except Exception:
        return {}


def risk_from_status(status, water=None, warning=None, danger=None):
    text = str(status or "").lower()
    if "danger" in text or (water is not None and danger is not None and water >= danger):
        return "critical"
    if "warning" in text or (water is not None and warning is not None and water >= warning):
        return "warning"
    if "rising" in text:
        return "watch"
    if "offline" in text or "unavailable" in text:
        return "offline"
    return "normal"


def extract_rows_with_playwright():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, locale="en-US")
        page.goto(RIVER_WATCH, wait_until="domcontentloaded", timeout=60000)
        try:
            page.get_by_text("Tabular View", exact=True).click(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(10000)
        tables = page.locator("table")
        best = []
        for i in range(tables.count()):
            rows = tables.nth(i).locator("tr")
            parsed = []
            for r in range(rows.count()):
                cells = rows.nth(r).locator("th,td")
                vals = [cells.nth(c).inner_text().strip() for c in range(cells.count())]
                if vals:
                    parsed.append(vals)
            if parsed and len(parsed) > len(best):
                best = parsed
        browser.close()
        return best


def normalize_river_watch(rows, meta):
    if not rows:
        return []
    header = [x.lower() for x in rows[0]]
    def col(*names):
        for name in names:
            for i, value in enumerate(header):
                if name in value:
                    return i
        return None
    station_i = col("station no", "station number", "station")
    basin_i = col("basin name", "basin")
    name_i = col("station name")
    district_i = col("district name", "district")
    water_i = col("water level")
    warning_i = col("warning level")
    danger_i = col("danger level")
    trend_i = col("trend")
    status_i = col("status")
    required = [station_i, name_i, water_i, warning_i, danger_i, trend_i, status_i]
    if any(i is None for i in required):
        raise RuntimeError(f"DHM River Watch schema changed. Header: {rows[0]}")
    result, seen = [], set()
    for cells in rows[1:]:
        if max(i for i in required if i is not None) >= len(cells):
            continue
        station_id = cells[station_i].strip()
        name = cells[name_i].strip()
        if not name:
            continue
        key = station_id or f"name:{name.lower()}"
        if key in seen:
            continue
        seen.add(key)
        water = number(cells[water_i])
        warning = number(cells[warning_i])
        danger = number(cells[danger_i])
        status = cells[status_i].strip() or ("Offline" if water is None else "Below Warning Level and Steady")
        extra = meta.get(station_id, {})
        result.append({
            "station_id": station_id or key,
            "name": name,
            "basin": cells[basin_i].strip() if basin_i is not None and basin_i < len(cells) else extra.get("basin"),
            "district": cells[district_i].strip() if district_i is not None and district_i < len(cells) else extra.get("district"),
            "water_level": water,
            "warning_level": warning,
            "danger_level": danger,
            "trend": cells[trend_i].strip(),
            "status": status,
            "risk_level": risk_from_status(status, water, warning, danger),
            "latitude": extra.get("latitude"),
            "longitude": extra.get("longitude"),
            "source": "DHM River Watch",
        })
    return result


def write_health(status, now, message, count=0):
    HEALTH.write_text(json.dumps({"schema_version": 3, "checked_at": now, "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal", "source_url": RIVER_WATCH, "status": status, "stations_found": count, "message": message}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stream_fallback(meta):
    req = Request(STREAM, headers={"User-Agent": "Mozilla/5.0 (Nepal-Flood-Monitor/3.0)"})
    with urlopen(req, timeout=45) as response:
        html = response.read().decode("utf-8", "replace")
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    for table in tables:
        rows = []
        for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).strip() for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw, re.I | re.S)]
            if cells:
                rows.append(cells)
        if rows and any("station" in x.lower() for x in rows[0]) and any("water" in x.lower() for x in rows[0]):
            h = [x.lower() for x in rows[0]]
            def col(*names):
                for n in names:
                    for i, v in enumerate(h):
                        if n in v:
                            return i
                return None
            si, ni, wi = col("station index", "station no", "station"), col("station name", "name"), col("water lvl", "water level", "water")
            if None in (si, ni, wi):
                continue
            out = []
            for c in rows[1:]:
                if max(si, ni, wi) >= len(c) or not c[ni].strip():
                    continue
                sid, water = c[si].strip(), number(c[wi])
                extra = meta.get(sid, {})
                out.append({"station_id": sid, "name": c[ni].strip(), "basin": extra.get("basin"), "district": extra.get("district"), "water_level": water, "warning_level": extra.get("warning_level"), "danger_level": extra.get("danger_level"), "trend": "Unknown", "status": "Observed" if water is not None else "Offline", "risk_level": risk_from_status("Observed", water, extra.get("warning_level"), extra.get("danger_level")), "latitude": extra.get("latitude"), "longitude": extra.get("longitude"), "source": "DHM Real Time Stream Flow"})
            return out
    return []


def main():
    now = datetime.now(timezone.utc).isoformat()
    meta = load_meta()
    try:
        stations = normalize_river_watch(extract_rows_with_playwright(), meta)
        if len(stations) < 20:
            raise RuntimeError(f"Only {len(stations)} River Watch rows validated")
        OUT.write_text(json.dumps({"schema_version": 4, "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal", "source_url": RIVER_WATCH, "updated_at": now, "data_status": "LIVE", "station_count": len(stations), "status_source": "DHM River Watch", "stations": stations}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_health("LIVE", now, f"Parsed {len(stations)} complete DHM River Watch rows including water level, warning, danger, trend and status.", len(stations))
        print(json.dumps({"status": "LIVE", "stations": len(stations)}))
        return
    except Exception as primary_error:
        print(f"River Watch failed: {primary_error}")
        try:
            stations = stream_fallback(meta)
            if len(stations) >= 20:
                OUT.write_text(json.dumps({"schema_version": 4, "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal", "source_url": STREAM, "updated_at": now, "data_status": "LIVE_PARTIAL", "station_count": len(stations), "status_source": "DHM Real Time Stream Flow", "stations": stations}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                write_health("LIVE_PARTIAL", now, f"River Watch unavailable; parsed {len(stations)} real-time stream-flow rows without River Watch trend/status fields.", len(stations))
                return
            raise RuntimeError(f"River Watch: {primary_error}; stream fallback: only {len(stations)} rows")
        except Exception as fallback_error:
            write_health("STALE", now, str(fallback_error))
            raise


if __name__ == "__main__":
    main()
