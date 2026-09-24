#!/usr/bin/env python3
"""Build a validated Nepal river-status snapshot from public government feeds."""
import json,re
from datetime import datetime,timezone
from html import unescape
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"; OUT=DATA/"flood-data.json"; HEALTH=DATA/"source-health.json"
BIPAD_BASE="https://bipadportal.gov.np/api/v1"; DHM_STREAM="https://www.dhm.gov.np/hydrology/realtime-stream"
UA="Mozilla/5.0 (compatible; Nepal-Flood-Monitor/6.0; +https://github.com/LaxmanNepal/Nepal-Flood-Monitor)"

def clean(v): return re.sub(r"\s+"," ",unescape(str(v or ""))).strip()
def number(v):
    if v is None or v=="": return None
    m=re.search(r"-?\d+(?:\.\d+)?",clean(v).replace(",","")); return float(m.group()) if m else None

def load_meta():
    try:
        raw=json.loads((DATA/"stations.json").read_text(encoding="utf-8")); items=raw.get("stations",raw) if isinstance(raw,(dict,list)) else []
        return {clean(x.get("station_id")):x for x in items if isinstance(x,dict) and x.get("station_id")}
    except Exception:return {}

def station_meta(meta, sid, name):
    if sid and sid in meta:return meta[sid]
    n=clean(name).lower()
    for x in meta.values():
        xn=clean(x.get("name")).lower()
        if xn and (xn==n or xn in n or n in xn):return x
    return {}

def risk(status,water=None,warning=None,danger=None):
    # Thresholds are authoritative; "BELOW WARNING LEVEL" is not a warning.
    s=clean(status).lower()
    if "offline" in s or "unavailable" in s:return "offline"
    if water is not None and danger is not None and water>=danger:return "critical"
    if water is not None and warning is not None and water>=warning:return "warning"
    if "above danger" in s:return "critical"
    if "above warning" in s:return "warning"
    if "rising" in s:return "watch"
    return "normal"

def fetch_json(url):
    r=urlopen(Request(url,headers={"User-Agent":UA,"Accept":"application/json"}),timeout=45); return json.loads(r.read().decode("utf-8","replace"))

def coordinate_pair(v):
    """Extract a WGS84 Nepal coordinate pair from common GeoJSON/BIPAD shapes."""
    if isinstance(v,str):
        raw=v.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:return coordinate_pair(json.loads(raw))
            except Exception:pass
        nums=re.findall(r"-?\\d+(?:\\.\\d+)?",raw)
        if len(nums)>=2:
            return coordinate_pair([float(nums[0]),float(nums[1])])
    if isinstance(v,(list,tuple)) and len(v)>=2:
        a,b=number(v[0]),number(v[1])
        if a is not None and b is not None:
            if 80<=a<=89 and 26<=b<=31:return b,a
            if 26<=a<=31 and 80<=b<=89:return a,b
    if isinstance(v,dict):
        for key in ("coordinates","coord","point","location","geometry","geo_point","geoPoint","geopoint","geo_location","geoLocation"):
            if key in v:
                p=coordinate_pair(v[key])
                if p:return p
        lat=number(field(v,"latitude","lat","y")); lon=number(field(v,"longitude","lon","lng","x"))
        if lat is not None and lon is not None and 26<=lat<=31 and 80<=lon<=89:return lat,lon
    return None

def load_station_locations():
    registry={}
    try:
        obj=fetch_json(f"{BIPAD_BASE}/station-location/")
        for row in candidates(obj):
            if not isinstance(row,dict):continue
            pair=coordinate_pair(row)
            if not pair:continue
            name=clean(field(row,"station_name","stationName","station","title","name"))
            sid=clean(field(row,"station_id","stationId","series_id","seriesId","station_index","stationIndex","id"))
            if sid:registry[f"id:{sid}"]={"latitude":pair[0],"longitude":pair[1],"name":name}
            if name:registry[f"name:{name.lower()}"]={"latitude":pair[0],"longitude":pair[1],"name":name}
    except Exception:
        pass
    return registry

def location_for(registry,sid,name):
    if sid and f"id:{sid}" in registry:return registry[f"id:{sid}"]
    n=clean(name).lower()
    if n and f"name:{n}" in registry:return registry[f"name:{n}"]
    for v in registry.values():
        vn=clean(v.get("name")).lower()
        if vn and n and (vn in n or n in vn):return v
    return {}

def field(d,*names):
    if not isinstance(d,dict):return None
    norm={re.sub(r"[^a-z0-9]","",str(k).lower()):v for k,v in d.items()}
    for name in names:
        k=re.sub(r"[^a-z0-9]","",name.lower())
        if k in norm:return norm[k]
    return None

def candidates(obj):
    """Only inspect likely collection containers; never treat arbitrary metadata as stations."""
    if isinstance(obj,list): return obj
    if not isinstance(obj,dict): return []
    for key in ("results","data","items","stations","river_stations","riverStations","objects"):
        value=obj.get(key)
        if isinstance(value,list): return value
        if isinstance(value,dict):
            nested=candidates(value)
            if nested:return nested
    return []

def valid_name(v):
    s=clean(v)
    if not s or len(s)<3 or len(s)>160:return False
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?",s):return False
    return bool(re.search(r"[A-Za-z\u0900-\u097F]",s))

def normalize(obj,meta,source,locations=None):
    locations=locations or {}; rows=candidates(obj); out={}
    for d in rows:
        if not isinstance(d,dict):continue
        name=field(d,"station_name","stationName","station","title","name")
        water=field(d,"water_level","waterLevel","current_water_level","level")
        status=field(d,"status","river_status","riverStatus")
        if not valid_name(name) or (water is None and status is None):continue
        sid=clean(field(d,"station_id","stationId","station_index","stationIndex","series_id","seriesId","id"))
        extra=station_meta(meta,sid,name)
        loc=location_for(locations,sid,name)
        wn=number(field(d,"warning_level","warningLevel","warning")); dn=number(field(d,"danger_level","dangerLevel","danger")); wl=number(water)
        st=clean(status) or ("Observed" if wl is not None else "Offline")
        pair=coordinate_pair(d)
        lat=pair[0] if pair else number(field(d,"latitude","lat"))
        lon=pair[1] if pair else number(field(d,"longitude","lon","lng"))
        # Reject impossible geographic values and obvious metadata leakage.
        if lat is not None and not 26<=lat<=31:lat=None
        if lon is not None and not 80<=lon<=89:lon=None
        key=sid or f"name:{clean(name).lower()}"
        out[key]={"station_id":sid or key,"name":clean(name),"basin":clean(field(d,"basin","basin_name","basinName")) or clean(extra.get("basin")),"district":clean(field(d,"district","district_name","districtName")) or clean(extra.get("district")),"water_level":wl,"warning_level":wn if wn is not None else extra.get("warning_level"),"danger_level":dn if dn is not None else extra.get("danger_level"),"trend":clean(field(d,"trend","water_trend","waterTrend")) or "Unknown","status":st,"risk_level":risk(st,wl,wn,dn),"latitude":lat if lat is not None else extra.get("latitude") if extra.get("latitude") is not None else loc.get("latitude"),"longitude":lon if lon is not None else extra.get("longitude") if extra.get("longitude") is not None else loc.get("longitude"),"source":source}
    return list(out.values())

def bipad(meta,locations):
    errors=[]; best=[]
    for endpoint in ("river/","river-stations/","flood-station/","river-trimed/"):
        url=f"{BIPAD_BASE}/{endpoint}"
        try:
            rows=normalize(fetch_json(url),meta,"BIPAD/DHM",locations)
            if len(rows)>len(best):best=rows
            if 20<=len(rows)<=500:return rows,url
        except Exception as e:errors.append(f"{endpoint}: {e}")
    raise RuntimeError(f"No valid BIPAD station collection; best={len(best)}; {' | '.join(errors)}")

def tables(html):
    for t in re.findall(r"<table[^>]*>(.*?)</table>",html,re.I|re.S):
        rows=[]
        for raw in re.findall(r"<tr[^>]*>(.*?)</tr>",t,re.I|re.S):
            c=[clean(re.sub(r"<[^>]+>"," ",x)) for x in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>",raw,re.I|re.S)]
            if c:rows.append(c)
        if rows:yield rows

def dhm(meta):
    html=urlopen(Request(DHM_STREAM,headers={"User-Agent":UA}),timeout=45).read().decode("utf-8","replace"); best=[]
    for rows in tables(html):
        if len(rows)<2:continue
        h=[clean(x).lower() for x in rows[0]]
        def col(*names):
            for n in names:
                for i,x in enumerate(h):
                    if n in x:return i
        si=col("station no","station number","station index"); ni=col("station name","name"); wi=col("water level","water lvl")
        if si is None or ni is None or wi is None:continue
        bi=col("basin"); di=col("district"); wn=col("warning"); dn=col("danger"); ti=col("trend"); sti=col("status"); out={}
        for c in rows[1:]:
            if max(si,ni,wi)>=len(c):continue
            name=clean(c[ni]); sid=clean(c[si])
            if not valid_name(name):continue
            wl=number(c[wi]); w=number(c[wn]) if wn is not None and wn<len(c) else None; d=number(c[dn]) if dn is not None and dn<len(c) else None; st=clean(c[sti]) if sti is not None and sti<len(c) else "Observed"; extra=meta.get(sid,{})
            out[sid or f"name:{name.lower()}"]={"station_id":sid or f"name:{name.lower()}","name":name,"basin":clean(c[bi]) if bi is not None and bi<len(c) else extra.get("basin"),"district":clean(c[di]) if di is not None and di<len(c) else extra.get("district"),"water_level":wl,"warning_level":w,"danger_level":d,"trend":clean(c[ti]) if ti is not None and ti<len(c) else "Unknown","status":st,"risk_level":risk(st,wl,w,d),"latitude":extra.get("latitude"),"longitude":extra.get("longitude"),"source":"DHM"}
        if len(out)>len(best):best=list(out.values())
    if len(best)>=20:return best,DHM_STREAM
    raise RuntimeError(f"DHM returned only {len(best)} valid rows")

def write(stations,now,url,status,source):
    OUT.write_text(json.dumps({"schema_version":7,"source":"Department of Hydrology and Meteorology (DHM), Government of Nepal","source_url":url,"updated_at":now,"data_status":status,"station_count":len(stations),"status_source":source,"stations":stations},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def health(status,now,msg,count=0,url=""):
    HEALTH.write_text(json.dumps({"schema_version":6,"checked_at":now,"source":"DHM via BIPAD","source_url":url,"status":status,"stations_found":count,"message":msg},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def main():
    now=datetime.now(timezone.utc).isoformat(); meta=load_meta(); locations=load_station_locations()
    try:
        rows,url=bipad(meta,locations); write(rows,now,url,"LIVE","DHM via BIPAD"); health("LIVE",now,f"Loaded {len(rows)} validated river stations.",len(rows),url); print(f"LIVE: {len(rows)} validated river stations"); return
    except Exception as e: print(f"BIPAD failed: {e}")
    try:
        rows,url=dhm(meta); write(rows,now,url,"LIVE_PARTIAL","DHM River Watch/Stream"); health("LIVE_PARTIAL",now,f"Loaded {len(rows)} validated DHM stations.",len(rows),url); print(f"LIVE_PARTIAL: {len(rows)} validated DHM stations"); return
    except Exception as e:
        health("STALE",now,f"All feeds failed: {e}"); raise RuntimeError(f"All river feeds failed: {e}")

if __name__=="__main__":main()
