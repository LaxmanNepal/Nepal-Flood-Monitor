import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SNAPSHOT=ROOT/"data/flood-data.json"
HISTORY=ROOT/"data/station-history.json"
RETENTION=timedelta(days=7)
MIN_INTERVAL=timedelta(minutes=50)

def number(v):
    try:
        n=float(v)
        return n if abs(n)<90000 else None
    except (TypeError,ValueError):
        return None

def main():
    snapshot=json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    now=datetime.fromisoformat(snapshot["updated_at"].replace("Z","+00:00"))
    cutoff=now-RETENTION
    try:
        old=json.loads(HISTORY.read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError):
        old={}
    stations=old.get("stations",{}) if isinstance(old,dict) else {}
    for s in snapshot.get("stations",[]):
        sid=str(s.get("station_id","")).strip()
        water=number(s.get("water_level"))
        if not sid or water is None:
            continue
        item=stations.setdefault(sid,{"name":s.get("name",sid),"points":[]})
        item["name"]=s.get("name") or item.get("name") or sid
        points=[]
        for p in item.get("points",[]):
            if isinstance(p,list) and len(p)==2:
                try:
                    dt=datetime.fromtimestamp(float(p[0]),timezone.utc)
                    if dt>=cutoff:
                        points.append([int(float(p[0])),float(p[1])])
                except (TypeError,ValueError,OverflowError):
                    pass
        if not points or now-datetime.fromtimestamp(points[-1][0],timezone.utc)>=MIN_INTERVAL:
            points.append([int(now.timestamp()),water])
        item["points"]=points[-200:]
    for sid in list(stations):
        if not stations[sid].get("points"):
            del stations[sid]
    out={"schema_version":1,"generated_at":now.isoformat(),"retention_days":7,"sample_interval_minutes":60,"station_count":len(stations),"stations":stations}
    HISTORY.write_text(json.dumps(out,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")

if __name__=="__main__":
    main()
