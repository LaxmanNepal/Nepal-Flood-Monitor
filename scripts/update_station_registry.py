#!/usr/bin/env python3
"""Build station coordinates from DHM's official station PDF."""
import json, re, subprocess, tempfile
from pathlib import Path
from urllib.request import Request, urlopen
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/stations.json'
PDF_URL='https://www.dhm.gov.np/uploads/dhm/downloads/Updated_Location_and_Catchment_Area_of_Hydrological_Stations.pdf'
def main():
    req=Request(PDF_URL,headers={'User-Agent':'Nepal-Flood-Monitor/1.0'})
    with urlopen(req,timeout=60) as r: data=r.read()
    with tempfile.NamedTemporaryFile(suffix='.pdf') as pdf, tempfile.NamedTemporaryFile(mode='w+b') as txt:
        pdf.write(data); pdf.flush(); subprocess.run(['pdftotext','-layout',pdf.name,txt.name],check=True)
        txt.seek(0); text=txt.read().decode('utf-8','replace')
    pattern=re.compile(r'(?m)^\s*(\d{1,3})\s+(\d+(?:\.\d+)?)\s+(.+?)\s{2,}(.+?)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*$')
    out=[]; seen=set()
    for m in pattern.finditer(text):
        _,sid,river,location,lat,lon,elev,drain=m.groups()
        if sid in seen: continue
        seen.add(sid); out.append({'station_id':sid,'river':' '.join(river.split()),'location':' '.join(location.split()),'latitude':float(lat),'longitude':float(lon),'elevation_m':float(elev),'drainage_km2':float(drain),'source_url':PDF_URL})
    if len(out)<150: raise RuntimeError(f'Incomplete DHM registry: {len(out)} records')
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Published {len(out)} DHM station coordinates')
if __name__=='__main__': main()
