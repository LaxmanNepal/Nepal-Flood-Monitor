#!/usr/bin/env python3
"""Build a validated Nepal river-status snapshot from public government feeds.

Primary: BIPAD realtime river feed (which is synchronized from DHM).
Secondary: DHM River Watch browser page.
Tertiary: DHM realtime stream page.
"""
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "flood-data.json"
HEALTH = DATA / "source-health.json"
BIPAD_BASE = "https://bipadportal.gov.np/api/v1"
BIPAD_REALTIME = "https://bipadportal.gov.np/realtime/"
DHM_RIVER_WATCH = "https://www.dhm.gov.np/hydrology/river-watch"
DHM_STREAM = "https://www.dhm.gov.np/hydrology/realtime-stream"
UA = "Mozilla/5.0 (compatible; Nepal-Flood-Monitor/5.0; +https://github.com/LaxmanNepal/Nepal-Flood-Monitor)"


def clean(v):
    return re.sub(r"\s+", " ", unescape(str(v or ""))).strip()


def number(v):
    if v is None or v == "":
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", clean(v).replace(",", ""))
    return float(m.group()) if m else None


def load_meta():
    try:
        raw = json.loads((DATA / "stations.json").read_text(encoding="utf-8"))
        items = raw.get("stations", raw) if isinstance(raw, (dict, list)) else []
        return {clean(x.get("station_id")): x for x in items if isinstance(x, dict) and x.get("station_id")}
    except Exception:
        return {}


def risk(status, water=None, warning=None, danger=None):
    s = clean(status).lower()
    if "danger" in s or (water is not None and danger is not None and water >= danger):
        return "critical"
    if "warning" in s or (water is not None and warning is not None and water >= warning):
        return "warning"
    if "rising" in s:
        return "watch"
    if "offline" in s or "unavailable" in s:
        return "offline"
    return "normal"


def fetch_json(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    with urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def recursive_records(obj):
    """Yield dicts that look like station observations from arbitrary API envelopes."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from recursive_records(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from recursive_records(value)


def field(d, *names):
    if not isinstance(d, dict):
        return None
    norm = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in d.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in norm:
            return norm[key]
    # tolerate nested/common naming variations
    for k, v in norm.items():
        for name in names:
            n = re.sub(r"[^a-z0-9]", "", name.lower())
            if n in k or k in n:
                return v
    return None


def normalize_json(obj, meta, source):
    records = []
    for d in recursive_records(obj):
        station = field(d, "station_name", "stationName", "station", "title", "name")
        water = field(d, "water_level", "waterLevel", "water level", "current_water_level", "level")
        status = field(d, "status", "river_status", "riverStatus")
        basin = field(d, "basin", "basin_name", "basinName")
        station_id = field(d, "station_id", "stationId", "station_index", "stationIndex", "series_id", "seriesId", "id")
        # A useful observation normally has at least a station name plus water/status.
        if not station or (water is None and status is None):
            continue
        station_id = clean(station_id)
        extra = meta.get(station_id, {})
        warning = number(field(d, "warning_level", "warningLevel", "warning"))
        danger = number(field(d, "danger_level", "dangerLevel", "danger"))
        water_n = number(water)
        status_s = clean(status) or ("Observed" if water_n is not None else "Offline")
        lat = number(field(d, "latitude", "lat"))
        lon = number(field(d, "longitude", "lon", "lng"))
        records.append({
            "station_id": station_id or clean(extra.get("station_id")) or f"name:{clean(station).lower()}",
            "name": clean(station),
            "basin": clean(basin) or clean(extra.get("basin")),
            "district": clean(field(d, "district", "district_name", "districtName")) or clean(extra.get("district")),
            "water_level": water_n,
            "warning_level": warning if warning is not None else extra.get("warning_level"),
            "danger_level": danger if danger is not None else extra.get("danger_level"),
            "trend": clean(field(d, "trend", "steady", "water_trend", "waterTrend")) or "Unknown",
            "status": status_s,
            "risk_level": risk(status_s, water_n, warning, danger),
            "latitude": lat if lat is not None else extra.get("latitude"),
            "longitude": lon if lon is not None else extra.get("longitude"),
            "source": source,
        })
    unique = {}
    for r in records:
        unique[r["station_id"]] = r
    return list(unique.values())


def bipad_fetch(meta):
    """Try the public BIPAD river endpoints. BIPAD documents DHM as its river source."""
    urls = [
        f"{BIPAD_BASE}/river/",
        f"{BIPAD_BASE}/river-stations/",
        f"{BIPAD_BASE}/flood-station/",
    ]
    errors = []
    best = []
    for url in urls:
        try:
            obj = fetch_json(url)
            rows = normalize_json(obj, meta, "BIPAD/DHM")
            if len(rows) > len(best):
                best = rows
            if len(rows) >= 20:
                return rows, url
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("BIPAD returned no usable river dataset: " + " | ".join(errors))


def html_tables(html):
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    all_rows = []
    for table in tables:
        rows = []
        for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [clean(re.sub(r"<[^>]+>", " ", x)) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", raw, re.I | re.S)]
            if cells:
                rows.append(cells)
        if rows:
            all_rows.append(rows)
    return all_rows


def normalize_table(rows, meta, source):
    if not rows:
        return []
    header = [clean(x).lower() for x in rows[0]]
    def col(*names):
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None
    si = col("station no", "station number", "station index")
    bi = col("basin name", "basin")
    ni = col("station name", "name")
    di = col("district name", "district")
    wi = col("water level", "water lvl", "water")
    wni = col("warning level", "warning")
    dgi = col("danger level", "danger")
    ti = col("trend")
    sti = col("status")
    if None in (si, ni, wi):
        return []
    out = {}
    for c in rows[1:]:
        if max(si, ni, wi) >= len(c):
            continue
        name = clean(c[ni])
        if not name or name.lower() == "loading data...":
            continue
        sid = clean(c[si]); extra = meta.get(sid, {})
        water = number(c[wi])
        warning = number(c[wni]) if wni is not None and wni < len(c) else extra.get("warning_level")
        danger = number(c[dgi]) if dgi is not None and dgi < len(c) else extra.get("danger_level")
        status = clean(c[sti]) if sti is not None and sti < len(c) else ("Observed" if water is not None else "Offline")
        out[sid or f"name:{name.lower()}"] = {
            "station_id": sid or f"name:{name.lower()}", "name": name,
            "basin": clean(c[bi]) if bi is not None and bi < len(c) else extra.get("basin"),
            "district": clean(c[di]) if di is not None and di < len(c) else extra.get("district"),
            "water_level": water, "warning_level": warning, "danger_level": danger,
            "trend": clean(c[ti]) if ti is not None and ti < len(c) else "Unknown",
            "status": status, "risk_level": risk(status, water, warning, danger),
            "latitude": extra.get("latitude"), "longitude": extra.get("longitude"), "source": source,
        }
    return list(out.values())


def dhm_http_fallback(meta):
    req = Request(DHM_STREAM, headers={"User-Agent": UA})
    with urlopen(req, timeout=45) as r:
        html = r.read().decode("utf-8", "replace")
    best = []
    for rows in html_tables(html):
        got = normalize_table(rows, meta, "DHM Real Time Stream Flow")
        if len(got) > len(best): best = got
    if len(best) >= 20:
        return best, DHM_STREAM
    raise RuntimeError(f"DHM stream returned only {len(best)} usable rows")


def publish(stations, now, source_url, status, status_source):
    OUT.write_text(json.dumps({
        "schema_version": 6, "source": "Department of Hydrology and Meteorology (DHM), Government of Nepal",
        "source_url": source_url, "updated_at": now, "data_status": status,
        "station_count": len(stations), "status_source": status_source, "stations": stations,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def health(status, now, message, count=0, url=BIPAD_REALTIME):
    HEALTH.write_text(json.dumps({
        "schema_version": 5, "checked_at": now,
        "source": "DHM via BIPAD realtime integration",
        "source_url": url, "status": status, "stations_found": count, "message": message,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    now = datetime.now(timezone.utc).isoformat()
    meta = load_meta()
    try:
        stations, url = bipad_fetch(meta)
        publish(stations, now, url, "LIVE", "DHM via BIPAD")
        health("LIVE", now, f"Loaded {len(stations)} river stations from BIPAD's DHM-backed feed.", len(stations), url)
        print(f"LIVE: {len(stations)} river stations from {url}")
        return
    except Exception as primary:
        print(f"BIPAD river feed failed: {primary}")
    try:
        stations, url = dhm_http_fallback(meta)
        publish(stations, now, url, "LIVE_PARTIAL", "DHM Real Time Stream Flow")
        health("LIVE_PARTIAL", now, f"BIPAD failed; loaded {len(stations)} DHM stream observations.", len(stations), url)
        print(f"LIVE_PARTIAL: {len(stations)} river stations from {url}")
        return
    except Exception as fallback:
        health("STALE", now, f"BIPAD: {primary}; DHM stream: {fallback}")
        raise RuntimeError(f"All river feeds failed. BIPAD: {primary}; DHM: {fallback}")


if __name__ == "__main__":
    main()
