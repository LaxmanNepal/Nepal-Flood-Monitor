#!/usr/bin/env python3
"""Fetch and normalize DHM's public real-time stream-flow table."""
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; OUT=DATA/'flood-data.json'; HEALTH=DATA/'source-health.json'
SOURCES=[
    'https://www.dhm.gov.np/hydrology/realtime-stream',
    'https://www.dhm.gov.np/bhasa/hydrology_realtime-stream/np',
]

def fetch(url):
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; Nepal-Flood-Monitor/2.0)','Accept':'text/html,application/xhtml+xml'})
    with urlopen(req,timeout=45) as r:
        return r.read().decode('utf-8','replace'),r.headers.get('Content-Type','')

def clean(v):
    return re.sub(r'\s+',' ',unescape(re.sub(r'<[^>]+>',' ',v))).strip()

def number(v):
    m=re.search(r'-?\d+(?:\.\d+)?',v or '')
    return float(m.group()) if m else None

def table_rows(html):
    out=[]
    for table in re.findall(r'<table[^>]*>(.*?)</table>',html,re.I|re.S):
        rows=[]
        for raw in re.findall(r'<tr[^>]*>(.*?)</tr>',table,re.I|re.S):
            cells=[clean(x) for x in re.findall(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>',raw,re.I|re.S)]
            if cells: rows.append(cells)
        if rows: out.append(rows)
    return out

def find_rows(html):
    for rows in table_rows(html):
        h=[x.lower() for x in rows[0]]
        if any('station' in x for x in h) and any('water' in x for x in h): return rows
    return []

def load_meta():
    try:
        items=json.loads((DATA/'stations.json').read_text())
        return {str(x.get('station_id')):x for x in items if x.get('station_id')}
    except Exception: return {}

def normalize(rows,meta):
    h=[x.lower() for x in rows[0]]
    def col(*names):
        for n in names:
            for i,v in enumerate(h):
                if n in v:return i
        return None
    si=col('station index','station no','station'); ni=col('station name','name'); wi=col('water lvl','water level','water')
    bi=col('basin'); di=col('district'); fi=col('discharge','flow')
    if si is None or ni is None or wi is None:return []
    result=[]
    for c in rows[1:]:
        if max(si,ni,wi)>=len(c):continue
        name=c[ni].strip()
        if not name:continue
        station_id=c[si].strip() or f'name:{name.lower()}'
        water=number(c[wi]); extra=meta.get(c[si].strip(),{}) if c[si].strip() else {}
        result.append({
            'station_id':station_id,'name':name,
            'basin':c[bi] if bi is not None and bi<len(c) else extra.get('basin'),
            'district':c[di] if di is not None and di<len(c) else extra.get('district'),
            'water_level':water,'discharge':number(c[fi]) if fi is not None and fi<len(c) else None,
            'warning_level':extra.get('warning_level'),'danger_level':extra.get('danger_level'),
            'trend':None,'status':'offline' if water is None else 'observed',
            'latitude':extra.get('latitude'),'longitude':extra.get('longitude')
        })
    return result

def health(status,now,message,count=0,url=None):
    HEALTH.write_text(json.dumps({'schema_version':2,'checked_at':now,'source':'Department of Hydrology and Meteorology (DHM), Government of Nepal','source_url':url or SOURCES[0],'status':status,'stations_found':count,'message':message},indent=2)+'\n')

def main():
    now=datetime.now(timezone.utc).isoformat(); last_error=''; meta=load_meta()
    for url in SOURCES:
        try:
            html,ctype=fetch(url); rows=find_rows(html); stations=normalize(rows,meta)
            if len(stations)>=20:
                payload={'schema_version':3,'source':'Department of Hydrology and Meteorology (DHM), Government of Nepal','source_url':url,'updated_at':now,'data_status':'LIVE','note':'Live observations are shown separately from official warning/danger classifications unless authoritative thresholds are available.','stations':stations}
                OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
                health('LIVE',now,f'Parsed {len(stations)} DHM real-time stream-flow rows.',len(stations),url)
                print(json.dumps({'status':'LIVE','stations':len(stations),'source':url,'content_type':ctype})); return
            last_error=f'Only {len(stations)} rows parsed from {url}'
        except Exception as e: last_error=f'{type(e).__name__}: {e}'
    health('STALE',now,last_error)
    print(last_error)

if __name__=='__main__':main()
