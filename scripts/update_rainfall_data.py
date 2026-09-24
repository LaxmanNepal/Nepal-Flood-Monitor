#!/usr/bin/env python3
"""Build a validated rainfall snapshot from the official DHM Rainfall Watch page."""
import json,re
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from html import unescape
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"
OUT=DATA/"rainfall-data.json"; HEALTH=DATA/"rainfall-source-health.json"
DHM_URL="https://dhm.gov.np/hydrology/rainfall-watch-map"
BIPAD_URL="https://bipadportal.gov.np/api/v1/rain/?limit=1000&ordering=-measuredOn"
BIPAD_REALTIME_URL="https://bipadportal.gov.np/realtime/"
UA="Mozilla/5.0 (compatible; Nepal-Flood-Monitor/6.0; +https://github.com/LaxmanNepal/Nepal-Flood-Monitor)"
THRESHOLDS={"1h":60.0,"3h":80.0,"6h":100.0,"12h":120.0,"24h":140.0}

def clean(v):
    return re.sub(r"\s+"," ",unescape(str(v or ""))).strip()
def number(v):
    if v is None or v=="": return None
    m=re.search(r"-?\d+(?:\.\d+)?",clean(v).replace(",",""))
    return float(m.group()) if m else None
def fetch(url,timeout=30):
    return urlopen(Request(url,headers={"User-Agent":UA,"Accept":"text/html,application/xhtml+xml"}),timeout=timeout).read().decode("utf-8","replace")
def tables(html):
    for t in re.findall(r"<table[^>]*>(.*?)</table>",html,re.I|re.S):
        rows=[]
        for raw in re.findall(r"<tr[^>]*>(.*?)</tr>",t,re.I|re.S):
            cells=[]
            for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>",raw,re.I|re.S):
                href=re.search(r'href=["\']([^"\']+)["\']',x,re.I)
                text=clean(re.sub(r"<[^>]+>"," ",x))
                cells.append((text,href.group(1) if href else ""))
            if cells: rows.append(cells)
        if rows: yield rows
def status(row):
    vals=[clean(x[0]).lower() for x in row]
    s=" ".join(vals)
    if "danger" in s or "critical" in s: return "critical"
    if "warning" in s or "alert" in s: return "warning"
    if "watch" in s: return "watch"
    return "normal"
def coord_from_page(url):
    try:
        html=fetch(url,15)
        mlat=re.search(r"Latitude\s*</[^>]+>\s*([0-9.]+)",html,re.I|re.S)
        mlon=re.search(r"Longitude\s*</[^>]+>\s*([0-9.]+)",html,re.I|re.S)
        if mlat and mlon:
            lat,lon=float(mlat.group(1)),float(mlon.group(1))
            if 26<=lat<=31 and 80<=lon<=89:return lat,lon
    except Exception: pass
    return None

def json_fetch(url,timeout=30):
    req=Request(url,headers={"User-Agent":UA,"Accept":"application/json","X-Requested-With":"XMLHttpRequest"})
    raw=urlopen(req,timeout=timeout).read().decode("utf-8","replace")
    return json.loads(raw)

def records_from_json(obj):
    if isinstance(obj,dict):
        for key in ("results","data","records","rain","stations"):
            if isinstance(obj.get(key),list): return obj[key]
    return obj if isinstance(obj,list) else []

def first_value(d,*keys):
    if not isinstance(d,dict): return None
    for k in keys:
        if k in d and d[k] not in (None,""): return d[k]
    return None

def bipad_rainfall():
    # Prefer the public API, but BIPAD may return a non-JSON gateway page.
    # The realtime page is also a public BIPAD/DHM presentation of the same feed.
    try:
        obj=json_fetch(BIPAD_URL,45)
        raw=records_from_json(obj)
    except Exception as e:
        print(f"BIPAD API rainfall unavailable: {e}")
        raw=[]
    out=[]
    for item in raw:
        if not isinstance(item,dict): continue
        station=item.get("station") if isinstance(item.get("station"),dict) else {}
        point=item.get("point") if isinstance(item.get("point"),dict) else {}
        sid=first_value(item,"stationSeriesId","station_id","station_no","station_number") or first_value(station,"id","station_id") or item.get("id")
        name=first_value(item,"title","station_name","name") or first_value(station,"name","title")
        if not name: continue
        averages=item.get("averages") if isinstance(item.get("averages"),list) else []
        rain={"1h":None,"3h":None,"6h":None,"12h":None,"24h":None}
        for avg in averages:
            if not isinstance(avg,dict): continue
            interval=number(avg.get("interval")); value=number(avg.get("value"))
            key={1:"1h",3:"3h",6:"6h",12:"12h",24:"24h"}.get(int(interval) if interval is not None else 0)
            if key: rain[key]=value
        coords=point.get("coordinates") if isinstance(point.get("coordinates"),list) else []
        lon=number(coords[0]) if len(coords)>1 else None
        lat=number(coords[1]) if len(coords)>1 else None
        if lat is not None and not (26<=lat<=31): lat=None
        if lon is not None and not (80<=lon<=89): lon=None
        out.append({"station_id":str(sid or name),"name":str(name),"basin":str(first_value(item,"basin","basin_name") or first_value(station,"basin","basin_name") or ""), "district":str(first_value(item,"district","district_name") or first_value(station,"district","district_name") or ""), "rainfall":rain, "status":status([((str(first_value(item,"status") or "")), "")]), "latitude":lat,"longitude":lon,"source":"BIPAD/DHM Rain Watch","source_url":BIPAD_REALTIME_URL,"source_period":"1h","observed_at":str(item.get("measuredOn") or "")} )
    if len(out)>=20:
        return out

    # Server-rendered fallback used when the API endpoint is behind a JSON-incompatible gateway.
    try:
        html=fetch(BIPAD_REALTIME_URL,45)
        parsed=[]
        for rows in tables(html):
            for cells in rows:
                vals=[clean(x[0]) for x in cells]
                if len(vals)<6: continue
                low=" ".join(v.lower() for v in vals)
                if "station name" in low or "rainfall" in low and "status" in low: continue
                # BIPAD realtime columns: basin, station, date, time, rainfall, status.
                basin,name,date,time_,rain,status_text=vals[-6:]
                value=number(rain)
                if not name or value is None and rain not in ("-","—"): continue
                if not name or name.lower() in {"station name","station"}: continue
                parsed.append({"station_id":name,"name":name,"basin":basin if basin!="-" else "","district":"","rainfall":{"1h":value,"3h":None,"6h":None,"12h":None,"24h":None},"status":status([(status_text,"")]),"latitude":None,"longitude":None,"source":"BIPAD/DHM Rain Watch","source_url":BIPAD_REALTIME_URL,"source_period":"1h","observed_at":f"{date}T{time_}"} )
        # Keep the latest row for each station name when the page contains multiple samples.
        latest={}
        for row in parsed: latest[row["name"]]=row
        out=list(latest.values())
    except Exception as e:
        print(f"BIPAD realtime rainfall fallback failed: {e}")
    return out

def station_url(href,number_id):
    if href:
        if href.startswith("http"): return href
        if href.startswith("/"): return "https://dhm.gov.np"+href
        return "https://dhm.gov.np/"+href.lstrip("./")
    return f"https://dhm.gov.np/hydrology/rainfallSingle/{number_id}" if number_id else ""
def main():
    now=datetime.now(timezone.utc).isoformat()
    html=fetch(DHM_URL,45); best=[]
    for rows in tables(html):
        if len(rows)<2: continue
        heads=[clean(x[0]).lower() for x in rows[0]]
        def col(*needles):
            for n in needles:
                for i,h in enumerate(heads):
                    if n in h:return i
            return None
        ni=col("station name","station")
        if ni is None: continue
        si=col("station no","station number","station index")
        bi=col("basin")
        di=col("district")
        # The DHM table has five accumulated-rainfall columns in order.
        ri=[col("1 hour"),col("3 hour"),col("6 hour"),col("12 hour"),col("24 hour")]
        if any(i is None for i in ri): 
            # fallback: locate five numeric columns after the station identity fields
            ri=[]
        rows_out=[]
        for cells in rows[1:]:
            if ni>=len(cells): continue
            name=clean(cells[ni][0]); sid=clean(cells[si][0]) if si is not None and si<len(cells) else ""
            if not name or len(name)<2: continue
            nums=[number(cells[i][0]) if i is not None and i<len(cells) else None for i in ri]
            if not ri:
                candidates=[]
                for j,c in enumerate(cells):
                    if j in {x for x in (si,ni,bi,di) if x is not None}: continue
                    n=number(c[0])
                    if n is not None: candidates.append((j,n))
                nums=[x[1] for x in candidates[:5]]
                if len(nums)<5: nums += [None]*(5-len(nums))
            values={k:nums[i] if i<len(nums) else None for i,k in enumerate(("1h","3h","6h","12h","24h"))}
            href=cells[ni][1] if ni<len(cells) else ""
            row={"station_id":sid or name.lower().replace(" ","-"),"name":name,"basin":clean(cells[bi][0]) if bi is not None and bi<len(cells) else "","district":clean(cells[di][0]) if di is not None and di<len(cells) else "","rainfall":values,"status":status([c for c in cells]),"latitude":None,"longitude":None,"source":"DHM Rainfall Watch","source_url":station_url(href,sid)}
            rows_out.append(row)
        if len(rows_out)>len(best): best=rows_out
    if len(best)<20:
        try:
            fallback=bipad_rainfall()
            if len(fallback)>=20:
                best=fallback
        except Exception as e:
            print(f"BIPAD rainfall fallback failed: {e}")
    if len(best)<20: raise RuntimeError(f"Rainfall sources returned only {len(best)} usable stations")
    observed=[]
    for row in best:
        try:
            observed.append(datetime.fromisoformat(str(row.get("observed_at","")).replace("Z","+00:00")))
        except Exception:
            pass
    latest_observation=max(observed) if observed else None
    freshness_hours=((datetime.now(timezone.utc)-latest_observation).total_seconds()/3600) if latest_observation else None
    snapshot_status="LIVE" if freshness_hours is not None and freshness_hours <= 6 else "STALE"
    # Add coordinates only from official station pages. Missing coordinates remain explicit.
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures={pool.submit(coord_from_page,s["source_url"]):i for i,s in enumerate(best) if s.get("source_url")}
        for f in as_completed(futures):
            p=f.result()
            if p:
                best[futures[f]]["latitude"],best[futures[f]]["longitude"]=p
    for s in best:
        r=s["rainfall"]; peak=max((v for v in r.values() if v is not None),default=None)
        exceeded=[k for k,v in r.items() if v is not None and v>=THRESHOLDS[k]]
        s["peak_mm"]=peak
        s["alert_windows"]=exceeded
        s["risk_level"]="critical" if exceeded else ("watch" if peak is not None and any(v is not None and v>=THRESHOLDS[k]*.75 for k,v in r.items()) else "normal")
    OUT.write_text(json.dumps({"schema_version":1,"source":"Department of Hydrology and Meteorology (DHM), Government of Nepal","source_url":DHM_URL,"updated_at":now,"data_status":snapshot_status,"station_count":len(best),"thresholds_mm":THRESHOLDS,"stations":best,"latest_observation_at":latest_observation.isoformat() if latest_observation else None,"source_status":snapshot_status},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    HEALTH.write_text(json.dumps({"schema_version":1,"checked_at":now,"source":"DHM Rainfall Watch / BIPAD","source_url":DHM_URL,"status":snapshot_status,"stations_found":len(best),"stations_with_coordinates":sum(1 for s in best if s.get("latitude") is not None),"message":("Loaded recent accumulated rainfall observations." if snapshot_status=="LIVE" else "Source reachable, but latest rainfall observation is stale.")},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"LIVE: {len(best)} rainfall stations; coordinates: {sum(1 for s in best if s.get('latitude') is not None)}")
if __name__=="__main__": main()
