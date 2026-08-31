#!/usr/bin/env python3
"""Collect DHM River Watch data and publish a validated static snapshot."""
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


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_meta():
    try:
        raw = json.loads((DATA / "stations.json").read_text(encoding="utf-8"))
        items = raw.get("stations", raw) if isinstance(raw, (dict, list)) else []
        return {clean(x.get("station_id")): x for x in items if x.get("station_id")}
    except Exception:
        return {}


def risk_from_status(status, water=None, warning=None, danger=None):
    text = clean(status).lower()
    if "danger" in text or (water is not None and danger is not None and water >= danger):
        return "critical"
    if "warning" in text or (water is not None and warning is not None and water >= warning):
        return "warning"
    if "rising" in text:
        return "watch"
    if "offline" in text or "unavailable" in text:
        return "offline"
    return "normal"


def table_rows(page):
    """Extract every rendered table; prefer the table containing River Watch headers."""
    tables = page.locator("table")
    candidates = []
    for i in range(tables.count()):
        rows = tables.nth(i).locator("tr")
        parsed = []
        for r in range(rows.count()):
            cells = rows.nth(r).locator("th,td")
            vals = [clean(cells.nth(c).inner_text()) for c in range(cells.count())]
            if vals:
                parsed.append(vals)
        if parsed:
            candidates.append(parsed)
    if not candidates:
        return []
    def score(rows):
        h = " | ".join(x.lower() for x in rows[0])
        return (100 if "station no" in h else 0) + (30 if "water level" in h else 0) + (30 if "warning level" in h else 0) + (30 if "danger level" in h else 0) + min(len(rows), 100) / 100
    return max(candidates, key=score)


def extract_rows_with_playwright():
    """Load the real page and capture both rendered HTML and JSON/XHR responses.

    DHM currently renders River Watch client-side. The collector therefore does not
    assume a permanent API URL: it records suitable JSON responses observed by the
    browser and falls back to the rendered table when necessary.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1200}, locale="en-US")
        page = context.new_page()
        captured = []

        def on_response(response):
            url = response.url.lower()
            ctype = response.headers.get("content-type", "").lower()
            if response.request.resource_type in {"xhr", "fetch"} or "json" in ctype:
                if any(k in url for k in ("river", "hydro", "station", "watch", "api")):
                    try:
                        text = response.text()
                        if len(text) < 5_000_000:
                            captured.append((response.url, ctype, text))
                    except Exception:
                        pass

        page.on("response", on_response)
        page.goto(RIVER_WATCH, wait_until="domcontentloaded", timeout=90000)
        try:
            page.get_by_text("Tabular View", exact=True).click(timeout=7000)
        except Exception:
            pass
        # Wait for actual data rows, not merely the page shell.
        try:
            page.wait_for_function("""() => [...document.querySelectorAll('table tr')].some(r => r.querySelectorAll('td').length >= 7)""", timeout=30000)
        except Exception:
            page.wait_for_timeout(10000)

        rows = table_rows(page)
        browser.close()
        return rows, captured


def json_rows(captured):
    """Best-effort extraction from captured JSON without depending on undocumented keys."""
    for url, ctype, text in captured:
        if not text.lstrip().startswith(("{", "[")):
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                values = list(cur.values())
                stack.extend(values)
                if len(cur) >= 5:
                    keys = {re.sub(r"[^a-z]", "", str(k).lower()) for k in cur}
                    if any("station" in k for k in keys) and any("water" in k or "level" in k for k in keys):
                        return obj, url
            elif isinstance(cur, list):
                stack.extend(cur[:1000])
    return None, None


def normalize_rows(rows, meta):
    if not rows:
        return []
    header = [clean(x).lower() for x in rows[0]]
    def col(*names):
        for name in names:
            for i, value in enumerate(header):
                if name in value:
                    return i
        return None
    station_i = col("station no", "station number", "station index")
    basin_i = col("basin name", "basin")
    name_i = col("station name")
    district_i = col("district name", "district")
    water_i = col("water level", "water lvl")
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
        station_id = clean(cells[station_i])
        name = clean(cells[name_i])
        if not name or name.lower() in {"station name", "loading data..."}:
            continue
        key = station_id or f"name:{name.lower()}"
        if key in seen:
            continue
        seen.add(key)
        water, warning, danger = number(cells[water_i]), number(cells[warning_i]), number(cells[danger_i])
        status = clean(cells[status_i]) or ("Offline" if water is None else "Below Warning Level and Steady")
        extra = meta.get(station_id, {})
        result.append({
            "station_id": station_id or key,
            "name": name,
            "basin": clean(cells[basin_i]) if basin_i is not None and basin_i < len(cells) else extra.get("basin"),
            "district": clean(cells[district_i]) if district_i is not None and district_i < len(cells) else extra.get("district"),
            "water_level": water,
            "warning_level": warning,
            "danger_level": danger,
            "trend": clean(cells[trend_i]),
            "status": status,
            "risk_level": risk_from_status(status, water, warning, danger),
            "latitude": extra.get("latitude"),
            "longitude": extra.get("longitude"),
            "source": "DHM River Watch"
        })
    return result


def write_health(status, now, message, count=0, source_url=RIVER_WATCH):
    HEALTH.write_text(json.dumps({"schema_version": 4, "checked_at": now, "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal", "source_url": source_url, "status": status, "stations_found": count, "message": message}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stream_fallback(meta):
    req = Request(STREAM, headers={"User-Agent": "Mozilla/5.0 (Nepal-Flood-Monitor/4.0)"})
    with urlopen(req, timeout=60) as response:
        html = response.read().decode("utf-8", "replace")
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    for table in tables:
        rows = []
        for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [clean(re.sub(r"<[^>]+>", " ", x)) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw, re.I | re.S)]
            if cells: rows.append(cells)
        if rows and any("station" in x.lower() for x in rows[0]) and any("water" in x.lower() for x in rows[0]):
            h = [x.lower() for x in rows[0]]
            def col(*names):
                for n in names:
                    for i, v in enumerate(h):
                        if n in v: return i
                return None
            si, ni, wi = col("station index", "station no", "station"), col("station name", "name"), col("water lvl", "water level", "water")
            if None in (si, ni, wi): continue
            out=[]
            for c in rows[1:]:
                if max(si,ni,wi)>=len(c) or not c[ni]: continue
                sid, water = c[si], number(c[wi]); extra=meta.get(sid,{})
                out.append({"station_id":sid,"name":c[ni],"basin":extra.get("basin"),"district":extra.get("district"),"water_level":water,"warning_level":extra.get("warning_level"),"danger_level":extra.get("danger_level"),"trend":"Unknown","status":"Observed" if water is not None else "Offline","risk_level":risk_from_status("Observed",water,extra.get("warning_level"),extra.get("danger_level")),"latitude":extra.get("latitude"),"longitude":extra.get("longitude"),"source":"DHM Real Time Stream Flow"})
            return out
    return []


def publish(stations, now, source_url, data_status, status_source):
    OUT.write_text(json.dumps({"schema_version":5,"source":"Department of Hydrology and Meteorology (DHM), Government of Nepal","source_url":source_url,"updated_at":now,"data_status":data_status,"station_count":len(stations),"status_source":status_source,"stations":stations},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def main():
    now=datetime.now(timezone.utc).isoformat(); meta=load_meta()
    try:
        rows,captured=extract_rows_with_playwright()
        stations=normalize_rows(rows,meta)
        if len(stations)<20:
            raise RuntimeError(f"Only {len(stations)} River Watch rows validated; captured={len(captured)} network responses")
        publish(stations,now,RIVER_WATCH,"LIVE","DHM River Watch")
        write_health("LIVE",now,f"Parsed {len(stations)} complete River Watch stations.",len(stations))
        print(json.dumps({"status":"LIVE","stations":len(stations)})); return
    except Exception as primary:
        print(f"River Watch failed: {primary}")
        try:
            stations=stream_fallback(meta)
            if len(stations)>=20:
                publish(stations,now,STREAM,"LIVE_PARTIAL","DHM Real Time Stream Flow")
                write_health("LIVE_PARTIAL",now,f"River Watch failed; parsed {len(stations)} stream-flow observations.",len(stations),STREAM); return
            raise RuntimeError(f"only {len(stations)} stream rows")
        except Exception as fallback:
            write_health("STALE",now,f"River Watch: {primary}; stream fallback: {fallback}")
            raise RuntimeError(f"DHM collection failed: {primary}; fallback: {fallback}")

if __name__ == "__main__": main()
